"""Mantiene vivo un proceso de FFmpeg que lleva una camara RTSP a MediaMTX."""

import collections
import ctypes
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from typing import IO, Any

from camara.configuracion import ocultar_credenciales

logger = logging.getLogger(__name__)

SIN_CONFIGURAR = "sin_configurar"
CONECTANDO = "conectando"
TRANSMITIENDO = "transmitiendo"
RECONECTANDO = "reconectando"
DETENIDO = "detenido"

_CADA_CUANTO_REVISAR_S = 0.2
_ESPERA_AL_CERRAR_S = 3.0
_VIDEO_RECIENTE_S = 5.0


def comando_ffmpeg(ejecutable: Sequence[str], url: str, destino: str) -> list[str]:
    return [
        *ejecutable,
        "-hide_banner", "-loglevel", "warning", "-nostats", "-progress", "pipe:1",
        # El timeout (microsegundos) hace que una camara muda termine en error en vez de colgarse para siempre.
        "-rtsp_transport", "tcp", "-timeout", "10000000",
        "-i", url,
        "-map", "0:v:0", "-an",
        # -pix_fmt obliga a recodificar: con -c copy se ignoraria. Algunas camaras, como Imou, envian rango completo,
        # y solo con -pix_fmt la salida seguiria siendo yuvj420p: el filtro la lleva a yuv420p de verdad.
        "-vf", "scale=out_range=tv",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-g", "30",
        "-f", "rtsp", "-rtsp_transport", "tcp", destino,
    ]


class _AtarAlPadre:
    """Hace que el sistema cierre FFmpeg si este programa muere, incluso a la fuerza, sin dejar huerfanos."""

    def __init__(self) -> None:
        self._trabajo: Any = None
        if sys.platform != "win32":
            return

        class _Basico(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong),
            ]

        class _Extendido(ctypes.Structure):
            _fields_ = [
                ("Basic", _Basico), ("IoInfo", ctypes.c_ulonglong * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._kernel32 = kernel32
        trabajo = kernel32.CreateJobObjectW(None, None)
        limites = _Extendido()
        limites.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        extendida = 9  # JobObjectExtendedLimitInformation
        if trabajo and kernel32.SetInformationJobObject(trabajo, extendida, ctypes.byref(limites), ctypes.sizeof(limites)):
            self._trabajo = trabajo
        else:
            logger.warning("No se pudo proteger FFmpeg contra quedar huerfano")

    def atar(self, proceso: subprocess.Popen[bytes]) -> None:
        if self._trabajo is not None and not self._kernel32.AssignProcessToJobObject(
            self._trabajo, int(proceso._handle),  # type: ignore[attr-defined]
        ):
            logger.warning("No se pudo proteger este FFmpeg contra quedar huerfano")


class GestorFFmpeg:
    def __init__(
        self, ejecutable: Sequence[str], destino: str, *, espera_min_s: float = 1.0, espera_max_s: float = 30.0,
        estable_tras_s: float = 30.0, sin_avance_s: float = 20.0, atadura: _AtarAlPadre | None = None,
    ) -> None:
        self._ejecutable = list(ejecutable)
        self._destino = destino
        self._espera_min_s = espera_min_s
        self._espera_max_s = espera_max_s
        self._estable_tras_s = estable_tras_s
        self._sin_avance_s = sin_avance_s
        self._atadura = atadura if atadura is not None else _AtarAlPadre()
        self._candado = threading.Lock()
        self._aviso = threading.Event()  # Despierta al hilo: cambio de camara o cierre.
        self._cerrando = False
        self._url: str | None = None
        self._version_url = 0
        self._estado = SIN_CONFIGURAR
        self._reintentos = 0
        self._ultimo_error: str | None = None
        self._cuadros = 0
        self._ultimo_avance = 0.0
        self._errores: collections.deque[str] = collections.deque(maxlen=20)
        self._proceso: subprocess.Popen[bytes] | None = None
        self._hilo: threading.Thread | None = None

    def iniciar(self) -> None:
        if self._hilo is None:
            self._hilo = threading.Thread(target=self._bucle, name="gestor-ffmpeg", daemon=True)
            self._hilo.start()

    def url(self) -> str | None:
        with self._candado:
            return self._url

    def cambiar_camara(self, url: str) -> None:
        with self._candado:
            if url == self._url:
                # Repetir la misma URL no corta un video que funciona; si estaba en espera, reintenta ya.
                self._aviso.set()
                return
            self._url = url
            self._version_url += 1
            self._reintentos = 0
            self._ultimo_error = None
        self._aviso.set()

    def solicitar_detencion(self) -> None:
        with self._candado:
            self._cerrando = True
        self._aviso.set()

    def detener(self) -> None:
        self.solicitar_detencion()
        if self._hilo is not None:
            self._hilo.join(timeout=_ESPERA_AL_CERRAR_S * 2 + 4)
        self._poner_estado(DETENIDO)

    def estado(self) -> dict[str, Any]:
        with self._candado:
            return {
                "estado": self._estado,
                "camara": None if self._url is None else ocultar_credenciales(self._url),
                "reintentos": self._reintentos,
                "cuadros": self._cuadros,
                "ultimo_error": self._ultimo_error,
            }

    def _poner_estado(self, estado: str) -> None:
        with self._candado:
            self._estado = estado

    def _bucle(self) -> None:
        espera = self._espera_min_s
        version_anterior = 0
        while True:
            with self._candado:
                if self._cerrando:
                    return
                url, version = self._url, self._version_url
                self._aviso.clear()
            if version != version_anterior:
                # Una camara nueva no hereda la espera acumulada por los fallos de la anterior.
                espera = self._espera_min_s
                version_anterior = version
            if url is None:
                self._aviso.wait()
                continue

            self._poner_estado(CONECTANDO)
            inicio = time.monotonic()
            error = self._ejecutar(url, version)
            with self._candado:
                if self._cerrando:
                    return
                cambio = version != self._version_url
            if cambio:
                continue

            if time.monotonic() - inicio >= self._estable_tras_s:
                espera = self._espera_min_s
            with self._candado:
                self._reintentos += 1
                self._ultimo_error = error
                self._estado = RECONECTANDO
            logger.warning("FFmpeg se detuvo: %s. Reintento en %.0f s", error, espera)
            self._aviso.wait(espera)
            espera = min(espera * 2, self._espera_max_s)

    def _ejecutar(self, url: str, version: int) -> str:
        """Corre FFmpeg hasta que falle o haya que cambiarlo; siempre lo deja cerrado. Devuelve el motivo."""
        self._errores.clear()
        with self._candado:
            self._cuadros = 0
        banderas = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            # Lista de argumentos y sin shell: la URL llega por HTTP y nunca debe interpretarse como comando.
            proceso = subprocess.Popen(  # noqa: S603
                comando_ffmpeg(self._ejecutable, url, self._destino),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=banderas,
            )
        except OSError:
            return "No se encontro FFmpeg; instalalo o revisa FFMPEG_PATH"
        self._atadura.atar(proceso)
        self._proceso = proceso
        self._ultimo_avance = time.monotonic()
        lectores = [
            threading.Thread(target=self._leer_avance, args=(proceso.stdout,), daemon=True),
            threading.Thread(target=self._leer_errores, args=(proceso.stderr,), daemon=True),
        ]
        for lector in lectores:
            lector.start()
        try:
            return self._vigilar(proceso, version)
        finally:
            self._cerrar(proceso)
            for lector in lectores:
                lector.join(timeout=1)
            self._proceso = None

    def _vigilar(self, proceso: subprocess.Popen[bytes], version: int) -> str:
        while True:
            codigo = proceso.poll()
            if codigo is not None:
                detalle = self._errores[-1] if self._errores else "sin detalle"
                return f"salio con codigo {codigo}: {detalle}"
            with self._candado:
                if self._cerrando or version != self._version_url:
                    return "cambio solicitado"
                # Un video congelado no debe figurar como activo mientras se agota el plazo de sin_avance_s.
                if self._cuadros > 0 and time.monotonic() - self._ultimo_avance < _VIDEO_RECIENTE_S:
                    self._estado = TRANSMITIENDO
                    self._ultimo_error = None
                elif self._estado == TRANSMITIENDO:
                    self._estado = RECONECTANDO
            # Si la camara deja de enviar sin cerrar la conexion, FFmpeg sigue vivo pero ya no avanza.
            if time.monotonic() - self._ultimo_avance > self._sin_avance_s:
                return f"sin video durante {self._sin_avance_s:.0f} s"
            self._aviso.wait(_CADA_CUANTO_REVISAR_S)
            with self._candado:
                # Un aviso consumido aqui no debe perderse para el bucle principal, que vuelve a mirar las versiones.
                if not self._cerrando and version == self._version_url:
                    self._aviso.clear()

    def _cerrar(self, proceso: subprocess.Popen[bytes]) -> None:
        if proceso.poll() is None and proceso.stdin is not None:
            try:
                proceso.stdin.write(b"q")  # FFmpeg cierra limpio y avisa a MediaMTX.
                proceso.stdin.close()
            except OSError:
                pass
        try:
            proceso.wait(timeout=_ESPERA_AL_CERRAR_S)
        except subprocess.TimeoutExpired:
            proceso.terminate()
            try:
                proceso.wait(timeout=_ESPERA_AL_CERRAR_S)
            except subprocess.TimeoutExpired:
                proceso.kill()
                proceso.wait()

    def _leer_avance(self, salida: IO[bytes]) -> None:
        ultimo = -1
        for linea in salida:
            texto = linea.decode("ascii", "replace").strip()
            if texto.startswith("frame="):
                try:
                    cuadros = int(texto.removeprefix("frame=").strip())
                except ValueError:
                    continue
                # -progress sigue escribiendo aunque no lleguen cuadros: solo cuenta si el numero sube.
                if cuadros > ultimo:
                    ultimo = cuadros
                    self._ultimo_avance = time.monotonic()
                    with self._candado:
                        self._cuadros = cuadros

    def _leer_errores(self, salida: IO[bytes]) -> None:
        # Hay que vaciar la tuberia siempre: si se llena, FFmpeg se bloquea.
        for linea in salida:
            texto = ocultar_credenciales(linea.decode("utf-8", "replace").strip())
            # "Last message repeated" taparia el error real, que es lo que se muestra como motivo.
            if texto and not texto.startswith("Last message repeated"):
                self._errores.append(texto[:300])


class GestorCamaras:
    """Un GestorFFmpeg por camara, cada una publicada en su propia ruta de MediaMTX."""

    def __init__(self, ejecutable: Sequence[str], base_rtsp: str, **opciones: Any) -> None:
        self._ejecutable = list(ejecutable)
        self._base_rtsp = base_rtsp.rstrip("/")
        self._opciones = opciones
        # Una sola atadura: si cada camara creara la suya, quitar camaras dejaria identificadores abiertos.
        self._atadura = _AtarAlPadre()
        self._candado = threading.Lock()
        self._motores: dict[str, GestorFFmpeg] = {}

    def urls(self) -> dict[str, str]:
        with self._candado:
            motores = dict(self._motores)
        return {camara_id: url for camara_id, motor in motores.items() if (url := motor.url()) is not None}

    def configurar(self, camara_id: str, url: str) -> None:
        with self._candado:
            motor = self._motores.get(camara_id)
            if motor is None:
                motor = GestorFFmpeg(
                    self._ejecutable, f"{self._base_rtsp}/{camara_id}", atadura=self._atadura, **self._opciones,
                )
                self._motores[camara_id] = motor
        motor.cambiar_camara(url)
        motor.iniciar()

    def quitar(self, camara_id: str) -> None:
        with self._candado:
            motor = self._motores.pop(camara_id, None)
        if motor is not None:
            motor.detener()

    def estado(self, camara_id: str) -> dict[str, Any]:
        with self._candado:
            motor = self._motores.get(camara_id)
        if motor is None:
            return {"estado": SIN_CONFIGURAR, "camara": None, "reintentos": 0, "cuadros": 0, "ultimo_error": None}
        return motor.estado()

    def motor(self, camara_id: str) -> GestorFFmpeg:
        with self._candado:
            return self._motores[camara_id]

    def detener(self) -> None:
        with self._candado:
            motores = list(self._motores.values())
        # Primero se avisa a todas: cerrarlas una por una sumaria los tiempos de espera.
        for motor in motores:
            motor.solicitar_detencion()
        for motor in motores:
            motor.detener()
