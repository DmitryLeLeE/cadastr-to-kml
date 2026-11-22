import io
import math
import zipfile
from xml.sax.saxutils import escape
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EARTH_RADIUS = 6378137.0


def normalize_point(x: float, y: float) -> tuple[float, float]:
    """Переводит EPSG:3857 (метры) в WGS84 (lon, lat), если координаты в метрах."""
    if abs(x) > 180 or abs(y) > 90:
        lon = (x / EARTH_RADIUS) * (180.0 / math.pi)
        lat = (2.0 * math.atan(math.exp(y / EARTH_RADIUS)) - math.pi / 2.0) * (180.0 / math.pi)
        return round(lon, 7), round(lat, 7)
    return round(x, 7), round(y, 7)


def _format_ring(ring: list) -> str:
    pts = [normalize_point(p[0], p[1]) for p in ring]
    if pts and pts[0] != pts[-1]:
        pts.append(pts[0])
    return " ".join(f"{lon},{lat},0" for lon, lat in pts)


def _polygon_to_kml(coords: list) -> str:
    outer = _format_ring(coords[0])
    inner_xml = ""
    for hole in coords[1:]:
        inner_xml += (
            f"<innerBoundaryIs><LinearRing>"
            f"<coordinates>{_format_ring(hole)}</coordinates>"
            f"</LinearRing></innerBoundaryIs>"
        )
    return (
        f"<Polygon>"
        f"<outerBoundaryIs><LinearRing><coordinates>{outer}</coordinates></LinearRing></outerBoundaryIs>"
        f"{inner_xml}"
        f"</Polygon>"
    )


def geometry_to_kml(name: str, description: str, geometry: dict) -> bytes:
    """Генерирует валидный KML (WGS84) из GeoJSON geometry."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "Point":
        lon, lat = normalize_point(coords[0], coords[1])
        geom_xml = f"<Point><coordinates>{lon},{lat},0</coordinates></Point>"
    elif gtype == "LineString":
        pts = " ".join(f"{lon},{lat},0" for lon, lat in (normalize_point(p[0], p[1]) for p in coords))
        geom_xml = f"<LineString><tessellate>1</tessellate><coordinates>{pts}</coordinates></LineString>"
    elif gtype == "Polygon":
        geom_xml = _polygon_to_kml(coords)
    elif gtype == "MultiPolygon":
        polys = "".join(_polygon_to_kml(poly) for poly in coords)
        geom_xml = f"<MultiGeometry>{polys}</MultiGeometry>"
    else:
        raise ValueError(f"Неподдерживаемый тип геометрии: {gtype}")

    kml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
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
      {geom_xml}
    </Placemark>
  </Document>
</kml>
"""
    return kml_text.encode("utf-8")


def kml_to_kmz(kml_bytes: bytes, inner_name: str = "doc.kml") -> bytes:
    """Упаковывает KML в KMZ в памяти без создания временных файлов."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, kml_bytes)
    return buf.getvalue()


def fetch_nspd(cadnum: str, thematic_id: str = "1") -> dict:
    """Запрашивает объект в API НСПД и возвращает геометрию и свойства."""
    url = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
    params = {"query": cadnum.strip(), "thematicSearchId": thematic_id}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://nspd.gov.ru/map?thematic=PKK",
        "Origin": "https://nspd.gov.ru",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=25, verify=False)
    resp.raise_for_status()
    data = resp.json()

    features = data.get("data", {}).get("features") or []
    if not features:
        raise ValueError("Объект не найден в НСПД. Проверьте кадастровый номер и слой.")

    feature = features[0]
    props = feature.get("properties", {})
    options = props.get("options", {})
    address = (
        options.get("readable_address")
        or props.get("readable_address")
        or props.get("address")
        or "Адрес не указан"
    )
    return {
        "cadnum": cadnum.strip(),
        "address": address,
        "geometry": feature.get("geometry"),
        "raw": data,
    }