#!/usr/bin/env python3
"""CLI: выгрузка границ объекта НСПД или координат в KML / KMZ / GeoJSON.

Примеры:
    python cadastr_to_kml.py 38:06:144003:4723
    python cadastr_to_kml.py 38:06:144003:4723 --format kmz -o uchastok.kmz
    python cadastr_to_kml.py --coords coords.txt --type Polygon --name "Участок"
    python cadastr_to_kml.py            # интерактивный режим

Без аргументов скрипт спросит кадастровый номер и сохранит KML рядом.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from pathlib import Path

from core import (
    CRS_AUTO,
    SUPPORTED_CRS,
    CoreError,
    GeometryError,
    InvalidCadastralNumber,
    NspdClient,
    NspdError,
    NspdNotFound,
    __version__,
    fetch_nspd,
    format_area,
    geometry_metrics,
    geometry_to_kml,
    kml_to_kmz,
    normalize_geometry,
    parse_coordinates,
    points_to_geometry,
)

THEMATIC_LAYERS = {
    "1": "Объекты недвижимости (ЗУ, ОКС)",
    "2": "Кадастровое деление",
    "4": "Административно-территориальное деление",
    "5": "Зоны и территории (ЗОУИТ)",
    "7": "Территориальные зоны",
}

GEOMETRY_CHOICES = {"polygon": "Polygon", "line": "LineString", "points": "MultiPoint"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cadastr-to-kml",
        description="Экспорт границ объектов НСПД и координат в KML / KMZ / GeoJSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Слои НСПД:\n"
            + "\n".join(f"  {key} — {value}" for key, value in THEMATIC_LAYERS.items())
            + "\n\nПеременная окружения NSPD_INSECURE_TLS=1 отключает проверку TLS "
            "(только если сертификат НСПД не проходит проверку)."
        ),
    )
    parser.add_argument("cadnum", nargs="?", help="кадастровый номер, например 38:06:144003:4723")
    parser.add_argument("--coords", type=Path, help="файл с координатами (по паре на строку)")
    parser.add_argument("--name", default="", help="название объекта для KML")
    parser.add_argument(
        "--type",
        choices=sorted(GEOMETRY_CHOICES),
        default="polygon",
        help="тип геометрии для --coords (по умолчанию polygon)",
    )
    parser.add_argument(
        "--order",
        choices=("latlon", "lonlat", "auto"),
        default="latlon",
        help="порядок координат в --coords (по умолчанию latlon)",
    )
    parser.add_argument(
        "--crs",
        choices=SUPPORTED_CRS,
        default=CRS_AUTO,
        help="система координат входных данных (по умолчанию auto)",
    )
    parser.add_argument("--thematic-id", default="1", help="слой НСПД (по умолчанию 1)")
    parser.add_argument(
        "-f",
        "--format",
        choices=("kml", "kmz", "geojson"),
        default="kml",
        help="формат результата (по умолчанию kml)",
    )
    parser.add_argument("-o", "--output", type=Path, help="путь к итоговому файлу")
    parser.add_argument("--json", type=Path, help="сохранить исходный ответ НСПД в JSON")
    parser.add_argument("--no-cache", action="store_true", help="не использовать кэш НСПД")
    parser.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификат")
    parser.add_argument("--quiet", action="store_true", help="не печатать сводку")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _safe_filename(value: str, fallback: str = "export") -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned[:80] or fallback


def _default_output(name: str, fmt: str) -> Path:
    suffix = {"kml": ".kml", "kmz": ".kmz", "geojson": ".geojson"}[fmt]
    return Path(_safe_filename(name) + suffix)


def _build_from_coords(args: argparse.Namespace) -> tuple[str, str, dict, dict]:
    if not args.coords.is_file():
        raise FileNotFoundError(f"Файл с координатами не найден: {args.coords}")
    text = args.coords.read_text(encoding="utf-8-sig")
    points = parse_coordinates(text, order=args.order, crs=args.crs)
    geometry_type = GEOMETRY_CHOICES[args.type]
    geometry = points_to_geometry(points, geometry_type)
    name = args.name.strip() or args.coords.stem
    description = f"Точек: {len(points)}"
    return name, description, geometry, {}


def _export(
    name: str,
    description: str,
    geometry: dict,
    properties: dict,
    fmt: str,
    output: Path,
) -> Path:
    if fmt == "geojson":
        payload = {
            "type": "Feature",
            "properties": {"name": name, "description": description, **properties},
            "geometry": normalize_geometry(geometry),
        }
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output

    kml = geometry_to_kml(
        name,
        description,
        geometry,
        properties=properties,
    )
    if fmt == "kmz":
        output.write_bytes(kml_to_kmz(kml))
    else:
        output.write_bytes(kml)
    return output


def _print_summary(name: str, geometry: dict, path: Path) -> None:
    metrics = geometry_metrics(geometry)
    print(f"Объект:   {name}")
    print(f"Точек:    {metrics['points']}")
    if metrics["area_m2"]:
        print(f"Площадь:  {format_area(float(metrics['area_m2']))}")
    if metrics["perimeter_m"]:
        print(f"Периметр: {float(metrics['perimeter_m']):,.0f} м".replace(",", " "))
    print(f"Файл:     {path.resolve()}")


def _configure_stdio() -> None:
    """UTF-8 в консоли Windows, чтобы «м²» и кириллица не роняли вывод."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    cadnum = (args.cadnum or "").strip()
    if not cadnum and not args.coords:
        try:
            cadnum = input("Кадастровый номер (например, 38:06:144003:4723): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nОтменено.", file=sys.stderr)
            return 130
        if not cadnum:
            parser.error("нужен кадастровый номер или --coords")

    try:
        properties: dict = {}
        if args.coords:
            name, description, geometry, properties = _build_from_coords(args)
        else:
            client = NspdClient(verify=False) if args.insecure else None
            payload = (
                client.fetch(cadnum, args.thematic_id, use_cache=not args.no_cache)
                if client
                else fetch_nspd(cadnum, args.thematic_id, use_cache=not args.no_cache)
            )
            geometry = payload.get("geometry")
            if not geometry:
                raise GeometryError("У объекта отсутствуют координаты.")
            name = args.name.strip() or payload["cadnum"]
            description = f"Кадастровый номер: {payload['cadnum']}\nАдрес: {payload['address']}"
            properties = {"cad_num": payload["cadnum"], "address": payload["address"]}
            if payload.get("attrs"):
                properties.update(
                    {key: value for key, value in payload["attrs"].items() if value is not None}
                )
            if args.json:
                args.json.write_text(
                    json.dumps(payload["raw"], ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        output = args.output or _default_output(name, args.format)
        saved = _export(name, description, geometry, properties, args.format, output)
        if not args.quiet:
            _print_summary(name, geometry, saved)
        return 0
    except InvalidCadastralNumber as exc:
        print(f"Ошибка в кадастровом номере: {exc}", file=sys.stderr)
        return 2
    except NspdNotFound as exc:
        print(f"Не найдено: {exc}", file=sys.stderr)
        return 3
    except NspdError as exc:
        print(f"Ошибка НСПД: {exc}", file=sys.stderr)
        return 4
    except (CoreError, ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
