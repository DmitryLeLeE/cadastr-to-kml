"""Тесты CLI: конвертация координат и коды возврата."""

from __future__ import annotations

import json
import zipfile

import pytest

from cadastr_to_kml import main


def test_cli_coords_to_kml(tmp_path):
    coords = tmp_path / "coords.txt"
    coords.write_text("52.28697, 104.28066\n52.28740, 104.28190\n52.28650, 104.28220\n")
    output = tmp_path / "out.kml"
    code = main(["--coords", str(coords), "-o", str(output), "--quiet"])
    assert code == 0
    text = output.read_text(encoding="utf-8")
    assert "<kml" in text
    assert "<Polygon>" in text
    assert "104.28" in text


def test_cli_coords_epsg3857_to_geojson(tmp_path):
    coords = tmp_path / "coords3857.txt"
    coords.write_text("4187000, 7476000\n4187100, 7476000\n4187100, 7476100\n")
    output = tmp_path / "out.geojson"
    code = main(
        [
            "--coords",
            str(coords),
            "--crs",
            "epsg3857",
            "--type",
            "line",
            "-f",
            "geojson",
            "-o",
            str(output),
            "--quiet",
        ]
    )
    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    lon, lat = payload["geometry"]["coordinates"][0]
    assert lon == pytest.approx(37.6124609, abs=1e-6)
    assert lat == pytest.approx(55.5837784, abs=1e-6)


def test_cli_missing_coords_file(tmp_path, capsys):
    code = main(["--coords", str(tmp_path / "nope.txt"), "--quiet"])
    assert code == 1
    assert "не найден" in capsys.readouterr().err


def test_cli_invalid_cadnum(capsys):
    code = main(["123"])
    assert code == 2
    assert "кадастровом номере" in capsys.readouterr().err


def test_cli_kmz(tmp_path):
    coords = tmp_path / "coords.txt"
    coords.write_text("52.28697, 104.28066\n52.28740, 104.28190\n52.28650, 104.28220\n")
    output = tmp_path / "out.kmz"
    code = main(["--coords", str(coords), "-f", "kmz", "-o", str(output), "--quiet"])
    assert code == 0
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["doc.kml"]
