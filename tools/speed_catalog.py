"""Offline-first DriveFlow speed catalog.

Independent from the trip/discovery SQLite databases. Catalog input is a
validated WGS84 JSON-lines export (one road segment per line). No external
requests are made in this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_SEGMENTS = 4_000_000
MAX_LINE = 16384
MAX_VERTICES = 128
MAX_DISTANCE_M = 35.0
AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def valid_coordinate(lat: float, lon: float) -> bool:
    return (math.isfinite(lat) and math.isfinite(lon)
            and 49.0 <= lat <= 54.0 and 2.0 <= lon <= 8.0)


def validate_segment(item: dict) -> tuple:
    identifier = item.get("id")
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 100:
        raise ValueError("invalid segment id")
    speed = item.get("speed_kmh")
    if not isinstance(speed, int) or isinstance(speed, bool) or speed not in range(5, 131):
        raise ValueError("invalid speed")
    coords = item.get("coordinates")
    if not isinstance(coords, list) or not 2 <= len(coords) <= MAX_VERTICES:
        raise ValueError("invalid geometry length")
    vertices = []
    for pair in coords:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("expected [longitude, latitude]")
        lon, lat = pair
        if not isinstance(lat, (float, int)) or not isinstance(lon, (float, int)):
            raise ValueError("coordinate must be numeric")
        lat, lon = float(lat), float(lon)
        if not valid_coordinate(lat, lon):
            raise ValueError("coordinate outside Netherlands")
        vertices.append([lon, lat])
    # Time-window restrictions cannot safely be inferred from incomplete data:
    # these segments must be marked unknown until a full schedule is imported.
    if item.get("variable") or item.get("rush_hour"):
        raise ValueError("dynamic / rush-hour speed cannot be resolved safely")
    start, end, alternative = item.get("time_begin"), item.get("time_end"), item.get("alternate_kmh")
    if any(x is not None for x in (start, end, alternative)):
        if not (isinstance(start, str) and isinstance(end, str) and
                isinstance(alternative, int) and not isinstance(alternative, bool)
                and 5 <= alternative <= 130):
            raise ValueError("incomplete scheduled speed")
        for clock in (start, end):
            parts = clock.split(":")
            if len(parts) not in (2, 3) or not all(p.isascii() and p.isdigit() for p in parts):
                raise ValueError("invalid schedule time")
            hour, minute = int(parts[0]), int(parts[1])
            second = int(parts[2]) if len(parts) == 3 else 0
            if hour > 23 or minute > 59 or second > 59:
                raise ValueError("schedule time out of range")
        if start == end:
            raise ValueError("ambiguous all-day speed schedule")
    return identifier, speed, vertices, start, end, alternative


def _init(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE segments (seq INTEGER PRIMARY KEY, segment_id TEXT UNIQUE NOT NULL,"
               " speed_kmh INTEGER NOT NULL, geometry TEXT NOT NULL,"
               " time_begin TEXT, time_end TEXT, alternate_kmh INTEGER)")
    db.execute("CREATE VIRTUAL TABLE segment_bounds USING rtree("
               "seq, min_lon, max_lon, min_lat, max_lat)")
    db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def import_jsonl(source: Path, destination: Path, version: str) -> dict:
    """Atomic import; rejects malformed catalog and preserves previous version."""
    if not version or len(version) > 100 or not all(c.isalnum() or c in "_-." for c in version):
        raise ValueError("invalid catalog version")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".speed-catalog-", suffix=".sqlite", dir=str(destination.parent))
    os.close(descriptor)
    db = None
    count = 0
    digest = hashlib.sha256()
    try:
        db = sqlite3.connect(temporary)
        _init(db)
        with source.open("rb") as stream:
            for raw in stream:
                if len(raw) > MAX_LINE:
                    raise ValueError("oversized catalog row")
                digest.update(raw)
                if not raw.strip():
                    continue
                item = json.loads(raw)
                identifier, speed, vertices, start, end, alternative = validate_segment(item)
                count += 1
                if count > MAX_SEGMENTS:
                    raise ValueError("catalog exceeds segment limit")
                cursor = db.execute(
                    "INSERT INTO segments (segment_id,speed_kmh,geometry,time_begin,time_end,alternate_kmh) VALUES (?,?,?,?,?,?)",
                    (identifier, speed, json.dumps(vertices, separators=(",", ":")), start, end, alternative))
                longitudes = [v[0] for v in vertices]
                latitudes = [v[1] for v in vertices]
                db.execute("INSERT INTO segment_bounds VALUES (?,?,?,?,?)",
                           (cursor.lastrowid, min(longitudes), max(longitudes),
                            min(latitudes), max(latitudes)))
        if not count:
            raise ValueError("empty speed catalog")
        for k, v in (("version", version), ("sha256", digest.hexdigest()),
                     ("count", str(count))):
            db.execute("INSERT INTO metadata VALUES (?,?)", (k, v))
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("speed catalog failed integrity check")
        db.close()
        db = None
        os.replace(temporary, destination)
        return {"version": version, "segments": count, "sha256": digest.hexdigest()}
    finally:
        if db is not None:
            db.close()
        if os.path.exists(temporary):
            os.unlink(temporary)


def _distance_to_segment(lat: float, lon: float, a: list, b: list) -> float:
    factor_lat = 111195.0
    factor_lon = factor_lat * math.cos(math.radians(lat))
    ax, ay = (a[0] - lon) * factor_lon, (a[1] - lat) * factor_lat
    bx, by = (b[0] - lon) * factor_lon, (b[1] - lat) * factor_lat
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy))) if dx * dx + dy * dy > 1e-7 else 0.0
    return math.hypot(ax + dx * t, ay + dy * t)


def lookup(path: Path, lat: float, lon: float, accuracy_m: float = 15,
           at: datetime | None = None) -> dict:
    if not valid_coordinate(lat, lon) or not math.isfinite(accuracy_m) or not 0 < accuracy_m <= 200:
        raise ValueError("invalid GPS fix")
    if not path.is_file():
        return {"status": "catalog_unavailable", "limit_kmh": None}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        version_row = db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
        version = version_row[0] if version_row else "unknown"
        radius = min(MAX_DISTANCE_M, max(12.0, accuracy_m * 1.5))
        lat_delta = radius / 111195.0
        lon_delta = radius / (111195.0 * math.cos(math.radians(lat)))
        rows = db.execute(
            "SELECT s.segment_id,s.speed_kmh,s.geometry,s.time_begin,s.time_end,s.alternate_kmh FROM segment_bounds b "
            "JOIN segments s ON s.seq=b.seq "
            "WHERE b.min_lon<=? AND b.max_lon>=? AND b.min_lat<=? AND b.max_lat>=? LIMIT 160",
            (lon + lon_delta, lon - lon_delta, lat + lat_delta, lat - lat_delta)).fetchall()
        matches = []
        now = (at or datetime.now(timezone.utc)).astimezone(AMSTERDAM)
        seconds = now.hour * 3600 + now.minute * 60 + now.second
        for identifier, speed, geometry, begin, end, alternate in rows:
            if begin is not None:
                def parse_clock(value: str) -> int:
                    parts = list(map(int, value.split(":")))
                    return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)
                first, last = parse_clock(begin), parse_clock(end)
                active = first <= seconds < last if first < last else (seconds >= first or seconds < last)
                if not active:
                    speed = alternate
            points = json.loads(geometry)
            distance = min(_distance_to_segment(lat, lon, a, b)
                           for a, b in zip(points, points[1:]))
            if distance <= radius:
                matches.append((distance, identifier, speed))
        if not matches:
            return {"status": "no_matching_road", "limit_kmh": None, "dataset_version": version}
        matches.sort()
        first = matches[0]
        # Do not claim one road when an adjacent conflicting parallel road
        # is indistinguishable at current GPS accuracy.
        if any(m[2] != first[2] and m[0] - first[0] <= max(7, accuracy_m)
               for m in matches[1:]):
            return {"status": "ambiguous_parallel_roads", "limit_kmh": None,
                    "dataset_version": version}
        return {"status": "matched", "limit_kmh": first[2],
                "segment_id": first[1], "distance_m": round(first[0], 1),
                "dataset_version": version, "source": "local_catalog"}
