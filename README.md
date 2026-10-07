# TerraMetric

**Turn geospatial files into measurements you can trust.** TerraMetric is a small, self-hostable FastAPI application for inspecting KML and zipped Shapefiles. It calculates polygon area and line length only after projecting coordinates into metres, and presents the results on an interactive map.

## Run locally

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) for the workbench, or [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for interactive API docs. Data is stored in `data/terrametric.sqlite3`; set `TERRAMETRIC_DATA_DIR` to change the storage directory.

## API

### `POST /api/files/`

Upload a `.kml` file or a `.zip` containing one or more Shapefile layers (`.shp`, `.shx`, `.dbf`, and preferably `.prj`). Multipart field: `file`.

```bash
curl -F 'file=@survey.kml' http://127.0.0.1:8000/api/files/
```

Returns `201 Created` with the file record, feature count, detected CRS, warnings, and links to the measurements and GeoJSON endpoints.

### `GET /api/files/{id}/`

Returns file metadata, processing status, and summary counts by geometry type.

### `GET /api/files/{id}/measurements/`

Returns per-feature geometry type, original geometry, CRS, properties, and measurement. Polygon area is reported in square metres and hectares; line length in metres and kilometres. Point features and unsupported geometries remain in the response with a clear `measurement: null` and reason.

Optional query parameters: `page` (default `1`), `page_size` (default `100`, max `500`), `geometry_type`, and `search` (matches feature properties). The response includes pagination metadata and aggregate totals for the filtered result set.

### `GET /api/files/{id}/geojson/`

Returns all processed features as a GeoJSON FeatureCollection, suitable for mapping or download.

### `GET /api/files/`

Lists recent uploads. `DELETE /api/files/{id}/` deletes a record and its processed features.

## Architecture

- `app/main.py` defines the HTTP API, request validation, CORS policy, and static workbench.
- `app/processor.py` validates archives, reads KML/Shapefile layers through Fiona, transforms geometries, and calculates measurements.
- `app/store.py` persists file metadata and feature records in SQLite. The original upload is not retained.
- `app/static/` contains the single-page workbench, with an interactive Leaflet map and searchable measurement table.

The upload flow validates the extension, size, archive paths and required Shapefile companions; processes each layer; then stores a completed record and its feature measurements transactionally. Processing errors return a useful `422` response and do not leave a half-created record.

## CRS and measurement decisions

Coordinates are never measured directly in longitude/latitude degrees. The source CRS is read from the file (KML defaults to EPSG:4326). For geographic data, a local UTM CRS is estimated from the combined dataset extent, with polar stereographic fallbacks; datasets too broad for local UTM use global equal-area EPSG:6933 for area and a local projection for length. Projected sources with linear units are converted to metres. Missing CRS is reported as a warning and treated as WGS84, the practical convention for KML and many unlabeled field files.

Measurements use Shapely planar geometry operations after PyProj transformation. This gives predictable local measurements without measuring degrees, while avoiding the extra complexity of ellipsoidal geodesics for the typical survey footprint. Very large datasets crossing projection zones should be divided into regional layers for best distance accuracy.

Uploads are limited to 50 MiB. Shapefile ZIPs are inspected before reading; archive paths are never extracted to disk. Geometry is repaired with `make_valid` when possible. Invalid individual features and unsupported measurement types produce warnings or null measurements instead of stopping the whole file.

## Design decisions

FastAPI was selected for typed request handling, async-ready APIs, and generated OpenAPI docs. SQLite keeps local setup friction low while preserving results across restarts; it can be replaced by PostgreSQL/PostGIS plus object storage for multi-user deployments. Fiona provides mature GDAL-backed KML and Shapefile support. A bundled server-rendered workbench avoids a separate frontend build chain.

## What I learned

Reliable geospatial measurement depends as much on knowing the coordinate reference system and units as on the geometry operation itself. File formats also differ in how they express CRS and layers, so validation and transparent warnings are central to a useful workflow.

## Future scope

- Background processing and progress events for very large datasets
- PostGIS storage, user accounts, and team workspaces
- GeoPackage and GeoJSON input, plus CSV export
- Better projection choice for datasets spanning multiple UTM zones
- Per-feature CRS provenance and measurement-method metadata
- Deployment container, rate limiting, and configurable retention

## License

MIT. See [LICENSE](LICENSE).
