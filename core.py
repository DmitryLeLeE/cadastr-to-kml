"""Ядро проекта: конвертация геометрии в KML/KMZ и клиент НСПД.

Модуль не зависит от FastAPI и может использоваться из CLI, веб-сервера
или ноутбука. Все координаты на выходе — WGS84 (EPSG:4326).
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
import threading
import time
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__version__ = "0.2.0"

logger = logging.getLogger(__name__)

EARTH_RADIUS = 6_378_137.0  # радиус сферы EPSG:3857
MAX_MERCATOR_X = math.pi * EARTH_RADIUS  # 20037508.34
MAX_MERCATOR_Y = MAX_MERCATOR_X

KML_NS = "http://www.opengis.net/kml/2.2"
NSPD_SEARCH_URL = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
NSPD_REFERER = "https://nspd.gov.ru/map?thematic=PKK"

MAX_THEMATIC_ID_LEN = 4
COORD_PRECISION = 7  # ~1 см в градусах

CADASTRAL_NUMBER_RE = re.compile(r"^\d{1,2}(?::\d{1,7}){2,3}$")

CRS_AUTO = "auto"
CRS_WGS84 = "wgs84"
CRS_EPSG3857 = "epsg3857"
SUPPORTED_CRS = (CRS_AUTO, CRS_WGS84, CRS_EPSG3857)

SUPPORTED_GEOMETRY_TYPES = (
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
)


class CoreError(Exception):
    """Базовая ошибка библиотеки."""


class GeometryError(CoreError, ValueError):
    """Некорректная или неподдерживаемая геометрия."""


class InvalidCadastralNumber(ValueError):
    """Кадастровый номер не соответствует ожидаемому формату."""


class NspdError(CoreError):
    """Ошибка при обращении к НСПД."""


class NspdNotFound(NspdError):
    """Объект не найден в НСПД."""


class NspdUnavailable(NspdError):
    """Сервис НСПД недоступен или вернул некорректный ответ."""


# ---------------------------------------------------------------------------
# Координаты и проекции
# ---------------------------------------------------------------------------


def _check_finite(value: float, label: str = "координата") -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise GeometryError(f"{label} должна быть числом, получено: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise GeometryError(f"{label} должна быть конечным числом, получено: {value!r}")
    return number


def wgs84_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    """Переводит WGS84 (градусы) в EPSG:3857 (метры). Полезно для тестов."""
    lon = _check_finite(lon, "долгота")
    lat = _check_finite(lat, "широта")
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise GeometryError(f"Координаты вне диапазона WGS84: lon={lon}, lat={lat}")
    x = math.radians(lon) * EARTH_RADIUS
    y = math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)) * EARTH_RADIUS
    return x, y


def mercator_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Переводит EPSG:3857 (метры) в WGS84 (lon, lat)."""
    x = _check_finite(x, "X")
    y = _check_finite(y, "Y")
    if abs(x) > MAX_MERCATOR_X + 1e-6 or abs(y) > MAX_MERCATOR_Y + 1e-6:
        raise GeometryError(
            f"Координаты вне диапазона EPSG:3857: X={x:g}, Y={y:g}. "
            "Проверьте систему координат входных данных."
        )
    lon = math.degrees(x / EARTH_RADIUS)
    lat = math.degrees(2.0 * math.atan(math.exp(y / EARTH_RADIUS)) - math.pi / 2.0)
    return _round_coord(lon), _round_coord(lat)


def _round_coord(value: float) -> float:
    return round(value, COORD_PRECISION)


def normalize_point(x: float, y: float, crs: str = CRS_AUTO) -> tuple[float, float]:
    """Возвращает (lon, lat) в WGS84.

    ``crs``:
      * ``wgs84``    — x=долгота, y=широта;
      * ``epsg3857`` — x, y в метрах Web Mercator;
      * ``auto``     — эвристика: если координаты выходят за пределы
        градусной сетки, считаем их метрами.
    """
    crs = _validate_crs(crs)
    if crs == CRS_AUTO:
        crs = CRS_EPSG3857 if _looks_like_mercator(x, y) else CRS_WGS84
    if crs == CRS_EPSG3857:
        return mercator_to_wgs84(x, y)

    lon = _check_finite(x, "долгота")
    lat = _check_finite(y, "широта")
    if not (-180.0 <= lon <= 180.0):
        raise GeometryError(f"Долгота вне диапазона [-180; 180]: {lon}")
    if not (-90.0 <= lat <= 90.0):
        raise GeometryError(f"Широта вне диапазона [-90; 90]: {lat}")
    return _round_coord(lon), _round_coord(lat)


def _validate_crs(crs: str) -> str:
    value = (crs or CRS_AUTO).strip().lower()
    if value not in SUPPORTED_CRS:
        raise GeometryError(
            f"Неизвестная система координат: {crs!r}. Допустимо: {', '.join(SUPPORTED_CRS)}"
        )
    return value


def _looks_like_mercator(x: float, y: float) -> bool:
    return abs(x) > 180.0 or abs(y) > 90.0


def _iter_positions(geometry: Mapping[str, Any]) -> Iterator[Sequence[Any]]:
    """Обходит все позиции геометрии (для автоопределения СК)."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Point":
        yield coords
    elif gtype in ("MultiPoint", "LineString"):
        yield from coords or []
    elif gtype in ("MultiLineString", "Polygon"):
        for part in coords or []:
            yield from part
    elif gtype == "MultiPolygon":
        for polygon in coords or []:
            for ring in polygon:
                yield from ring
    elif gtype == "GeometryCollection":
        for child in geometry.get("geometries") or []:
            yield from _iter_positions(child)


def _resolve_crs(geometry: Mapping[str, Any], crs: str) -> str:
    crs = _validate_crs(crs)
    if crs != CRS_AUTO:
        return crs
    for position in _iter_positions(geometry):
        if not position or len(position) < 2:
            continue
        x, y = position[0], position[1]
        if (
            isinstance(x, int | float)
            and isinstance(y, int | float)
            and _looks_like_mercator(float(x), float(y))
        ):
            return CRS_EPSG3857
    return CRS_WGS84


# ---------------------------------------------------------------------------
# Геометрия → KML
# ---------------------------------------------------------------------------


def normalize_geometry(
    geometry: Mapping[str, Any], crs: str = CRS_AUTO
) -> dict[str, Any]:
    """Возвращает копию GeoJSON-геометрии в WGS84 (lon, lat[, alt])."""
    if not isinstance(geometry, Mapping):
        raise GeometryError("Геометрия должна быть объектом GeoJSON")
    gtype = geometry.get("type")
    resolved = _resolve_crs(geometry, crs)

    def convert(position: Sequence[Any]) -> list[float]:
        if not position or len(position) < 2:
            raise GeometryError("Точка должна содержать минимум X и Y")
        lon, lat = normalize_point(position[0], position[1], resolved)
        result: list[float] = [lon, lat]
        if len(position) > 2 and isinstance(position[2], int | float):
            result.append(float(position[2]))
        return result

    coords = geometry.get("coordinates")
    if gtype == "Point":
        return {"type": gtype, "coordinates": convert(coords)}
    if gtype in ("MultiPoint", "LineString"):
        return {"type": gtype, "coordinates": [convert(p) for p in coords or []]}
    if gtype in ("MultiLineString", "Polygon"):
        return {
            "type": gtype,
            "coordinates": [[convert(p) for p in part] for part in coords or []],
        }
    if gtype == "MultiPolygon":
        return {
            "type": gtype,
            "coordinates": [
                [[convert(p) for p in ring] for ring in polygon] for polygon in coords or []
            ],
        }
    if gtype == "GeometryCollection":
        return {
            "type": gtype,
            "geometries": [
                normalize_geometry(child, resolved)
                for child in geometry.get("geometries") or []
            ],
        }
    raise GeometryError(f"Неподдерживаемый тип геометрии: {gtype!r}")


def _format_ring(ring: Sequence[Sequence[float]], crs: str) -> str:
    if not ring:
        raise GeometryError("Пустое кольцо полигона")
    points = [
        normalize_point(position[0], position[1], crs)
        for position in ring
        if position and len(position) >= 2
    ]
    if len(points) != len(ring):
        raise GeometryError("Некорректная точка в кольце: ожидались минимум X и Y")
    if len(points) < 3:
        raise GeometryError(f"В кольце должно быть минимум 3 точки, получено {len(points)}")
    if points[0] != points[-1]:
        points.append(points[0])
    return " ".join(f"{lon:.{COORD_PRECISION}f},{lat:.{COORD_PRECISION}f},0" for lon, lat in points)


def _format_position(position: Sequence[float], crs: str) -> str:
    if not position or not isinstance(position, Sequence) or len(position) < 2:
        raise GeometryError("Точка должна содержать минимум X и Y")
    lon, lat = normalize_point(position[0], position[1], crs)
    return f"{lon:.{COORD_PRECISION}f},{lat:.{COORD_PRECISION}f},0"


def _format_line(coords: Sequence[Sequence[float]], crs: str) -> str:
    if len(coords) < 2:
        raise GeometryError(f"В линии должно быть минимум 2 точки, получено {len(coords)}")
    points = [normalize_point(position[0], position[1], crs) for position in coords]
    return " ".join(f"{lon:.{COORD_PRECISION}f},{lat:.{COORD_PRECISION}f},0" for lon, lat in points)


def _polygon_to_kml(coords: Sequence[Sequence[Sequence[float]]], crs: str) -> str:
    if not coords:
        raise GeometryError("Полигон без координат")
    outer = _format_ring(coords[0], crs)
    inner_xml = "".join(
        "<innerBoundaryIs><LinearRing>"
        f"<coordinates>{_format_ring(hole, crs)}</coordinates>"
        "</LinearRing></innerBoundaryIs>"
        for hole in coords[1:]
    )
    return (
        "<Polygon>"
        "<outerBoundaryIs><LinearRing>"
        f"<coordinates>{outer}</coordinates>"
        "</LinearRing></outerBoundaryIs>"
        f"{inner_xml}"
        "</Polygon>"
    )


def _geometry_to_kml(geometry: Mapping[str, Any], crs: str) -> str:
    if not isinstance(geometry, Mapping):
        raise GeometryError("Геометрия должна быть объектом GeoJSON")
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "Point":
        return f"<Point><coordinates>{_format_position(coords, crs)}</coordinates></Point>"
    if gtype == "MultiPoint":
        points = "".join(
            f"<Point><coordinates>{_format_position(position, crs)}</coordinates></Point>"
            for position in coords or []
        )
        if not points:
            raise GeometryError("MultiPoint без координат")
        return f"<MultiGeometry>{points}</MultiGeometry>"
    if gtype == "LineString":
        return (
            "<LineString><tessellate>1</tessellate>"
            f"<coordinates>{_format_line(coords or [], crs)}</coordinates></LineString>"
        )
    if gtype == "MultiLineString":
        lines = "".join(
            "<LineString><tessellate>1</tessellate>"
            f"<coordinates>{_format_line(line, crs)}</coordinates></LineString>"
            for line in coords or []
        )
        if not lines:
            raise GeometryError("MultiLineString без координат")
        return f"<MultiGeometry>{lines}</MultiGeometry>"
    if gtype == "Polygon":
        return _polygon_to_kml(coords or [], crs)
    if gtype == "MultiPolygon":
        polygons = "".join(_polygon_to_kml(polygon, crs) for polygon in coords or [])
        if not polygons:
            raise GeometryError("MultiPolygon без координат")
        return f"<MultiGeometry>{polygons}</MultiGeometry>"
    if gtype == "GeometryCollection":
        children = "".join(
            _geometry_to_kml(child, crs) for child in geometry.get("geometries") or []
        )
        if not children:
            raise GeometryError("GeometryCollection без геометрий")
        return f"<MultiGeometry>{children}</MultiGeometry>"

    raise GeometryError(
        f"Неподдерживаемый тип геометрии: {gtype!r}. "
        f"Поддерживаются: {', '.join(SUPPORTED_GEOMETRY_TYPES)}"
    )


def geometry_to_kml(
    name: str,
    description: str = "",
    geometry: Mapping[str, Any] | None = None,
    *,
    crs: str = CRS_AUTO,
    properties: Mapping[str, Any] | None = None,
) -> bytes:
    """Генерирует KML-документ (WGS84) из GeoJSON geometry.

    ``properties`` попадают в ``ExtendedData`` карточки объекта.
    """
    if geometry is None:
        raise GeometryError("Геометрия не передана")
    resolved_crs = _resolve_crs(geometry, crs)
    geom_xml = _geometry_to_kml(geometry, resolved_crs)
    extended = _extended_data_xml(properties)

    kml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="{KML_NS}">
  <Document>
    <name>{escape(name)}</name>
    <Style id="cadastrStyle">
      <LineStyle><color>ffeb6325</color><width>2.5</width></LineStyle>
      <PolyStyle><color>4deb6325</color></PolyStyle>
    </Style>
    <Placemark>
      <name>{escape(name)}</name>
      <description>{escape(description)}</description>
      <styleUrl>#cadastrStyle</styleUrl>
      {extended}
      {geom_xml}
    </Placemark>
  </Document>
</kml>
"""
    return kml_text.encode("utf-8")


def features_to_kml(
    features: Iterable[Mapping[str, Any]],
    *,
    document_name: str = "Экспорт",
    crs: str = CRS_AUTO,
) -> bytes:
    """Генерирует KML из набора GeoJSON Feature/geometry (по одному Placemark)."""
    placemarks: list[str] = []
    for index, feature in enumerate(features, start=1):
        if not isinstance(feature, Mapping):
            raise GeometryError("Элемент коллекции должен быть объектом GeoJSON")
        if feature.get("type") == "Feature":
            geometry = feature.get("geometry")
            props = feature.get("properties") or {}
        else:
            geometry, props = feature, {}
        if not geometry:
            continue
        name = str(props.get("name") or props.get("cad_num") or f"Объект {index}")
        description = str(props.get("description") or "")
        geometry_kml = _geometry_to_kml(geometry, _resolve_crs(geometry, crs))
        placemarks.append(
            "<Placemark>"
            f"<name>{escape(name)}</name>"
            f"<description>{escape(description)}</description>"
            "<styleUrl>#cadastrStyle</styleUrl>"
            f"{_extended_data_xml(props)}"
            f"{geometry_kml}"
            "</Placemark>"
        )

    if not placemarks:
        raise GeometryError("В коллекции нет геометрий для экспорта")

    kml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="{KML_NS}">
  <Document>
    <name>{escape(document_name)}</name>
    <Style id="cadastrStyle">
      <LineStyle><color>ffeb6325</color><width>2.5</width></LineStyle>
      <PolyStyle><color>4deb6325</color></PolyStyle>
    </Style>
    {''.join(placemarks)}
  </Document>
</kml>
"""
    return kml_text.encode("utf-8")


def _extended_data_xml(properties: Mapping[str, Any] | None) -> str:
    if not properties:
        return ""
    data = "".join(
        f"<Data name={quoteattr(str(key))}><value>{escape(str(value))}</value></Data>"
        for key, value in properties.items()
        if value not in (None, "")
    )
    return f"<ExtendedData>{data}</ExtendedData>" if data else ""


def kml_to_kmz(kml_bytes: bytes, inner_name: str = "doc.kml") -> bytes:
    """Упаковывает KML в KMZ в памяти, без временных файлов."""
    safe_name = os.path.basename(inner_name or "doc.kml")
    if not safe_name.lower().endswith(".kml"):
        safe_name += ".kml"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(safe_name, kml_bytes)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Разбор координат из текста (используется CLI)
# ---------------------------------------------------------------------------


def parse_coordinates(
    text: str,
    order: str = "latlon",
    crs: str = CRS_AUTO,
) -> list[tuple[float, float]]:
    """Разбирает координаты из текста: по паре на строку.

    Поддерживаются разделители: запятая, точка с запятой, пробел, табуляция.
    ``order``: ``latlon`` (широта, долгота), ``lonlat`` (долгота, широта) или
    ``auto`` — по диапазонам значений.
    """
    if order not in ("latlon", "lonlat", "auto"):
        raise ValueError("order должен быть одним из: latlon, lonlat, auto")
    crs = _validate_crs(crs)

    pairs: list[tuple[float, float]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Поддерживаем десятичную запятую: "55,7558 37,6173" → "55.7558 37.6173"
        line = re.sub(r"(?<=\d),(?=\d)", ".", line)
        parts = re.split(r"[,;\s]+", line)
        numbers = []
        for part in parts:
            if not part:
                continue
            try:
                numbers.append(float(part.replace(",", ".")))
            except ValueError as exc:
                raise ValueError(
                    f"Строка {line_number}: не удалось разобрать число {part!r}"
                ) from exc
        if len(numbers) < 2:
            raise ValueError(f"Строка {line_number}: нужны две координаты")
        a, b = numbers[0], numbers[1]
        if crs == CRS_EPSG3857:
            # Для метров порядок всегда X, Y — это ожидаемое поведение.
            x, y = a, b
            pairs.append(normalize_point(x, y, CRS_EPSG3857))
            continue
        if order == "latlon":
            lon, lat = b, a
        elif order == "lonlat":
            lon, lat = a, b
        elif abs(a) <= 90.0 and abs(b) <= 180.0:
            lon, lat = b, a
        else:
            lon, lat = a, b
        pairs.append(normalize_point(lon, lat, crs))

    if not pairs:
        raise ValueError("Не найдено ни одной пары координат")
    return pairs


def points_to_geometry(points: Sequence[Sequence[float]], geometry_type: str = "Polygon") -> dict:
    """Собирает GeoJSON geometry из списка (lon, lat)."""
    if geometry_type == "Polygon":
        ring = [list(point) for point in points]
        if len(ring) < 3:
            raise GeometryError("Для полигона нужно минимум 3 точки")
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        return {"type": "Polygon", "coordinates": [ring]}
    if geometry_type == "LineString":
        if len(points) < 2:
            raise GeometryError("Для линии нужно минимум 2 точки")
        return {"type": "LineString", "coordinates": [list(point) for point in points]}
    if geometry_type == "MultiPoint":
        return {"type": "MultiPoint", "coordinates": [list(point) for point in points]}
    raise GeometryError(f"Неизвестный тип геометрии: {geometry_type!r}")


# ---------------------------------------------------------------------------
# Метрики (площадь, периметр, центр)
# ---------------------------------------------------------------------------


def haversine_distance(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Расстояние между точками в метрах (сферическая модель)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS * math.asin(min(1.0, math.sqrt(a)))


def line_length_m(points: Sequence[Sequence[float]]) -> float:
    return sum(
        haversine_distance(p1[0], p1[1], p2[0], p2[1]) for p1, p2 in zip(points, points[1:], strict=False)
    )


def ring_area_m2(ring: Sequence[Sequence[float]]) -> float:
    """Площадь кольца на сфере (в м²)."""
    if len(ring) < 3:
        return 0.0
    points = [normalize_point(p[0], p[1], CRS_AUTO) for p in ring]
    if points[0] != points[-1]:
        points.append(points[0])
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(points, points[1:], strict=False):
        total += math.radians(lon2 - lon1) * (2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    return abs(total * EARTH_RADIUS**2 / 2.0)


def polygon_area_m2(polygon: Sequence[Sequence[Sequence[float]]]) -> float:
    """Площадь полигона с учётом отверстий."""
    if not polygon:
        return 0.0
    area = ring_area_m2(polygon[0])
    area -= sum(ring_area_m2(hole) for hole in polygon[1:])
    return max(0.0, area)


def geometry_metrics(geometry: Mapping[str, Any]) -> dict[str, float | int]:
    """Возвращает площадь (м²), периметр/длину (м) и число точек."""
    metrics: dict[str, float | int] = {"area_m2": 0.0, "perimeter_m": 0.0, "points": 0}
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "Point":
        metrics["points"] = 1
    elif gtype == "MultiPoint":
        metrics["points"] = len(coords or [])
    elif gtype == "LineString":
        points = [normalize_point(p[0], p[1], CRS_AUTO) for p in coords or []]
        metrics["points"] = len(points)
        metrics["perimeter_m"] = line_length_m(points)
    elif gtype == "MultiLineString":
        metrics["points"] = sum(len(line) for line in coords or [])
        metrics["perimeter_m"] = sum(
            line_length_m([normalize_point(p[0], p[1], CRS_AUTO) for p in line])
            for line in coords or []
        )
    elif gtype == "Polygon":
        metrics["points"] = sum(len(ring) for ring in coords or [])
        metrics["area_m2"] = polygon_area_m2(coords or [])
        metrics["perimeter_m"] = sum(
            line_length_m([normalize_point(p[0], p[1], CRS_AUTO) for p in ring])
            for ring in coords or []
        )
    elif gtype == "MultiPolygon":
        metrics["points"] = sum(len(ring) for poly in coords or [] for ring in poly)
        metrics["area_m2"] = sum(polygon_area_m2(poly) for poly in coords or [])
        metrics["perimeter_m"] = sum(
            line_length_m([normalize_point(p[0], p[1], CRS_AUTO) for p in ring])
            for poly in coords or []
            for ring in poly
        )
    return metrics


def format_area(area_m2: float) -> str:
    """Человекочитаемая площадь: м², сотки, гектары."""
    if area_m2 <= 0:
        return "—"
    if area_m2 < 1_000:
        return f"{area_m2:.0f} м²"
    if area_m2 < 100_000:
        return f"{area_m2:,.0f} м² ({area_m2 / 100:.1f} сот.)".replace(",", " ")
    return f"{area_m2:,.0f} м² ({area_m2 / 10_000:.2f} га)".replace(",", " ")


# ---------------------------------------------------------------------------
# Клиент НСПД
# ---------------------------------------------------------------------------


def clean_cadastral_number(value: str) -> str:
    """Нормализует и валидирует кадастровый номер."""
    if not isinstance(value, str):
        raise InvalidCadastralNumber("Кадастровый номер должен быть строкой")
    cleaned = re.sub(r"\s+", "", value.strip())
    if not cleaned:
        raise InvalidCadastralNumber("Кадастровый номер не указан")
    if not CADASTRAL_NUMBER_RE.match(cleaned):
        raise InvalidCadastralNumber(
            f"Некорректный кадастровый номер: {value!r}. "
            "Ожидается формат вида 38:06:144003:4723"
        )
    return cleaned


def _first_feature(payload: Any) -> Mapping[str, Any] | None:
    """Достаёт первый объект из разных вариантов ответа НСПД."""
    if isinstance(payload, list):
        for item in payload:
            feature = _first_feature(item)
            if feature:
                return feature
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("type") == "Feature" and payload.get("geometry"):
        return payload
    if isinstance(payload.get("geometry"), Mapping):
        return payload
    features = payload.get("features")
    if isinstance(features, list) and features:
        fallback: Mapping[str, Any] | None = None
        for feature in features:
            if not isinstance(feature, Mapping):
                continue
            fallback = fallback or feature
            if _extract_geometry(feature):
                return feature
        return fallback
    for key in ("data", "result", "results", "items"):
        if key in payload:
            feature = _first_feature(payload[key])
            if feature:
                return feature
    return None


def _extract_geometry(feature: Mapping[str, Any]) -> Mapping[str, Any] | None:
    geometry = feature.get("geometry")
    if isinstance(geometry, Mapping):
        return geometry
    props = feature.get("properties")
    if isinstance(props, Mapping):
        for key in ("geometry", "geom", "geodata"):
            candidate = props.get(key)
            if isinstance(candidate, Mapping) and candidate.get("type"):
                return candidate
    return None


def _display_address(props: Mapping[str, Any]) -> str:
    options = props.get("options") if isinstance(props.get("options"), Mapping) else {}
    for source in (options, props):
        for key in ("readable_address", "address", "address_full", "address_readable"):
            value = source.get(key) if isinstance(source, Mapping) else None
            if value:
                return str(value)
    return "Адрес не указан"


class NspdClient:
    """HTTP-клиент НСПД с ретраями и TTL-кэшем."""

    def __init__(
        self,
        *,
        base_url: str = NSPD_SEARCH_URL,
        timeout: tuple[float, float] = (5.0, 25.0),
        retries: int = 2,
        cache_ttl: float = 300.0,
        verify: bool | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

        if verify is None:
            verify = os.getenv("NSPD_INSECURE_TLS", "").strip().lower() not in {"1", "true", "yes"}
        self.verify = bool(verify)
        if not self.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            logger.warning("Проверка TLS-сертификата НСПД отключена (NSPD_INSECURE_TLS)")

        self.session = session or self._build_session(retries)

    @staticmethod
    def _build_session(retries: int) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=max(0, retries),
            connect=retries,
            read=retries,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 cadastr-to-kml/0.2"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
                "Referer": NSPD_REFERER,
                "Origin": "https://nspd.gov.ru",
            }
        )
        return session

    def fetch(self, cadnum: str, thematic_id: str = "1", *, use_cache: bool = True) -> dict[str, Any]:
        cadnum = clean_cadastral_number(cadnum)
        thematic_id = str(thematic_id or "1").strip()
        if not thematic_id.isdigit() or len(thematic_id) > MAX_THEMATIC_ID_LEN:
            raise ValueError("thematic_id должен быть числом, например 1")

        cache_key = (cadnum, thematic_id)
        now = time.monotonic()
        if use_cache:
            with self._lock:
                cached = self._cache.get(cache_key)
                if cached and (now - cached[0]) < self.cache_ttl:
                    logger.debug("Кэш НСПД: %s", cadnum)
                    return cached[1]

        params = {"query": cadnum, "thematicSearchId": thematic_id}
        tls_verified = self.verify
        try:
            response = self.session.get(
                self.base_url,
                params=params,
                timeout=self.timeout,
                verify=self.verify,
            )
        except requests.exceptions.SSLError as exc:
            if not self.verify:
                raise NspdUnavailable(
                    "Не удалось установить TLS-соединение с НСПД"
                ) from exc
            # НСПД использует сертификат российского УЦ, которого нет в certifi.
            # Пробуем ещё раз без проверки цепочки, но явно предупреждаем.
            logger.warning(
                "Сертификат НСПД не прошёл проверку цепочки — повторяю без проверки TLS. "
                "Чтобы отключить проверку заранее, задайте NSPD_INSECURE_TLS=1."
            )
            logger.debug("Детали TLS-ошибки: %s", exc)
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            tls_verified = False
            try:
                response = self.session.get(
                    self.base_url,
                    params=params,
                    timeout=self.timeout,
                    verify=False,
                )
            except requests.RequestException as retry_exc:
                raise NspdUnavailable(f"Сервис НСПД недоступен: {retry_exc}") from retry_exc
        except requests.RequestException as exc:
            raise NspdUnavailable(f"Сервис НСПД недоступен: {exc}") from exc

        if response.status_code == 404:
            raise NspdNotFound(f"Объект {cadnum} не найден в НСПД")
        if response.status_code >= 400:
            raise NspdUnavailable(
                f"НСПД вернул HTTP {response.status_code}. Попробуйте позже."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise NspdUnavailable("НСПД вернул ответ, который не является JSON") from exc

        feature = _first_feature(payload)
        if feature is None:
            raise NspdNotFound("Объект не найден в НСПД. Проверьте кадастровый номер и слой.")

        props = feature.get("properties") if isinstance(feature.get("properties"), Mapping) else {}
        options = props.get("options") if isinstance(props.get("options"), Mapping) else {}
        geometry = _extract_geometry(feature)

        result: dict[str, Any] = {
            "cadnum": cadnum,
            "address": _display_address(props),
            "geometry": dict(geometry) if geometry else None,
            "thematic_id": thematic_id,
            "tls_verified": tls_verified,
            "properties": dict(props),
            "attrs": {
                key: options.get(key) if options.get(key) is not None else props.get(key)
                for key in (
                    "cad_num",
                    "category",
                    "categoryName",
                    "area",
                    "area_value",
                    "cost_value",
                    "status",
                    "common_data_status",
                )
                if (options.get(key) if options.get(key) is not None else props.get(key))
                is not None
            },
            "raw": payload,
        }

        if use_cache:
            with self._lock:
                if len(self._cache) > 256:
                    self._cache.clear()
                self._cache[cache_key] = (now, result)
        return result


_default_client: NspdClient | None = None
_default_client_lock = threading.Lock()


def get_nspd_client() -> NspdClient:
    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = NspdClient()
        return _default_client


def fetch_nspd(cadnum: str, thematic_id: str = "1", **kwargs: Any) -> dict[str, Any]:
    """Запрашивает объект в НСПД (совместимая обёртка над NspdClient)."""
    session = kwargs.pop("session", None)
    use_cache = kwargs.pop("use_cache", True)
    if kwargs:
        raise TypeError(f"Неизвестные аргументы: {', '.join(sorted(kwargs))}")
    client = NspdClient(session=session) if session is not None else get_nspd_client()
    return client.fetch(cadnum, thematic_id, use_cache=use_cache)
