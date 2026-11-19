"""API local del gestor. Se ejecuta con: python -m uvicorn camara.api:app --host 127.0.0.1 --port 8095"""

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Lock
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, field_validator

from camara.configuracion import (
    ID_POR_DEFECTO,
    PATRON_ID,
    Ajustes,
    cargar_camaras,
    guardar_camaras,
    validar_url_camara,
)
from camara.gestor import GestorCamaras

logger = logging.getLogger(__name__)
_cabecera_clave = APIKeyHeader(name="X-API-Key", auto_error=False)


class ConfigurarCamara(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="rtsp://usuario:clave@IP:554/ruta")
    camara_id: str = Field(default=ID_POR_DEFECTO, pattern=PATRON_ID, description="Tambien es su ruta en MediaMTX")

    @field_validator("url")
    @classmethod
    def url_rtsp(cls, valor: str) -> str:
        return validar_url_camara(valor)


def crear_app(ajustes: Ajustes | None = None, gestor: GestorCamaras | None = None) -> FastAPI:
    config = ajustes if ajustes is not None else Ajustes()  # type: ignore[call-arg]
    motor = gestor if gestor is not None else GestorCamaras([config.ffmpeg_path], config.mediamtx_rtsp)
    candado = Lock()

    @asynccontextmanager
    async def ciclo(_app: FastAPI) -> AsyncIterator[None]:
        for camara_id, url in cargar_camaras(config.config_path).items():
            motor.configurar(camara_id, url)
        yield
        motor.detener()

    # Sin /docs: la forma de uso esta en el README y aqui solo se atiende a programas con la clave.
    app = FastAPI(title="Gestor de camaras SmartCattle", lifespan=ciclo, docs_url=None, redoc_url=None, openapi_url=None)
    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["X-API-Key", "Content-Type"],
        )

    def exigir_clave(clave: str | None = Security(_cabecera_clave)) -> None:
        esperada = config.camera_api_key.get_secret_value().encode("utf-8")
        if clave is None or not secrets.compare_digest(clave.encode("utf-8"), esperada):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Clave invalida o ausente")

    def vista(camara_id: str) -> dict[str, Any]:
        return {
            "camara_id": camara_id,
            **motor.estado(camara_id),
            # Sin credenciales: son las direcciones de MediaMTX, no las de la camara.
            "rtsp_url": f"{config.mediamtx_rtsp.rstrip('/')}/{camara_id}",
            "hls_url": f"{config.mediamtx_hls.rstrip('/')}/{camara_id}/index.m3u8",
        }

    def guardar(camaras: dict[str, str]) -> None:
        try:
            guardar_camaras(config.config_path, camaras)
        except OSError as error:
            logger.exception("No se pudo guardar la configuracion")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="No se pudo guardar la configuracion",
            ) from error

    @app.exception_handler(RequestValidationError)
    async def validacion(_request: Request, error: RequestValidationError) -> JSONResponse:
        # La respuesta por defecto repite lo enviado, y aqui eso incluye la contrasena de la camara.
        detalle = [{"loc": [str(parte) for parte in item["loc"]], "msg": item["msg"]} for item in error.errors()]
        return JSONResponse(status_code=422, content={"detail": detalle})

    @app.post("/api/configurar-camara", dependencies=[Depends(exigir_clave)])
    def configurar_camara(datos: ConfigurarCamara) -> dict[str, Any]:
        """Crea la camara o cambia su URL. Se guarda primero: si falla, lo que esta transmitiendo sigue igual."""
        with candado:
            camaras = motor.urls()
            if datos.camara_id not in camaras and len(camaras) >= config.max_camaras:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Se alcanzo el limite de camaras")
            guardar({**camaras, datos.camara_id: datos.url})
            motor.configurar(datos.camara_id, datos.url)
        return vista(datos.camara_id)

    @app.get("/api/camaras", dependencies=[Depends(exigir_clave)])
    def listar_camaras() -> dict[str, Any]:
        return {"camaras": [vista(camara_id) for camara_id in sorted(motor.urls())], "max_camaras": config.max_camaras}

    @app.get("/api/estado-camara", dependencies=[Depends(exigir_clave)])
    def estado_camara(camara_id: Annotated[str, Query(pattern=PATRON_ID)] = ID_POR_DEFECTO) -> dict[str, Any]:
        return vista(camara_id)

    @app.delete(
        "/api/camaras/{camara_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(exigir_clave)],
    )
    def quitar_camara(camara_id: Annotated[str, Path(pattern=PATRON_ID)]) -> Response:
        with candado:
            camaras = motor.urls()
            if camara_id not in camaras:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camara no encontrada")
            del camaras[camara_id]
            guardar(camaras)
            motor.quitar(camara_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def __getattr__(nombre: str) -> FastAPI:
    # Se crea al pedirla uvicorn: importar este modulo no debe exigir la clave ni lanzar procesos.
    if nombre == "app":
        return crear_app()
    raise AttributeError(nombre)
