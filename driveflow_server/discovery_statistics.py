"""Server-authoritative DriveFlow Road Discovery statistics (new discoveries only).

The road catalogue is indexed once into SQLite; every subsequent request uses
the server's append-only discovery tables. No HA Road Discovery import occurs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from road_discovery_store import CATALOG as ROADS
from discovery_places import CATALOG as PLACES

CATALOG_REVISION = "ha-road-discovery-nl-v0.18"
METERS_PER_KM = 1000.0


def create_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS discovery_road_lengths (
        segment_id TEXT PRIMARY KEY, length_m REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS discovery_statistics_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")


def index_roads(db: sqlite3.Connection, path: Path, revision: str = CATALOG_REVISION) -> int:
    """Index trusted catalogue geometry; keep discoveries in their own tables.

    A failed parse rolls back rather than publishing partial national totals.
    """
    create_tables(db)
    current = db.execute(
        "SELECT value FROM discovery_statistics_meta WHERE key='road_revision'"
    ).fetchone()
    if current and current[0] == revision:
        return db.execute("SELECT COUNT(*) FROM discovery_road_lengths").fetchone()[0]
    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list) or not segments:
        raise ValueError("ongeldig of leeg wegenbestand")
    entries = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_id, length = segment.get("id"), segment.get("length_m")
        if not isinstance(segment_id, str) or not segment_id:
            continue
        if not isinstance(length, (int, float)) or not 0 < length < 500000:
            continue
        entries.append((segment_id, float(length)))
    if not entries:
        raise ValueError("geen bruikbare weglengtes")
    # Do not remove discovery events or reset synchronization cursors.
    with db:
        db.execute("DELETE FROM discovery_road_lengths")
        db.executemany(
            "INSERT OR REPLACE INTO discovery_road_lengths(segment_id,length_m) VALUES(?,?)",
            entries)
        db.execute(
            "INSERT OR REPLACE INTO discovery_statistics_meta(key,value) VALUES('road_revision',?)",
            (revision,))
    return len(entries)


def statistics(db: sqlite3.Connection, now: datetime | None = None) -> dict:
    create_tables(db)
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo('Europe/Amsterdam')).date().isoformat()
    row = db.execute("""SELECT COUNT(*),COALESCE(SUM(length_m),0)
        FROM discovery_road_lengths""").fetchone()
    road_total, meters_total = int(row[0]), float(row[1])
    discovered, meters = db.execute("""SELECT COUNT(*),COALESCE(SUM(l.length_m),0)
        FROM discovery_segments d JOIN discovery_road_lengths l
        ON d.segment_id=l.segment_id WHERE d.catalog=?""", (ROADS,)).fetchone()
    discovered = int(discovered)
    meters = float(meters)
    road_today, meters_today = db.execute("""SELECT COUNT(*),COALESCE(SUM(l.length_m),0)
        FROM discovery_segments d JOIN discovery_road_lengths l
        ON d.segment_id=l.segment_id WHERE d.catalog=? AND substr(d.discovered_at,1,10)=?""",
        (ROADS, today)).fetchone()
    places = db.execute(
        "SELECT COUNT(*) FROM discovery_places WHERE catalog=?", (PLACES,)
    ).fetchone()[0]
    places_today = db.execute("""SELECT COUNT(*) FROM discovery_places
        WHERE catalog=? AND substr(discovered_at,1,10)=?""", (PLACES, today)).fetchone()[0]
    recent = db.execute("""SELECT name,discovered_at FROM discovery_places
        WHERE catalog=? ORDER BY seq DESC LIMIT 1""", (PLACES,)).fetchone()
    return {
        "ok": True, "catalog": ROADS, "places_catalog": PLACES,
        "roads_ready": road_total > 0,
        "road_segments_total": road_total,
        "road_segments_discovered": discovered,
        "road_km_total": round(meters_total / METERS_PER_KM, 3),
        "road_km_discovered": round(meters / METERS_PER_KM, 3),
        "road_km_today": round(float(meters_today) / METERS_PER_KM, 3),
        "road_segments_today": int(road_today),
        "road_percentage": round(100 * meters / meters_total, 4) if meters_total else None,
        "places_discovered": int(places),
        "places_today": int(places_today),
        "last_place": recent[0] if recent else None,
        "last_place_at": recent[1] if recent else None,
        "source": "driveflow_server",
    }
