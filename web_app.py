from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from core import fetch_nspd, geometry_to_kml, kml_to_kmz

app = FastAPI(title="Кадастр НСПД ➔ KML / KMZ")


@app.get("/")
def index():
    return FileResponse("docs/index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/nspd")
def get_nspd_object(cadnum: str = Query(...), thematic_id: str = Query("1")):
    try:
        return fetch_nspd(cadnum, thematic_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/export")
def export_nspd(
    cadnum: str = Query(...),
    thematic_id: str = Query("1"),
    fmt: str = Query("kml", pattern="^(kml|kmz)$"),
):
    try:
        obj = fetch_nspd(cadnum, thematic_id)
        if not obj["geometry"]:
            raise ValueError("У объекта отсутствуют координаты.")
        desc = f"Кадастровый номер: {obj['cadnum']}\nАдрес: {obj['address']}"
        kml_bytes = geometry_to_kml(obj["cadnum"], desc, obj["geometry"])
        safe_name = obj["cadnum"].replace(":", "_")

        if fmt == "kmz":
            data = kml_to_kmz(kml_bytes, f"{safe_name}.kml")
            media = "application/vnd.google-earth.kmz"
        else:
            data = kml_bytes
            media = "application/vnd.google-earth.kml+xml"

        return Response(
            content=data,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.{fmt}"'},
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))