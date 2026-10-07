"""Small transactional SQLite store for processed uploads."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_DIR = Path(os.environ.get("TERRAMETRIC_DATA_DIR", "data"))
DB_PATH = DATA_DIR / "terrametric.sqlite3"


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL, feature_count INTEGER NOT NULL, crs TEXT NOT NULL,
            measurement_crs TEXT NOT NULL, geometry_counts TEXT NOT NULL,
            warnings TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS features (
            file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            feature_id INTEGER NOT NULL, geometry_type TEXT NOT NULL,
            body TEXT NOT NULL, PRIMARY KEY(file_id, feature_id)
        );
        CREATE INDEX IF NOT EXISTS features_file_type ON features(file_id, geometry_type);
        """)


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "filename": row["filename"], "created_at": row["created_at"],
        "status": row["status"], "feature_count": row["feature_count"], "crs": row["crs"],
        "measurement_crs": json.loads(row["measurement_crs"]),
        "geometry_counts": json.loads(row["geometry_counts"]), "warnings": json.loads(row["warnings"]),
        "measurements_url": f"/api/files/{row['id']}/measurements/",
        "geojson_url": f"/api/files/{row['id']}/geojson/",
    }


def create(filename: str, result: dict[str, Any]) -> dict[str, Any]:
    file_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, filename, now, "COMPLETED", result["feature_count"], result["crs"],
             json.dumps(result["measurement_crs"]), json.dumps(result["geometry_counts"]), json.dumps(result["warnings"])),
        )
        db.executemany(
            "INSERT INTO features VALUES (?, ?, ?, ?)",
            [(file_id, f["id"], f["geometry_type"], json.dumps(f, separators=(",", ":"), allow_nan=False)) for f in result["features"]],
        )
        row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    return _record(row)


def get_file(file_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    return _record(row) if row else None


def list_files(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM files ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_record(row) for row in rows]


def filtered_features(file_id: str, geometry_type: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
    clauses, params = ["file_id=?"], [file_id]
    if geometry_type and geometry_type.lower() != "all":
        clauses.append("lower(geometry_type)=lower(?)")
        params.append(geometry_type)
    query = "SELECT body FROM features WHERE " + " AND ".join(clauses) + " ORDER BY feature_id"
    with connect() as db:
        rows = db.execute(query, params).fetchall()
    features = [json.loads(row["body"]) for row in rows]
    if search:
        needle = search.casefold()
        features = [f for f in features if needle in json.dumps(f["properties"], ensure_ascii=False).casefold()]
    return features


def get_feature(file_id: str, feature_id: int) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT body FROM features WHERE file_id=? AND feature_id=?", (file_id, feature_id)).fetchone()
    return json.loads(row["body"]) if row else None


def dashboard_summary() -> dict[str, Any]:
    with connect() as db:
        counts = db.execute("SELECT COUNT(*) AS files, COALESCE(SUM(feature_count), 0) AS features FROM files").fetchone()
        rows = db.execute("SELECT features.body FROM features JOIN files ON files.id=features.file_id").fetchall()
    area_m2 = length_m = 0.0
    for row in rows:
        measurement = json.loads(row["body"])["measurement"]
        if measurement and measurement["kind"] == "area":
            area_m2 += measurement["value"]
        elif measurement and measurement["kind"] == "length":
            length_m += measurement["value"]
    return {
        "file_count": counts["files"], "feature_count": counts["features"],
        "area_m2": area_m2, "area_hectares": area_m2 / 10000,
        "length_m": length_m, "length_km": length_m / 1000,
    }


def delete_file(file_id: str) -> bool:
    with connect() as db:
        cursor = db.execute("DELETE FROM files WHERE id=?", (file_id,))
    return cursor.rowcount > 0
