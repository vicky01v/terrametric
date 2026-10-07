from __future__ import annotations

import math
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

from app import store
from app.processor import MAX_UPLOAD_BYTES, ProcessingError, process_upload

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    yield


app = FastAPI(
    title="TerraMetric API", version="1.0.0",
    description="CRS-aware polygon area and line length measurement for KML and zipped Shapefiles.",
    lifespan=lifespan,
)
origins = [origin.strip() for origin in os.environ.get("TERRAMETRIC_CORS_ORIGINS", "").split(",") if origin.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"])


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(STATIC / "index.html")


@app.post("/api/files/", status_code=201, tags=["Files"])
async def upload_file(file: UploadFile = File(...)):
    raw_name = (file.filename or "upload").replace("\\", "/").split("/")[-1]
    filename = re.sub(r"[^\w.() -]", "_", raw_name, flags=re.UNICODE).strip(" .") or "upload"
    if Path(filename).suffix.lower() not in {".zip", ".kml"}:
        raise HTTPException(415, "Unsupported format. Upload a .kml or .zip Shapefile archive.")
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Files must be 50 MiB or smaller.")
    try:
        result = process_upload(filename, payload)
        return store.create(filename, result)
    except ProcessingError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        # Keep internal details in server logs; return a safe, actionable response.
        import logging
        logging.getLogger(__name__).exception("Upload processing failed")
        raise HTTPException(500, "The upload could not be processed. Check that its contents are valid and try again.") from exc


@app.get("/api/files/", tags=["Files"])
async def list_uploads(limit: int = Query(20, ge=1, le=100)):
    return {"files": store.list_files(limit)}


@app.get("/api/files/{file_id}/", tags=["Files"])
async def file_info(file_id: str):
    record = store.get_file(file_id)
    if not record:
        raise HTTPException(404, "File not found.")
    return record


@app.get("/api/files/{file_id}/measurements/", tags=["Measurements"])
async def measurements(
    file_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    geometry_type: str | None = None,
    search: str | None = Query(None, max_length=120),
):
    if not store.get_file(file_id):
        raise HTTPException(404, "File not found.")
    features = store.filtered_features(file_id, geometry_type, search)
    totals = {"area_m2": 0.0, "length_m": 0.0}
    counts = {"area_features": 0, "length_features": 0, "unmeasured_features": 0}
    for feature in features:
        measurement = feature["measurement"]
        if measurement and measurement["kind"] == "area":
            totals["area_m2"] += measurement["value"]
            counts["area_features"] += 1
        elif measurement and measurement["kind"] == "length":
            totals["length_m"] += measurement["value"]
            counts["length_features"] += 1
        else:
            counts["unmeasured_features"] += 1
    start = (page - 1) * page_size
    return {
        "features": features[start:start + page_size],
        "pagination": {"page": page, "page_size": page_size, "total": len(features), "pages": math.ceil(len(features) / page_size)},
        "totals": {**totals, "area_hectares": totals["area_m2"] / 10000, "length_km": totals["length_m"] / 1000, **counts},
    }


@app.get("/api/files/{file_id}/geojson/", tags=["Measurements"])
async def geojson(file_id: str, download: bool = False):
    record = store.get_file(file_id)
    if not record:
        raise HTTPException(404, "File not found.")
    features = store.filtered_features(file_id)
    source = CRS.from_user_input(record["crs"])
    to_wgs84 = Transformer.from_crs(source, CRS.from_epsg(4326), always_xy=True).transform
    collection = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": f["id"],
         "geometry": mapping(transform(to_wgs84, shape(f["geometry"]))) if f["geometry"] else None,
         "properties": {**f["properties"], "_feature_id": f["id"], "_layer": f["layer"],
                        "_geometry_type": f["geometry_type"], "_measurement": f["measurement"]}}
        for f in features
    ]}
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(record["filename"]).stem) or "measurements"
    headers = {"Content-Disposition": f'attachment; filename="{safe_stem}.geojson"'} if download else None
    return JSONResponse(collection, headers=headers)


@app.delete("/api/files/{file_id}/", status_code=204, tags=["Files"])
async def delete_upload(file_id: str):
    if not store.delete_file(file_id):
        raise HTTPException(404, "File not found.")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
