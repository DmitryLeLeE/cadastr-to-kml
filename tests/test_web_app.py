"""Тесты FastAPI-слоя (без обращения к реальному НСПД)."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import web_app
from core import NspdNotFound
from web_app import app

client = TestClient(app)

MERCATOR_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [
            [4_187_000.0, 7_476_000.0],
            [4_187_100.0, 7_476_000.0],
            [4_187_100.0, 7_476_100.0],
            [4_187_000.0, 7_476_000.0],
        ]
    ],
}


def fake_object(**overrides):
    payload = {
        "cadnum": "38:06:144003:4723",
        "address": "Иркутская область, тестовый адрес",
        "geometry": MERCATOR_POLYGON,
        "thematic_id": "1",
        "properties": {},
        "attrs": {},
        "raw": {"secret": "не должен отдаваться"},
    }
    payload.update(overrides)
    return payload


def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_layers():
    response = client.get("/api/layers")
    assert response.status_code == 200
    ids = [layer["id"] for layer in response.json()["layers"]]
    assert {"1", "2", "4", "5", "7"} <= set(ids)


def test_index_page():
    response = client.get("/")
    assert response.status_code == 200
    assert "Кадастр НСПД" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


def test_static_assets_mime():
    script = client.get("/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    styles = client.get("/styles.css")
    assert styles.status_code == 200
    assert "text/css" in styles.headers["content-type"]


def test_convert_polygon_to_kml():
    response = client.post(
        "/api/convert",
        json={
            "geojson": MERCATOR_POLYGON,
            "name": "Участок",
            "crs": "epsg3857",
            "format": "kml",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.google-earth.kml")
    assert "attachment" in response.headers["content-disposition"]
    text = response.content.decode("utf-8")
    assert "<Polygon>" in text
    assert "37.61" in text


def test_convert_to_kmz():
    response = client.post(
        "/api/convert",
        json={"geojson": MERCATOR_POLYGON, "name": "test", "crs": "epsg3857", "format": "kmz"},
    )
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == ["doc.kml"]


def test_convert_to_geojson_normalizes_crs():
    response = client.post(
        "/api/convert",
        json={"geojson": MERCATOR_POLYGON, "name": "test", "crs": "epsg3857", "format": "geojson"},
    )
    assert response.status_code == 200
    body = json.loads(response.content)
    lon, lat = body["geometry"]["coordinates"][0][0]
    assert lon == pytest.approx(37.6124609, abs=1e-6)
    assert lat == pytest.approx(55.5837784, abs=1e-6)


def test_convert_feature_and_feature_collection():
    feature = {"type": "Feature", "properties": {"name": "A"}, "geometry": MERCATOR_POLYGON}
    response = client.post(
        "/api/convert", json={"geojson": feature, "crs": "epsg3857", "format": "kml"}
    )
    assert response.status_code == 200

    collection = {"type": "FeatureCollection", "features": [feature, feature]}
    response = client.post(
        "/api/convert", json={"geojson": collection, "crs": "epsg3857", "format": "kml"}
    )
    assert response.status_code == 200
    assert response.content.decode().count("<Placemark>") == 2


def test_convert_validation_errors():
    response = client.post("/api/convert", json={"geojson": {"type": "Circle"}})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_geometry"

    response = client.post(
        "/api/convert",
        json={"geojson": MERCATOR_POLYGON, "crs": "epsg:4326"},
    )
    assert response.status_code == 400

    response = client.post("/api/convert", json={"geojson": "not json"})
    assert response.status_code == 400

    response = client.post("/api/convert", json={"geojson": MERCATOR_POLYGON, "format": "shp"})
    assert response.status_code == 422


def test_export_nspd(monkeypatch):
    monkeypatch.setattr(web_app, "fetch_nspd", lambda *a, **k: fake_object())
    response = client.get("/api/export", params={"cadnum": "38:06:144003:4723"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.google-earth.kml")
    assert "38_06_144003_4723" in response.headers["content-disposition"]

    response = client.get("/api/export", params={"cadnum": "38:06:144003:4723", "fmt": "kmz"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == ["doc.kml"]

    response = client.get(
        "/api/export", params={"cadnum": "38:06:144003:4723", "fmt": "geojson"}
    )
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["geometry"]["coordinates"][0][0][0] == pytest.approx(37.6124609, abs=1e-6)


def test_export_nspd_not_found(monkeypatch):
    def raise_not_found(*args, **kwargs):
        raise NspdNotFound("Объект не найден")

    monkeypatch.setattr(web_app, "fetch_nspd", raise_not_found)
    response = client.get("/api/export", params={"cadnum": "38:06:144003:9999"})
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_export_nspd_without_geometry(monkeypatch):
    monkeypatch.setattr(web_app, "fetch_nspd", lambda *a, **k: fake_object(geometry=None))
    response = client.get("/api/export", params={"cadnum": "38:06:144003:4723"})
    assert response.status_code == 400


def test_get_nspd_hides_raw(monkeypatch):
    monkeypatch.setattr(web_app, "fetch_nspd", lambda *a, **k: fake_object())
    response = client.get("/api/nspd", params={"cadnum": "38:06:144003:4723"})
    assert response.status_code == 200
    body = response.json()
    assert body["cadnum"] == "38:06:144003:4723"
    assert "raw" not in body
    assert body["geometry"]["coordinates"][0][0][0] == pytest.approx(37.6124609, abs=1e-6)


def test_invalid_cadnum_returns_422(monkeypatch):
    from core import InvalidCadastralNumber

    def raise_invalid(*args, **kwargs):
        raise InvalidCadastralNumber("Некорректный номер")

    monkeypatch.setattr(web_app, "fetch_nspd", raise_invalid)
    response = client.get("/api/nspd", params={"cadnum": "abc"})
    assert response.status_code == 422
