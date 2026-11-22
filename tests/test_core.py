"""Тесты ядра: проекции, KML/KMZ, метрики, разбор координат и клиент НСПД."""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET

import pytest

import core
from core import (
    GeometryError,
    InvalidCadastralNumber,
    NspdClient,
    NspdNotFound,
    NspdUnavailable,
    clean_cadastral_number,
    features_to_kml,
    geometry_metrics,
    geometry_to_kml,
    haversine_distance,
    kml_to_kmz,
    mercator_to_wgs84,
    normalize_geometry,
    normalize_point,
    parse_coordinates,
    points_to_geometry,
    polygon_area_m2,
    wgs84_to_mercator,
)

KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
MOSCOW = (37.6173, 55.7558)
SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [
            [37.6170, 55.7550],
            [37.6180, 55.7550],
            [37.6180, 55.7560],
            [37.6170, 55.7560],
            [37.6170, 55.7550],
        ]
    ],
}


class FakeResponse:
    def __init__(self, payload=None, status_code=200, invalid_json=False):
        self.payload = payload
        self.status_code = status_code
        self.invalid_json = invalid_json
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        if self.invalid_json:
            raise ValueError("not json")
        return self.payload


class FakeSession:
    def __init__(self, *responses: FakeResponse):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("Неожиданный запрос")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def nspd_payload(geometry=None, address="Тестовый адрес"):
    return {
        "data": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": geometry
                    or {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [4_187_000.0, 7_476_000.0],
                                [4_187_100.0, 7_476_000.0],
                                [4_187_100.0, 7_476_100.0],
                                [4_187_000.0, 7_476_000.0],
                            ]
                        ],
                    },
                    "properties": {
                        "options": {
                            "cad_num": "38:06:144003:4723",
                            "readable_address": address,
                        }
                    },
                }
            ],
        }
    }


# ---------------------------------------------------------------------------
# Проекции
# ---------------------------------------------------------------------------


def test_mercator_roundtrip():
    x, y = wgs84_to_mercator(*MOSCOW)
    assert abs(x) > 180  # автоопределение должно распознать метры
    lon, lat = normalize_point(x, y)
    assert lon == pytest.approx(MOSCOW[0], abs=1e-6)
    assert lat == pytest.approx(MOSCOW[1], abs=1e-6)


def test_mercator_known_origin():
    assert mercator_to_wgs84(0, 0) == (0.0, 0.0)


def test_normalize_point_wgs84_passthrough():
    assert normalize_point(37.6173, 55.7558) == (37.6173, 55.7558)


def test_normalize_point_explicit_crs():
    x, y = wgs84_to_mercator(*MOSCOW)
    assert normalize_point(x, y, "epsg3857")[0] == pytest.approx(MOSCOW[0], abs=1e-6)
    with pytest.raises(GeometryError):
        normalize_point(4_000_000, 7_000_000, "wgs84")


def test_normalize_point_out_of_range():
    with pytest.raises(GeometryError):
        normalize_point(40_000_000, 7_000_000)
    with pytest.raises(GeometryError):
        normalize_point(200, 100, "wgs84")
    with pytest.raises(GeometryError):
        normalize_point(float("nan"), 1)


def test_unknown_crs():
    with pytest.raises(GeometryError):
        normalize_point(1, 2, "epsg:4326")


# ---------------------------------------------------------------------------
# KML
# ---------------------------------------------------------------------------


def test_geometry_to_kml_polygon_is_valid_xml():
    kml = geometry_to_kml("Участок & Ко", "Описание <тест>", SQUARE).decode("utf-8")
    root = ET.fromstring(kml)
    assert root.tag == "{http://www.opengis.net/kml/2.2}kml"
    name = root.find(".//k:Document/k:name", KML_NS)
    assert name is not None and name.text == "Участок & Ко"
    rings = root.findall(".//k:Polygon/k:outerBoundaryIs/k:LinearRing", KML_NS)
    assert len(rings) == 1
    coords = rings[0].findtext("k:coordinates", namespaces=KML_NS).split()
    assert len(coords) == 5
    assert coords[0] == coords[-1]


def test_geometry_to_kml_hole_and_multipolygon():
    polygon_with_hole = {
        "type": "Polygon",
        "coordinates": [
            [
                [37.0, 55.0],
                [37.1, 55.0],
                [37.1, 55.1],
                [37.0, 55.0],
            ],
            [
                [37.02, 55.02],
                [37.03, 55.02],
                [37.03, 55.03],
                [37.02, 55.02],
            ],
        ],
    }
    root = ET.fromstring(geometry_to_kml("hole", "", polygon_with_hole))
    assert len(root.findall(".//k:innerBoundaryIs", KML_NS)) == 1

    multipolygon = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]] * 2}
    root = ET.fromstring(geometry_to_kml("multi", "", multipolygon))
    assert len(root.findall(".//k:Polygon", KML_NS)) == 2


def test_geometry_to_kml_mercator_input():
    polygon = {
        "type": "Polygon",
        "coordinates": [[list(wgs84_to_mercator(*p)) for p in SQUARE["coordinates"][0]]],
    }
    kml = geometry_to_kml("mercator", "", polygon).decode("utf-8")
    coords = ET.fromstring(kml).findtext(
        ".//k:Polygon/k:outerBoundaryIs/k:LinearRing/k:coordinates", namespaces=KML_NS
    )
    lon, lat, _ = coords.split()[0].split(",")
    assert float(lon) == pytest.approx(37.6170, abs=1e-6)
    assert float(lat) == pytest.approx(55.7550, abs=1e-6)


def test_geometry_to_kml_supported_types():
    assert "<Point>" in geometry_to_kml("p", "", {"type": "Point", "coordinates": [37, 55]}).decode()
    multipoint = geometry_to_kml(
        "mp", "", {"type": "MultiPoint", "coordinates": [[37, 55], [38, 56]]}
    ).decode()
    assert multipoint.count("<Point>") == 2
    line = geometry_to_kml(
        "l", "", {"type": "LineString", "coordinates": [[37, 55], [38, 56]]}
    ).decode()
    assert "<LineString>" in line
    multiline = geometry_to_kml(
        "ml",
        "",
        {"type": "MultiLineString", "coordinates": [[[37, 55], [38, 56]]]},
    ).decode()
    assert "<LineString>" in multiline
    collection = geometry_to_kml(
        "gc",
        "",
        {
            "type": "GeometryCollection",
            "geometries": [
                {"type": "Point", "coordinates": [37, 55]},
                {"type": "LineString", "coordinates": [[37, 55], [38, 56]]},
            ],
        },
    ).decode()
    assert "<Point>" in collection and "<LineString>" in collection


def test_geometry_errors():
    with pytest.raises(GeometryError):
        geometry_to_kml("x", "", {"type": "Circle", "coordinates": []})
    with pytest.raises(GeometryError):
        geometry_to_kml("x", "", {"type": "Polygon", "coordinates": []})
    with pytest.raises(GeometryError):
        geometry_to_kml("x", "", {"type": "LineString", "coordinates": [[1, 2]]})
    with pytest.raises(GeometryError):
        geometry_to_kml("x", "", None)
    with pytest.raises(GeometryError):
        normalize_geometry({"type": "Circle", "coordinates": []})


def test_extended_data():
    kml = geometry_to_kml(
        "x", "", SQUARE, properties={"cad_num": "38:06:144003:4723", "address": "A&B"}
    )
    root = ET.fromstring(kml)
    values = [node.text for node in root.findall(".//k:ExtendedData/k:Data/k:value", KML_NS)]
    assert "38:06:144003:4723" in values
    assert "A&B" in values


def test_features_to_kml():
    features = [
        {"type": "Feature", "properties": {"name": "A"}, "geometry": SQUARE},
        {"type": "Feature", "properties": {"name": "B"}, "geometry": SQUARE},
    ]
    kml = features_to_kml(features, document_name="Коллекция")
    root = ET.fromstring(kml)
    assert len(root.findall(".//k:Placemark", KML_NS)) == 2
    with pytest.raises(GeometryError):
        features_to_kml([])


def test_kmz_contains_doc_kml():
    kml = geometry_to_kml("x", "", SQUARE)
    kmz = kml_to_kmz(kml)
    with zipfile.ZipFile(io.BytesIO(kmz)) as zf:
        assert zf.namelist() == ["doc.kml"]
        assert zf.read("doc.kml") == kml


def test_kmz_custom_name_extension():
    kmz = kml_to_kmz(b"<kml/>", "my-object")
    with zipfile.ZipFile(io.BytesIO(kmz)) as zf:
        assert zf.namelist() == ["my-object.kml"]


def test_normalize_geometry():
    geometry = {
        "type": "Polygon",
        "coordinates": [[list(wgs84_to_mercator(*p)) for p in SQUARE["coordinates"][0]]],
    }
    normalized = normalize_geometry(geometry)
    lon, lat = normalized["coordinates"][0][0]
    assert lon == pytest.approx(37.6170, abs=1e-6)
    assert lat == pytest.approx(55.7550, abs=1e-6)


# ---------------------------------------------------------------------------
# Разбор координат
# ---------------------------------------------------------------------------


def test_parse_coordinates_latlon():
    points = parse_coordinates("55.7558, 37.6173\n55.7560; 37.6180\n", order="latlon")
    assert points == [(37.6173, 55.7558), (37.618, 55.756)]


def test_parse_coordinates_decimal_comma():
    points = parse_coordinates("55,7558; 37,6173", order="latlon")
    assert points[0] == pytest.approx((37.6173, 55.7558))


def test_parse_coordinates_comments_and_blank_lines():
    points = parse_coordinates("# заголовок\n\n55 37\n", order="latlon")
    assert points == [(37.0, 55.0)]


def test_parse_coordinates_auto_order():
    assert parse_coordinates("55.75 37.61", order="auto") == [(37.61, 55.75)]
    assert parse_coordinates("104.28 52.28", order="auto") == [(104.28, 52.28)]


def test_parse_coordinates_epsg3857_uses_xy():
    x, y = wgs84_to_mercator(*MOSCOW)
    points = parse_coordinates(f"{x}, {y}", order="latlon", crs="epsg3857")
    assert points[0][0] == pytest.approx(MOSCOW[0], abs=1e-6)
    assert points[0][1] == pytest.approx(MOSCOW[1], abs=1e-6)


def test_parse_coordinates_errors():
    with pytest.raises(ValueError):
        parse_coordinates("55.75", order="latlon")
    with pytest.raises(ValueError):
        parse_coordinates("abc def", order="latlon")
    with pytest.raises(ValueError):
        parse_coordinates("", order="latlon")
    with pytest.raises(ValueError):
        parse_coordinates("1 2", order="bad-order")


def test_points_to_geometry():
    polygon = points_to_geometry([(37, 55), (38, 55), (38, 56)], "Polygon")
    assert polygon["coordinates"][0][0] == polygon["coordinates"][0][-1]
    line = points_to_geometry([(37, 55), (38, 56)], "LineString")
    assert line["coordinates"] == [[37, 55], [38, 56]]
    multipoint = points_to_geometry([(37, 55)], "MultiPoint")
    assert multipoint["coordinates"] == [[37, 55]]
    with pytest.raises(GeometryError):
        points_to_geometry([(37, 55)], "Polygon")
    with pytest.raises(GeometryError):
        points_to_geometry([(37, 55)], "Unknown")


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------


def test_haversine_one_degree_at_equator():
    assert haversine_distance(0, 0, 1, 0) == pytest.approx(111_319.49, rel=1e-4)


def test_polygon_area_and_perimeter():
    square = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]],
        ],
    }
    assert polygon_area_m2(square["coordinates"]) == pytest.approx(1.239e6, rel=0.01)
    metrics = geometry_metrics(square)
    assert metrics["points"] == 5
    assert metrics["perimeter_m"] == pytest.approx(4_447.8, rel=0.01)
    assert metrics["area_m2"] == pytest.approx(1.239e6, rel=0.01)


def test_geometry_metrics_other_types():
    point = geometry_metrics({"type": "Point", "coordinates": [37, 55]})
    assert point == {"area_m2": 0.0, "perimeter_m": 0.0, "points": 1}
    line = geometry_metrics(
        {"type": "LineString", "coordinates": [[0, 0], [1, 0]]}
    )
    assert float(line["perimeter_m"]) == pytest.approx(111_319.49, rel=1e-4)
    multipolygon = geometry_metrics(
        {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"], SQUARE["coordinates"]]}
    )
    assert float(multipolygon["area_m2"]) > 0


def test_format_area():
    assert core.format_area(0) == "—"
    assert "м²" in core.format_area(500)
    assert "сот." in core.format_area(5000)
    assert "га" in core.format_area(500_000)


# ---------------------------------------------------------------------------
# Клиент НСПД
# ---------------------------------------------------------------------------


def test_clean_cadastral_number():
    assert clean_cadastral_number(" 38:06:144003:4723 ") == "38:06:144003:4723"
    assert clean_cadastral_number("38:06:144003") == "38:06:144003"
    for bad in ("", "abc", "38-06-144003-4723", "38:06"):
        with pytest.raises(InvalidCadastralNumber):
            clean_cadastral_number(bad)


def test_nspd_fetch_ok():
    session = FakeSession(FakeResponse(nspd_payload()))
    client = NspdClient(session=session)
    result = client.fetch("38:06:144003:4723")
    assert result["address"] == "Тестовый адрес"
    assert result["geometry"]["type"] == "Polygon"
    assert result["attrs"]["cad_num"] == "38:06:144003:4723"
    assert result["tls_verified"] is True
    assert "raw" in result
    assert session.calls[0]["params"]["query"] == "38:06:144003:4723"
    assert session.calls[0]["verify"] is True


def test_nspd_tls_fallback():
    import requests

    session = FakeSession(
        requests.exceptions.SSLError("certificate verify failed"),
        FakeResponse(nspd_payload()),
    )
    client = NspdClient(session=session)
    result = client.fetch("38:06:144003:4723")
    assert result["tls_verified"] is False
    assert len(session.calls) == 2
    assert session.calls[0]["verify"] is True
    assert session.calls[1]["verify"] is False


def test_nspd_cache():
    session = FakeSession(FakeResponse(nspd_payload()), FakeResponse(nspd_payload()))
    client = NspdClient(session=session)
    client.fetch("38:06:144003:4723")
    client.fetch("38:06:144003:4723")
    assert len(session.calls) == 1
    client.fetch("38:06:144003:4723", use_cache=False)
    assert len(session.calls) == 2


def test_nspd_not_found():
    session = FakeSession(FakeResponse({"data": {"features": []}}))
    with pytest.raises(NspdNotFound):
        NspdClient(session=session).fetch("38:06:144003:4723")

    session = FakeSession(FakeResponse({}, status_code=404))
    with pytest.raises(NspdNotFound):
        NspdClient(session=session).fetch("38:06:144003:4723")


def test_nspd_unavailable():
    session = FakeSession(FakeResponse({}, status_code=503))
    with pytest.raises(NspdUnavailable):
        NspdClient(session=session).fetch("38:06:144003:4723")

    session = FakeSession(FakeResponse(invalid_json=True))
    with pytest.raises(NspdUnavailable):
        NspdClient(session=session).fetch("38:06:144003:4723")


def test_nspd_picks_feature_with_geometry():
    payload = {
        "data": {
            "features": [
                {"type": "Feature", "properties": {}, "geometry": None},
                {"type": "Feature", "properties": {}, "geometry": SQUARE},
            ]
        }
    }
    session = FakeSession(FakeResponse(payload))
    result = NspdClient(session=session).fetch("38:06:144003:4723")
    assert result["geometry"]["type"] == "Polygon"


def test_nspd_invalid_thematic_id():
    client = NspdClient(session=FakeSession())
    with pytest.raises(ValueError):
        client.fetch("38:06:144003:4723", "abc")


def test_nspd_accepts_feature_and_list_payloads():
    payloads = [
        {"type": "Feature", "properties": {}, "geometry": SQUARE},
        {"features": [{"type": "Feature", "geometry": SQUARE}]},
        [{"geometry": SQUARE}],
    ]
    for payload in payloads:
        session = FakeSession(FakeResponse(payload))
        result = NspdClient(session=session).fetch("38:06:144003:4723")
        assert result["geometry"]["type"] == "Polygon"
