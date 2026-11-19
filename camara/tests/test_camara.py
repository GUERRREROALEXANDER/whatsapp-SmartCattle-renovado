import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camara.api import crear_app
from camara.configuracion import Ajustes, cargar_camaras, guardar_camaras, ocultar_credenciales, validar_url_camara
from camara.gestor import RECONECTANDO, TRANSMITIENDO, GestorCamaras, GestorFFmpeg, comando_ffmpeg

FALSO = [sys.executable, str(Path(__file__).with_name("falso_ffmpeg.py"))]
CLAVE = "clave-de-prueba-" + "x" * 32
CABECERA = {"X-API-Key": CLAVE}
URL = "rtsp://admin:secreto@192.168.1.50:554/live"
OTRA = "rtsp://192.168.1.51/live"
TIEMPOS = {"espera_min_s": 0.05, "espera_max_s": 0.2, "sin_avance_s": 0.5}


def esperar(condicion, segundos=10.0):
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        if condicion():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def gestor(request):
    motor = GestorFFmpeg(FALSO, "rtsp://127.0.0.1:8554/live", **TIEMPOS)
    request.addfinalizer(motor.detener)
    return motor


@pytest.fixture
def camaras(request):
    motor = GestorCamaras(FALSO, "rtsp://127.0.0.1:8554", **TIEMPOS)
    request.addfinalizer(motor.detener)
    return motor


def ajustes_de(tmp_path, **extra):
    return Ajustes(_env_file=None, camera_api_key=CLAVE, config_path=tmp_path / "config.json", **extra)


@pytest.fixture
def api(tmp_path, camaras, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "ok")
    ajustes = ajustes_de(tmp_path, max_camaras=2)
    with TestClient(crear_app(ajustes, camaras)) as cliente:
        cliente.ruta = ajustes.config_path
        yield cliente


def transmite(camaras, camara_id="live"):
    return camaras.estado(camara_id)["estado"] == TRANSMITIENDO


def test_el_comando_usa_yuv420p_recodificando():
    comando = comando_ffmpeg(["ffmpeg"], URL, "rtsp://127.0.0.1:8554/live")
    assert comando[comando.index("-pix_fmt") + 1] == "yuv420p"
    assert comando[comando.index("-c:v") + 1] == "libx264"
    assert comando[comando.index("-vf") + 1] == "scale=out_range=tv"
    assert comando[-1] == "rtsp://127.0.0.1:8554/live"


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini", "http://192.168.1.50/live", "-y", "rtsp://", "", "rtsp://a b/live",
    "rtsp://host:puerto/live", "concat:a|b", "rtsp://host/" + "x" * 500, "rtsp://host/live\n-y",
])
def test_una_url_que_no_es_rtsp_se_rechaza(url):
    with pytest.raises(ValueError):
        validar_url_camara(url)


@pytest.mark.parametrize("url", [URL, "rtsp://192.168.1.50/cam/realmonitor?channel=1&subtype=0", "rtsps://cam.local:322/a"])
def test_una_url_rtsp_se_acepta(url):
    assert validar_url_camara(f"  {url} ") == url


@pytest.mark.parametrize(("entrada", "salida"), [
    (f"fallo {URL} hoy", "fallo rtsp://***@192.168.1.50:554/live hoy"),
    ("rtsp://admin:p@ss@10.0.0.1/live", "rtsp://***@10.0.0.1/live"),
    ("rtsp://10.0.0.1/live", "rtsp://10.0.0.1/live"),
])
def test_la_contrasena_se_oculta_en_los_textos(entrada, salida):
    assert ocultar_credenciales(entrada) == salida


def test_guardar_y_cargar_conservan_las_camaras(tmp_path):
    ruta = tmp_path / "sub" / "config.json"
    guardar_camaras(ruta, {"live": URL, "corral": OTRA})
    assert cargar_camaras(ruta) == {"live": URL, "corral": OTRA}
    assert list(ruta.parent.iterdir()) == [ruta]


def test_sin_archivo_no_hay_camaras(tmp_path):
    assert cargar_camaras(tmp_path / "config.json") == {}


def test_el_formato_anterior_de_una_camara_se_sigue_leyendo(tmp_path):
    ruta = tmp_path / "config.json"
    ruta.write_text(json.dumps({"camera_url": URL}), encoding="utf-8")
    assert cargar_camaras(ruta) == {"live": URL}


@pytest.mark.parametrize("contenido", [
    "{roto", "[]", '{"otra": 1}', '{"camera_url": "file:///x"}', '{"camera_url": 5}', '{"camaras": 5}',
])
def test_un_archivo_danado_no_impide_arrancar(tmp_path, contenido):
    ruta = tmp_path / "config.json"
    ruta.write_text(contenido, encoding="utf-8")
    assert cargar_camaras(ruta) == {}


def test_una_camara_danada_no_descarta_las_demas(tmp_path):
    ruta = tmp_path / "config.json"
    datos = {"camaras": {"buena": URL, "mala": "file:///x", "../fuera": OTRA, "numero": 5}}
    ruta.write_text(json.dumps(datos), encoding="utf-8")
    assert cargar_camaras(ruta) == {"buena": URL}


def test_transmite_cuando_los_cuadros_avanzan(gestor, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "ok")
    gestor.cambiar_camara(URL)
    gestor.iniciar()
    assert esperar(lambda: gestor.estado()["estado"] == TRANSMITIENDO)
    assert gestor.estado()["ultimo_error"] is None


def test_se_reconecta_cuando_ffmpeg_termina_y_no_filtra_la_contrasena(gestor, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "falla")
    gestor.cambiar_camara(URL)
    gestor.iniciar()
    assert esperar(lambda: gestor.estado()["reintentos"] >= 3)
    estado = gestor.estado()
    assert estado["estado"] in (RECONECTANDO, "conectando")
    assert "secreto" not in json.dumps(estado)
    assert "***@" in estado["camara"]


def test_vuelve_a_transmitir_y_borra_el_error_cuando_la_camara_regresa(gestor, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "falla")
    gestor.cambiar_camara(URL)
    gestor.iniciar()
    assert esperar(lambda: gestor.estado()["reintentos"] >= 1)
    assert gestor.estado()["ultimo_error"] is not None
    monkeypatch.setenv("FALSO_MODO", "ok")
    assert esperar(lambda: gestor.estado()["estado"] == TRANSMITIENDO)
    assert esperar(lambda: gestor.estado()["ultimo_error"] is None)


def test_un_video_congelado_se_reinicia(gestor, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "cuelga")
    gestor.cambiar_camara(URL)
    gestor.iniciar()
    assert esperar(lambda: "sin video" in (gestor.estado()["ultimo_error"] or ""))
    assert gestor.estado()["reintentos"] >= 1


def test_un_video_congelado_deja_de_figurar_como_transmitiendo(monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "cuelga")
    monkeypatch.setattr("camara.gestor._VIDEO_RECIENTE_S", 0.6)
    motor = GestorFFmpeg(FALSO, "rtsp://x/live", sin_avance_s=30.0)
    motor.cambiar_camara(URL)
    motor.iniciar()
    try:
        assert esperar(lambda: motor.estado()["cuadros"] == 7)
        assert esperar(lambda: motor.estado()["estado"] == RECONECTANDO)
    finally:
        motor.detener()


def test_sin_ffmpeg_instalado_informa_y_sigue_vivo():
    motor = GestorFFmpeg(["no-existe-este-programa"], "rtsp://x/live", espera_min_s=0.05, espera_max_s=0.1)
    motor.cambiar_camara(URL)
    motor.iniciar()
    try:
        assert esperar(lambda: motor.estado()["reintentos"] >= 2)
        assert "No se encontro FFmpeg" in motor.estado()["ultimo_error"]
    finally:
        motor.detener()


def test_detener_cierra_ffmpeg(gestor, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "ok")
    gestor.cambiar_camara(URL)
    gestor.iniciar()
    assert esperar(lambda: gestor.estado()["estado"] == TRANSMITIENDO)
    proceso = gestor._proceso
    gestor.detener()
    assert proceso.poll() is not None
    assert gestor.estado()["estado"] == "detenido"


def test_cambiar_de_camara_cierra_el_proceso_anterior(gestor, monkeypatch, tmp_path):
    registro = tmp_path / "registro.txt"
    monkeypatch.setenv("FALSO_MODO", "ok")
    monkeypatch.setenv("FALSO_REGISTRO", str(registro))
    gestor.cambiar_camara(URL)
    gestor.iniciar()
    assert esperar(lambda: gestor.estado()["estado"] == TRANSMITIENDO)
    anterior = gestor._proceso
    gestor.cambiar_camara(OTRA)
    assert esperar(lambda: gestor._proceso not in (None, anterior) and gestor.estado()["estado"] == TRANSMITIENDO)
    assert anterior.poll() is not None
    assert registro.read_text(encoding="utf-8").splitlines() == [URL, OTRA]


def test_repetir_la_misma_url_no_corta_el_video(gestor, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "ok")
    gestor.cambiar_camara(URL)
    gestor.iniciar()
    assert esperar(lambda: gestor.estado()["estado"] == TRANSMITIENDO)
    proceso = gestor._proceso
    gestor.cambiar_camara(URL)
    time.sleep(0.5)
    assert gestor._proceso is proceso
    assert proceso.poll() is None


def test_cada_camara_publica_en_su_propia_ruta_y_se_cierra_sola(camaras, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "ok")
    camaras.configurar("live", URL)
    camaras.configurar("corral", OTRA)
    assert esperar(lambda: transmite(camaras, "live") and transmite(camaras, "corral"))
    assert camaras.motor("corral")._destino == "rtsp://127.0.0.1:8554/corral"
    corral, live = camaras.motor("corral")._proceso, camaras.motor("live")._proceso
    camaras.quitar("corral")
    assert corral.poll() is not None
    assert live.poll() is None
    assert camaras.urls() == {"live": URL}
    assert camaras.estado("corral")["estado"] == "sin_configurar"


def test_una_camara_caida_no_afecta_a_las_demas(camaras, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "ok")
    camaras.configurar("live", URL)
    assert esperar(lambda: transmite(camaras))
    monkeypatch.setenv("FALSO_MODO", "falla")
    camaras.configurar("corral", OTRA)
    assert esperar(lambda: camaras.estado("corral")["reintentos"] >= 2)
    assert transmite(camaras)
    assert camaras.estado("live")["reintentos"] == 0


def test_detener_cierra_todas_las_camaras(camaras, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "ok")
    camaras.configurar("live", URL)
    camaras.configurar("corral", OTRA)
    assert esperar(lambda: transmite(camaras, "live") and transmite(camaras, "corral"))
    procesos = [camaras.motor("live")._proceso, camaras.motor("corral")._proceso]
    camaras.detener()
    assert all(proceso.poll() is not None for proceso in procesos)


def test_el_endpoint_guarda_cambia_y_oculta_la_contrasena(api, camaras):
    respuesta = api.post("/api/configurar-camara", json={"url": URL}, headers=CABECERA)
    assert respuesta.status_code == 200
    assert "secreto" not in respuesta.text
    assert respuesta.json()["camara_id"] == "live"
    assert respuesta.json()["rtsp_url"] == "rtsp://127.0.0.1:8554/live"
    assert respuesta.json()["hls_url"] == "http://127.0.0.1:8888/live/index.m3u8"
    assert json.loads(api.ruta.read_text(encoding="utf-8")) == {"camaras": {"live": URL}}
    assert esperar(lambda: transmite(camaras))
    assert api.get("/api/estado-camara", headers=CABECERA).json()["camara"] == "rtsp://***@192.168.1.50:554/live"


def test_una_camara_sin_configurar_se_informa_sin_error(api):
    respuesta = api.get("/api/estado-camara", params={"camara_id": "corral"}, headers=CABECERA)
    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "sin_configurar"


def test_listar_devuelve_las_camaras_ordenadas_y_sin_contrasena(api):
    for camara_id in ("live", "corral"):
        api.post("/api/configurar-camara", json={"url": URL, "camara_id": camara_id}, headers=CABECERA)
    respuesta = api.get("/api/camaras", headers=CABECERA)
    assert [camara["camara_id"] for camara in respuesta.json()["camaras"]] == ["corral", "live"]
    assert respuesta.json()["max_camaras"] == 2
    assert "secreto" not in respuesta.text


def test_el_limite_de_camaras_se_aplica_solo_a_las_nuevas(api):
    for camara_id in ("a", "b"):
        assert api.post("/api/configurar-camara", json={"url": URL, "camara_id": camara_id}, headers=CABECERA).status_code == 200
    assert api.post("/api/configurar-camara", json={"url": URL, "camara_id": "c"}, headers=CABECERA).status_code == 409
    assert api.post("/api/configurar-camara", json={"url": OTRA, "camara_id": "a"}, headers=CABECERA).status_code == 200
    assert set(json.loads(api.ruta.read_text(encoding="utf-8"))["camaras"]) == {"a", "b"}


def test_quitar_una_camara_la_borra_del_archivo(api):
    api.post("/api/configurar-camara", json={"url": URL, "camara_id": "corral"}, headers=CABECERA)
    assert api.delete("/api/camaras/corral", headers=CABECERA).status_code == 204
    assert json.loads(api.ruta.read_text(encoding="utf-8")) == {"camaras": {}}
    assert api.delete("/api/camaras/corral", headers=CABECERA).status_code == 404


@pytest.mark.parametrize("camara_id", ["../salir", "CON ESPACIO", "", "a/b", "x" * 65])
def test_un_identificador_invalido_se_rechaza(api, camara_id):
    respuesta = api.post("/api/configurar-camara", json={"url": URL, "camara_id": camara_id}, headers=CABECERA)
    assert respuesta.status_code == 422
    assert "secreto" not in respuesta.text
    assert not api.ruta.exists()


@pytest.mark.parametrize("camara_id", ["a", "Corral-1", "camara_2", "x" * 64])
def test_un_identificador_valido_se_acepta(api, camara_id):
    respuesta = api.post("/api/configurar-camara", json={"url": URL, "camara_id": camara_id}, headers=CABECERA)
    assert respuesta.status_code == 200


@pytest.mark.parametrize("cabecera", [{}, {"X-API-Key": "incorrecta"}])
def test_sin_la_clave_no_se_cambia_nada(api, cabecera):
    assert api.post("/api/configurar-camara", json={"url": URL}, headers=cabecera).status_code == 401
    assert api.get("/api/estado-camara", headers=cabecera).status_code == 401
    assert api.get("/api/camaras", headers=cabecera).status_code == 401
    assert api.delete("/api/camaras/live", headers=cabecera).status_code == 401
    assert not api.ruta.exists()


def test_una_url_invalida_no_se_guarda_ni_se_repite(api):
    cuerpo = {"url": "http://admin:secreto@192.168.1.50/live"}
    respuesta = api.post("/api/configurar-camara", json=cuerpo, headers=CABECERA)
    assert respuesta.status_code == 422
    assert "secreto" not in respuesta.text
    assert not api.ruta.exists()


def test_un_campo_extra_se_rechaza(api):
    cuerpo = {"url": URL, "otro": 1}
    assert api.post("/api/configurar-camara", json=cuerpo, headers=CABECERA).status_code == 422


def test_al_arrancar_retoma_todas_las_camaras_guardadas(tmp_path, camaras, monkeypatch):
    monkeypatch.setenv("FALSO_MODO", "ok")
    ajustes = ajustes_de(tmp_path)
    guardar_camaras(ajustes.config_path, {"live": URL, "corral": OTRA})
    with TestClient(crear_app(ajustes, camaras)):
        assert esperar(lambda: transmite(camaras, "live") and transmite(camaras, "corral"))
    assert camaras.estado("live")["estado"] == "detenido"


def test_solo_el_origen_autorizado_recibe_permiso_cors(tmp_path, camaras):
    permitido = "http://localhost:5173"
    with TestClient(crear_app(ajustes_de(tmp_path, cors_origins=[permitido]), camaras)) as cliente:
        def preflight(origen):
            return cliente.options("/api/configurar-camara", headers={
                "Origin": origen, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-api-key",
            })
        assert preflight(permitido).headers["access-control-allow-origin"] == permitido
        assert "access-control-allow-origin" not in preflight("https://otra.example").headers


def test_sin_origenes_configurados_no_hay_cors(api):
    respuesta = api.get("/api/camaras", headers={**CABECERA, "Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in respuesta.headers


def test_cors_no_admite_comodin():
    with pytest.raises(ValueError):
        Ajustes(_env_file=None, camera_api_key=CLAVE, cors_origins=["*"])


def test_no_arranca_con_una_clave_corta():
    with pytest.raises(ValueError):
        Ajustes(_env_file=None, camera_api_key="corta")
