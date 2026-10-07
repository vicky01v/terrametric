"""Safe ingestion and CRS-aware measurements for KML and zipped Shapefiles."""
from __future__ import annotations

import json
import math
import tempfile
import zipfile
from collections import Counter
from pathlib import PurePosixPath
from typing import Any

import fiona
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely import make_valid
from shapely.geometry import shape, mapping
from shapely.ops import transform

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_FEATURES = 100_000


class ProcessingError(ValueError):
    pass


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _select_crs(bounds: tuple[float, float, float, float], source: CRS, for_area: bool = False) -> CRS:
    if source.is_projected:
        return source
    minx, miny, maxx, maxy = bounds
    lat = (miny + maxy) / 2
    lon = (minx + maxx) / 2
    if maxy > 84:
        return CRS.from_epsg(3413)
    if miny < -80:
        return CRS.from_epsg(3031)
    # A regional UTM estimate is accurate for ordinary survey footprints.
    if maxx - minx <= 12 and maxy - miny <= 12 and -80 <= lat <= 84:
        try:
            return CRS.from_user_input(source.estimate_utm_crs(datum_name="WGS 84") if hasattr(source, "estimate_utm_crs") else _utm_for(lon, lat))
        except Exception:
            return _utm_for(lon, lat)
    # A broad area needs an equal-area projection. Distances still use a local UTM.
    return CRS.from_epsg(6933) if for_area else _utm_for(lon, lat)


def _utm_for(lon: float, lat: float) -> CRS:
    zone = min(60, max(1, int((lon + 180) // 6) + 1))
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


def _read_layers(filename: str, payload: bytes) -> list[tuple[str, list[dict[str, Any]], CRS | None]]:
    suffix = filename.lower().rsplit(".", 1)[-1]
    sources: list[tuple[str, str]] = []
    temp = tempfile.TemporaryDirectory(prefix="terrametric-")
    try:
        if suffix == "kml":
            path = f"{temp.name}/upload.kml"
            with open(path, "wb") as stream:
                stream.write(payload)
            sources.append(("KML", path))
        elif suffix == "zip":
            try:
                archive = zipfile.ZipFile(__import__("io").BytesIO(payload))
            except zipfile.BadZipFile as exc:
                raise ProcessingError("This ZIP archive is damaged or unreadable.") from exc
            members = archive.infolist()
            if len(members) > 500:
                raise ProcessingError("The archive contains too many files (maximum 500).")
            names: dict[str, str] = {}
            total = 0
            for member in members:
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in member.filename or ":" in member.filename or "\x00" in member.filename:
                    raise ProcessingError("The archive contains an unsafe file path.")
                total += member.file_size
                if total > MAX_UPLOAD_BYTES * 5:
                    raise ProcessingError("The uncompressed archive exceeds the 250 MiB limit.")
                if not member.is_dir():
                    names[member.filename.lower()] = member.filename
            shp_names = [v for k, v in names.items() if k.endswith(".shp")]
            if not shp_names:
                raise ProcessingError("No .shp layer was found in the ZIP archive.")
            if len(shp_names) > 20:
                raise ProcessingError("The archive contains too many Shapefile layers (maximum 20).")
            for shp in shp_names:
                stem = shp[:-4].lower()
                if stem + ".dbf" not in names or stem + ".shx" not in names:
                    raise ProcessingError(f"Layer '{PurePosixPath(shp).name}' is missing its .dbf or .shx companion.")
            for member in members:
                if member.is_dir():
                    continue
                target = PurePosixPath(member.filename)
                local = f"{temp.name}/{target}"
                import os
                os.makedirs(os.path.dirname(local), exist_ok=True)
                with archive.open(member) as src, open(local, "wb") as dst:
                    dst.write(src.read())
            sources = [(PurePosixPath(path).stem, f"{temp.name}/{path}") for path in shp_names]
        else:
            raise ProcessingError("Please upload a .kml file or a .zip containing Shapefile layers.")

        layers: list[tuple[str, list[dict[str, Any]], CRS | None]] = []
        feature_total = 0
        for layer_name, path in sources:
            try:
                open_options = {"driver": "KML"} if suffix == "kml" else {}
                with fiona.open(path, **open_options) as collection:
                    records = []
                    for feature in collection:
                        geometry_data = feature.get("geometry")
                        geom = shape(geometry_data) if geometry_data else None
                        props = {str(k): _json_value(v) for k, v in (feature.get("properties") or {}).items()}
                        records.append({"geometry": geom, "properties": props})
                        if feature_total + len(records) > MAX_FEATURES:
                            raise ProcessingError(f"The upload exceeds the {MAX_FEATURES:,} feature limit.")
                    feature_total += len(records)
                    raw_crs = collection.crs_wkt or collection.crs
                    try:
                        crs = CRS.from_user_input(raw_crs) if raw_crs else None
                    except CRSError:
                        crs = None
                    layers.append((layer_name, records, crs))
            except ProcessingError:
                raise
            except Exception as exc:
                raise ProcessingError(f"Could not read layer '{layer_name}': {exc}") from exc
        return layers
    finally:
        # Keep temp alive until geometries have been materialized; all are in memory now.
        temp.cleanup()


def process_upload(filename: str, payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ProcessingError("Files must be 50 MiB or smaller.")
    layers = _read_layers(filename, payload)
    if not layers:
        raise ProcessingError("No readable layers were found in the upload.")
    source_crs = next((crs for _, _, crs in layers if crs), None)
    warnings: list[str] = []
    if source_crs is None:
        source_crs = CRS.from_epsg(4326)
        warnings.append("The file has no declared CRS; coordinates were assumed to be WGS 84 (EPSG:4326). Confirm this matches the source data.")
    elif any(crs and not crs.equals(source_crs) for _, _, crs in layers):
        warnings.append("Layers declare different coordinate reference systems. All layers were interpreted using the first declared CRS.")

    valid_geometries = [row["geometry"] for _, records, _ in layers for row in records if row["geometry"] and not row["geometry"].is_empty]
    if not valid_geometries:
        raise ProcessingError("No non-empty geometries were found in the uploaded file.")
    fixed_geometries = [make_valid(g) if not g.is_valid else g for g in valid_geometries]
    bounds = [geometry.bounds for geometry in fixed_geometries]
    combined_bounds = (min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds))
    length_crs = _select_crs(combined_bounds, source_crs, False)
    area_crs = _select_crs(combined_bounds, source_crs, True)
    to_length = Transformer.from_crs(source_crs, length_crs, always_xy=True).transform
    to_area = Transformer.from_crs(source_crs, area_crs, always_xy=True).transform
    # Projected source CRSs may use feet or another linear unit; Shapely itself is unit agnostic.
    source_unit_factor = float(source_crs.axis_info[0].unit_conversion_factor or 1.0) if source_crs.is_projected and source_crs.axis_info else 1.0
    source_label = source_crs.to_string()

    features: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for layer_name, records, layer_crs in layers:
        layer_crs = layer_crs or source_crs
        if not layer_crs.equals(source_crs):
            layer_to_source = Transformer.from_crs(layer_crs, source_crs, always_xy=True).transform
        else:
            layer_to_source = None
        for record in records:
            original = record["geometry"]
            if original is not None and layer_to_source:
                original = transform(layer_to_source, original)
            geom = make_valid(original) if original and not original.is_valid else original
            if geom is None:
                geom_type = "Unknown"
                geometry = None
            else:
                geom_type = geom.geom_type
                geometry = mapping(geom)
            counts[geom_type] += 1
            measurement = None
            reason = None
            if geom is None or geom.is_empty:
                reason = "No geometry"
            elif geom_type in ("Polygon", "MultiPolygon"):
                area_m2 = abs(transform(to_area, geom).area) * source_unit_factor**2
                measurement = {"kind": "area", "value": area_m2, "unit": "m²", "hectares": area_m2 / 10000}
            elif geom_type in ("LineString", "MultiLineString", "LinearRing"):
                length_m = transform(to_length, geom).length * source_unit_factor
                measurement = {"kind": "length", "value": length_m, "unit": "m", "kilometres": length_m / 1000}
            elif geom_type in ("Point", "MultiPoint"):
                reason = "Point geometry; no measurement is required."
            else:
                reason = f"Measurement is not supported for {geom_type} geometry."
            feature_id = len(features) + 1
            features.append({
                "id": feature_id, "layer": layer_name, "geometry_type": geom_type,
                "geometry": geometry, "crs": source_label, "properties": record["properties"],
                "measurement": measurement, "measurement_note": reason,
            })
    if not features:
        raise ProcessingError("The uploaded file contains no features.")
    return {
        "features": features,
        "feature_count": len(features),
        "crs": source_label,
        "measurement_crs": {"area": area_crs.to_string(), "length": length_crs.to_string()},
        "geometry_counts": dict(counts),
        "warnings": warnings,
    }
