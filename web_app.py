"""FastAPI-бэкенд: прямой поиск в НСПД и конвертация GeoJSON в KML/KMZ.

Запуск:
    uvicorn web_app:app --reload --port 8000
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import (
    CRS_AUTO,
    SUPPORTED_CRS,
    GeometryError,
    InvalidCadastralNumber,
    NspdNotFound,
    NspdUnavailable,
    __version__,
    features_to_kml,
    fetch_nspd,
    geometry_to_kml,
    kml_to_kmz,
    normalize_geometry,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / "docs"

THEMATIC_LAYERS: dict[str, str] = {
    "1": "Объекты недвижимости (ЗУ, ОКС)",
    "2": "Кадастровое деление",
    "4": "Административно-территориальное деление",
    "5": "Зоны и территории (ЗОУИТ)",
    "7": "Территориальные зоны",
}

MEDIA_TYPES = {
    "kml": "application/vnd.google-earth.kml+xml",
    "kmz": "application/vnd.google-earth.kmz",
    "geojson": "application/geo+json",
}


class ConvertRequest(BaseModel):
    """Тело запроса POST /api/convert."""

    geojson: Any = Field(..., description="GeoJSON geometry, Feature или FeatureCollection")
    name: str = Field("Экспорт", max_length=200)
    description: str = Field("", max_length=2000)
    format: Literal["kml", "kmz", "geojson"] = "kml"
    crs: str = Field(CRS_AUTO, description="auto, wgs84 или epsg3857")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Кадастр НСПД ➔ KML / KMZ",
        version=__version__,
        description=(
            "Конвертация координат и границ объектов НСПД в KML/KMZ. "
            "Все координаты на выходе — WGS84."
        ),
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

    @app.middleware("http")
    async def static_mime_types(request: Request, call_next):
        # Windows-реестр иногда отдаёт .js/.css как text/plain, из-за чего
        # браузер отказывается исполнять скрипты (strict MIME checking).
        response = await call_next(request)
        suffix = Path(request.url.path).suffix.lower()
        if suffix == ".js":
            response.headers["content-type"] = "text/javascript; charset=utf-8"
        elif suffix == ".css":
            response.headers["content-type"] = "text/css; charset=utf-8"
        return response

    register_error_handlers(app)
    register_api_routes(app)

    if DOCS_DIR.is_dir():
        app.mount("/", StaticFiles(directory=DOCS_DIR, html=True), name="docs")
    else:  # pragma: no cover - защита от запуска без docs/
        logger.warning("Каталог %s не найден: веб-интерфейс недоступен", DOCS_DIR)

    return app


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NspdNotFound)
    async def _not_found(request: Request, exc: NspdNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc), "code": "not_found"})

    @app.exception_handler(InvalidCadastralNumber)
    async def _bad_cadnum(request: Request, exc: InvalidCadastralNumber):
        return JSONResponse(
            status_code=422, content={"detail": str(exc), "code": "invalid_cadnum"}
        )

    @app.exception_handler(GeometryError)
    async def _bad_geometry(request: Request, exc: GeometryError):
        return JSONResponse(
            status_code=400, content={"detail": str(exc), "code": "invalid_geometry"}
        )

    @app.exception_handler(NspdUnavailable)
    async def _nspd_down(request: Request, exc: NspdUnavailable):
        return JSONResponse(
            status_code=502, content={"detail": str(exc), "code": "nspd_unavailable"}
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.exception("Необработанная ошибка на %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Внутренняя ошибка сервера", "code": "internal_error"},
        )


def register_api_routes(app: FastAPI) -> None:
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "docs": DOCS_DIR.is_dir()}

    @app.get("/api/layers")
    def layers() -> dict[str, Any]:
        return {
            "layers": [
                {"id": layer_id, "title": title} for layer_id, title in THEMATIC_LAYERS.items()
            ]
        }

    @app.get("/api/nspd")
    def get_nspd_object(
        cadnum: str = Query(..., min_length=3, max_length=40, description="Кадастровый номер"),
        thematic_id: str = Query("1", pattern=r"^\d{1,4}$"),
    ) -> dict[str, Any]:
        obj = fetch_nspd(cadnum, thematic_id)
        result = {key: value for key, value in obj.items() if key != "raw"}
        if result.get("geometry"):
            result["geometry"] = normalize_geometry(result["geometry"])
        return result

    @app.get("/api/export")
    def export_nspd(
        cadnum: str = Query(..., min_length=3, max_length=40),
        thematic_id: str = Query("1", pattern=r"^\d{1,4}$"),
        fmt: Literal["kml", "kmz", "geojson"] = Query("kml"),
    ) -> Response:
        obj = fetch_nspd(cadnum, thematic_id)
        geometry = obj.get("geometry")
        if not geometry:
            raise GeometryError("У объекта отсутствуют координаты.")
        name = obj["cadnum"]
        description = f"Кадастровый номер: {name}\nАдрес: {obj['address']}"
        properties = {"cad_num": name, "address": obj["address"]}
        return build_export_response(
            name=name,
            description=description,
            geometry=geometry,
            properties=properties,
            fmt=fmt,
        )

    @app.post("/api/convert")
    def convert(request: ConvertRequest) -> Response:
        if request.crs not in SUPPORTED_CRS:
            raise GeometryError(
                f"Неизвестная система координат: {request.crs!r}. "
                f"Допустимо: {', '.join(SUPPORTED_CRS)}"
            )
        geojson = request.geojson
        if isinstance(geojson, str):
            try:
                geojson = json.loads(geojson)
            except ValueError as exc:
                raise GeometryError("Поле geojson не является валидным JSON") from exc

        if not isinstance(geojson, dict):
            raise GeometryError("Ожидается GeoJSON-объект")

        if geojson.get("type") == "FeatureCollection":
            features = [
                feature
                for feature in geojson.get("features") or []
                if isinstance(feature, dict) and feature.get("geometry")
            ]
            if not features:
                raise GeometryError("В FeatureCollection нет объектов с геометрией")
            kml = features_to_kml(features, document_name=request.name, crs=request.crs)
            normalized = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": feature.get("properties") or {},
                        "geometry": normalize_geometry(feature["geometry"], request.crs),
                    }
                    for feature in features
                ],
            }
        else:
            if geojson.get("type") == "Feature":
                geometry = geojson.get("geometry")
                props = geojson.get("properties") or {}
            else:
                geometry, props = geojson, {}
            if not geometry:
                raise GeometryError("В GeoJSON нет геометрии")
            properties = {
                key: value
                for key, value in props.items()
                if isinstance(value, str | int | float | bool)
            }
            kml = geometry_to_kml(
                request.name,
                request.description,
                geometry,
                crs=request.crs,
                properties=properties,
            )
            normalized = {
                "type": "Feature",
                "properties": {"name": request.name, **properties},
                "geometry": normalize_geometry(geometry, request.crs),
            }

        if request.format == "geojson":
            return Response(
                content=json.dumps(normalized, ensure_ascii=False).encode("utf-8"),
                media_type=MEDIA_TYPES["geojson"],
                headers=content_disposition_headers(request.name, "geojson"),
            )
        data = kml_to_kmz(kml) if request.format == "kmz" else kml
        return Response(
            content=data,
            media_type=MEDIA_TYPES[request.format],
            headers=content_disposition_headers(request.name, request.format),
        )


def build_export_response(
    *,
    name: str,
    description: str,
    geometry: dict[str, Any],
    properties: dict[str, Any],
    fmt: str,
) -> Response:
    if fmt == "geojson":
        payload = {
            "type": "Feature",
            "properties": {"name": name, "description": description, **properties},
            "geometry": normalize_geometry(geometry),
        }
        return Response(
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            media_type=MEDIA_TYPES["geojson"],
            headers=content_disposition_headers(name, "geojson"),
        )

    kml = geometry_to_kml(name, description, geometry, properties=properties)
    data = kml_to_kmz(kml) if fmt == "kmz" else kml
    return Response(
        content=data,
        media_type=MEDIA_TYPES[fmt],
        headers=content_disposition_headers(name, fmt),
    )


def safe_filename(value: str, fallback: str = "export") -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", value or "", flags=re.UNICODE).strip("._")
    return cleaned[:80] or fallback


def content_disposition_headers(filename: str, extension: str) -> dict[str, str]:
    base = safe_filename(filename)
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "export"
    ascii_name = f"{ascii_name}.{extension}"
    utf8_name = quote(f"{base}.{extension}", safe="")
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'
        )
    }


app = create_app()
