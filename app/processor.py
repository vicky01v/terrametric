"""Safe ingestion and CRS-aware measurements for KML and zipped Shapefiles."""
from __future__ import annotations

import json
import math
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

import shapefile
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely import make_valid
from shapely.geometry import (
    GeometryCollection, LineString, LinearRing, MultiLineString, MultiPoint,
    MultiPolygon, Point, Polygon, mapping, shape,
)
from shapely.ops import transform

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_FEATURES = 100_000


class ProcessingError(ValueError):
    pass


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kml_coordinates(text: str | None) -> list[tuple[float, ...]]:
    coordinates = []
    for token in (text or "").split():
        values = tuple(float(value) for value in token.split(",") if value != "")
        if len(values) < 2 or not all(math.isfinite(value) for value in values):
            raise ProcessingError("The KML file contains invalid coordinates.")
        coordinates.append(values)
    return coordinates


def _kml_geometry(element: ET.Element):
    kind = _local_name(element.tag)
    child_by_name = lambda parent, wanted: next((child for child in parent if _local_name(child.tag) == wanted), None)
    if kind in {"Point", "LineString", "LinearRing"}:
        node = next((node for node in element.iter() if _local_name(node.tag) == "coordinates"), None)
        coords = _kml_coordinates(node.text if node is not None else None)
        if not coords:
            return None
        if kind == "Point":
            return Point(coords[0])
        return LinearRing(coords) if kind == "LinearRing" else LineString(coords)
    if kind == "Polygon":
        outer = child_by_name(element, "outerBoundaryIs")
        outer_ring = next((node for node in outer.iter() if _local_name(node.tag) == "LinearRing"), None) if outer is not None else None
        if outer_ring is None:
            return None
        shell = _kml_coordinates(next((n.text for n in outer_ring.iter() if _local_name(n.tag) == "coordinates"), None))
        holes = []
        for boundary in element:
            if _local_name(boundary.tag) != "innerBoundaryIs":
                continue
            ring = next((node for node in boundary.iter() if _local_name(node.tag) == "LinearRing"), None)
            if ring is not None:
                holes.append(_kml_coordinates(next((n.text for n in ring.iter() if _local_name(n.tag) == "coordinates"), None)))
        return Polygon(shell, holes)
    if kind == "MultiGeometry":
        parts = [geom for child in element for geom in [_kml_geometry(child)] if geom is not None and not geom.is_empty]
        if not parts:
            return None
        if all(isinstance(part, Polygon) for part in parts):
            return MultiPolygon(parts)
        if all(isinstance(part, (LineString, LinearRing)) for part in parts):
            return MultiLineString(parts)
        if all(isinstance(part, Point) for part in parts):
            return MultiPoint(parts)
        return GeometryCollection(parts)
    return None


def _read_kml(payload: bytes) -> list[tuple[str, list[dict[str, Any]], CRS | None]]:
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ProcessingError("KML files containing document type or entity declarations are not accepted.")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ProcessingError("The KML file is not valid XML.") from exc
    placemarks = [element for element in root.iter() if _local_name(element.tag) == "Placemark"]
    records = []
    for index, placemark in enumerate(placemarks, start=1):
        props: dict[str, Any] = {}
        for element in placemark:
            name = _local_name(element.tag)
            if name in {"name", "description"} and element.text:
                props["Name" if name == "name" else "Description"] = element.text.strip()
            elif name == "ExtendedData":
                for item in element.iter():
                    if _local_name(item.tag) == "Data" and item.attrib.get("name"):
                        value = next((child.text for child in item if _local_name(child.tag) == "value"), None)
                        props[item.attrib["name"]] = _json_value(value.strip() if value else None)
                    elif _local_name(item.tag) == "SimpleData" and item.attrib.get("name"):
                        props[item.attrib["name"]] = _json_value(item.text.strip() if item.text else None)
        geometry_element = next((element for element in placemark if _local_name(element.tag) in {"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry"}), None)
        try:
            geom = _kml_geometry(geometry_element) if geometry_element is not None else None
        except (ValueError, TypeError) as exc:
            raise ProcessingError(f"KML feature {index} has invalid geometry coordinates.") from exc
        if geom is not None:
            props.setdefault("_kml_id", placemark.attrib.get("id", str(index)))
        records.append({"geometry": geom, "properties": props})
        if len(records) > MAX_FEATURES:
            raise ProcessingError(f"The upload exceeds the {MAX_FEATURES:,} feature limit.")
    if not placemarks:
        raise ProcessingError("No KML placemarks were found in the file.")
    return [("KML", records, CRS.from_epsg(4326))]


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
    if suffix == "kml":
        return _read_kml(payload)
    sources: list[tuple[str, str]] = []
    temp = tempfile.TemporaryDirectory(prefix="terrametric-")
    try:
        if suffix == "zip":
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
                with shapefile.Reader(path, encoding="utf-8", encodingErrors="replace") as collection:
                    records = []
                    field_names = [field[0] for field in collection.fields[1:]]
                    for feature in collection.iterShapeRecords():
                        try:
                            geometry_data = feature.shape.__geo_interface__ if feature.shape.shapeType else None
                            geom = shape(geometry_data) if geometry_data else None
                        except Exception:
                            # Preserve an unconvertible feature in the response as unmeasured.
                            geom = None
                        props = {str(k): _json_value(v) for k, v in zip(field_names, feature.record)}
                        records.append({"geometry": geom, "properties": props})
                        if feature_total + len(records) > MAX_FEATURES:
                            raise ProcessingError(f"The upload exceeds the {MAX_FEATURES:,} feature limit.")
                    feature_total += len(records)
                    path_by_lower = {candidate.name.lower(): candidate for candidate in Path(path).parent.iterdir()}
                    prj = path_by_lower.get(f"{Path(path).stem.lower()}.prj")
                    raw_crs = prj.read_text(errors="replace") if prj else None
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

    valid_geometries = []
    for _, records, layer_crs in layers:
        layer_crs = layer_crs or source_crs
        to_source = Transformer.from_crs(layer_crs, source_crs, always_xy=True).transform if not layer_crs.equals(source_crs) else None
        for record in records:
            geom = record["geometry"]
            if geom is not None and to_source:
                geom = transform(to_source, geom)
            if geom is not None and not geom.is_empty:
                valid_geometries.append(geom)
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
