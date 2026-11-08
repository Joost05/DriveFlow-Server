"""DriveFlow-owned discovery of CBS population cores.

The IDs and source version follow Road Discovery v0.18.0. This module stores
discoveries only: full CBS geometry remains a versioned catalogue managed
separately from discovery events.
"""
from __future__ import annotations

import re
import sqlite3

CATALOG = "cbs-bevolkingskernen-2021"
MAX_BATCH = 1000
MAX_PAGE = 2000
_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,160}$")
_SOURCE = re.compile(r"^[a-z0-9_-]{2,32}$")


def create_tables(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS discovery_places (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog TEXT NOT NULL,
            place_id TEXT NOT NULL,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            UNIQUE(catalog, place_id)
        );
        CREATE INDEX IF NOT EXISTS idx_discovery_places_seq
            ON discovery_places(catalog, seq);
    """)


def _check_catalog(value: str) -> None:
    if value != CATALOG:
        raise ValueError("onbekende CBS-plaatscatalogus")


def merge(db: sqlite3.Connection, payload: dict, now: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("JSON-object verwacht")
    catalog = payload.get("catalog", CATALOG)
    _check_catalog(catalog)
    source = payload.get("source", "driveflow")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise ValueError("ongeldige ontdekkingsbron")
    places = payload.get("places")
    if not isinstance(places, list) or len(places) > MAX_BATCH:
        raise ValueError("places moet een lijst van maximaal 1000 items zijn")
    prepared = {}
    for item in places:
        if not isinstance(item, dict):
            raise ValueError("plaatsrecord moet een object zijn")
        place_id, name = item.get("id"), item.get("name")
        if not isinstance(place_id, str) or not _ID.fullmatch(place_id):
            raise ValueError("ongeldige plaats-ID")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise ValueError("ongeldige plaatsnaam")
        prepared[place_id] = name.strip()
    before = db.total_changes
    db.executemany(
        "INSERT OR IGNORE INTO discovery_places(catalog,place_id,name,source,discovered_at) "
        "VALUES(?,?,?,?,?)",
        ((catalog, place_id, name, source, now) for place_id, name in prepared.items()),
    )
    added = db.total_changes - before
    return {
        "ok": True, "catalog": catalog, "received": len(places),
        "added": added, "already_known": len(prepared) - added,
        "cursor": _cursor(db, catalog),
    }


def _cursor(db: sqlite3.Connection, catalog: str) -> int:
    return db.execute(
        "SELECT COALESCE(MAX(seq),0) FROM discovery_places WHERE catalog=?",
        (catalog,),
    ).fetchone()[0]


def status(db: sqlite3.Connection, catalog: str = CATALOG) -> dict:
    _check_catalog(catalog)
    total, cursor = db.execute(
        "SELECT COUNT(*),COALESCE(MAX(seq),0) FROM discovery_places WHERE catalog=?",
        (catalog,),
    ).fetchone()
    return {"ok": True, "catalog": catalog, "discovered_places": total, "cursor": cursor}


def changes(db: sqlite3.Connection, since: int, limit: int = 1000,
            catalog: str = CATALOG) -> dict:
    _check_catalog(catalog)
    if not isinstance(since, int) or since < 0 or not 1 <= limit <= MAX_PAGE:
        raise ValueError("ongeldige cursor of paginagrootte")
    rows = db.execute(
        "SELECT seq,place_id,name FROM discovery_places WHERE catalog=? AND seq>? "
        "ORDER BY seq LIMIT ?", (catalog, since, limit + 1),
    ).fetchall()
    page = rows[:limit]
    return {
        "ok": True, "catalog": catalog,
        "places": [{"id": row[1], "name": row[2]} for row in page],
        "cursor": page[-1][0] if page else since, "has_more": len(rows) > limit,
    }
