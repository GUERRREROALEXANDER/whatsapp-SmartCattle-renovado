"""Ajustes del servicio y la URL de la camara guardada en config.json.

config.json guarda la contrasena de la camara en texto plano, porque FFmpeg la necesita para abrirla. Por eso esta en
.gitignore, y la URL completa nunca sale en respuestas ni en registros: todo pasa por ocultar_credenciales.
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

MAX_LARGO_URL = 500
ID_POR_DEFECTO = "live"
# La misma regla del backend y del servicio de IA: las alertas solo llegan si la camara conserva su ID del backend.
PATRON_ID = r"^[A-Za-z0-9_-]{1,64}$"
_CREDENCIALES = re.compile(r"(rtsps?://)[^/\s]*@", re.IGNORECASE)


class Ajustes(BaseSettings):
    # extra="ignore": este .env tambien lo usa el servicio de WhatsApp.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Sin valor por defecto: este endpoint cambia a donde apunta FFmpeg, asi que no arranca abierto.
    camera_api_key: SecretStr = Field(min_length=32)
    ffmpeg_path: str = "ffmpeg"
    mediamtx_rtsp: str = "rtsp://127.0.0.1:8554"
    mediamtx_hls: str = "http://127.0.0.1:8888"
    config_path: Path = Path("config.json")
    # Cada camara recodifica video, asi que el limite protege la CPU del PC.
    max_camaras: int = Field(default=8, ge=1, le=32)
    cors_origins: list[str] = []

    @field_validator("cors_origins")
    @classmethod
    def sin_comodin(cls, origenes: list[str]) -> list[str]:
        # Con "*", cualquier pagina abierta en este PC podria intentar usar la API.
        if "*" in origenes:
            raise ValueError("CORS_ORIGINS debe listar origenes concretos")
        return origenes


def validar_url_camara(url: str) -> str:
    """Solo RTSP. FFmpeg tambien abre archivos y otros protocolos, y este valor llega por HTTP."""
    limpia = url.strip()
    if not limpia or len(limpia) > MAX_LARGO_URL or any(c.isspace() or ord(c) < 32 for c in limpia):
        raise ValueError("La URL de la camara no es valida")
    try:
        partes = urlsplit(limpia)
        host = partes.hostname
        _ = partes.port  # Un puerto invalido lanza ValueError al leerlo.
    except ValueError as error:
        raise ValueError("La URL de la camara no es valida") from error
    if partes.scheme.lower() not in ("rtsp", "rtsps") or not host:
        raise ValueError("La URL debe ser rtsp://IP:puerto/ruta; si la clave tiene simbolos, codificalos")
    return limpia


def ocultar_credenciales(texto: str) -> str:
    return _CREDENCIALES.sub(r"\1***@", texto)


def cargar_camaras(ruta: Path) -> dict[str, str]:
    """Las camaras guardadas. Un archivo danado no debe impedir arrancar: se pueden volver a configurar."""
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("No se pudo leer %s; hay que configurar las camaras otra vez", ruta)
        return {}
    # {"camera_url": ...} es el formato anterior, de cuando habia una sola camara.
    crudas = datos.get("camaras", {ID_POR_DEFECTO: datos.get("camera_url")}) if isinstance(datos, dict) else None
    if not isinstance(crudas, dict):
        logger.warning("%s no tiene el formato esperado; hay que configurar las camaras otra vez", ruta)
        return {}

    camaras: dict[str, str] = {}
    for camara_id, url in crudas.items():
        try:
            if not isinstance(url, str) or not re.fullmatch(PATRON_ID, camara_id):
                raise ValueError(camara_id)
            camaras[camara_id] = validar_url_camara(url)
        except ValueError:
            logger.warning("Se ignoro una camara no valida guardada en %s", ruta)
    return camaras


def guardar_camaras(ruta: Path, camaras: dict[str, str]) -> None:
    """Escribe en un temporal y lo reemplaza de una vez: un corte a la mitad no deja el archivo danado."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(dir=ruta.parent, prefix=f"{ruta.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            json.dump({"camaras": camaras}, archivo)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(temporal, ruta)
    except BaseException:
        Path(temporal).unlink(missing_ok=True)
        raise
