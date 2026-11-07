"""Server-owned, append-only Road Discovery segment state.

IDs match the Home Assistant Road Discovery `road_discovery.discovered`
storage/export (`{"ids": [...]}`). Geometry is intentionally not copied.
"""
from __future__ import annotations

import re
import sqlite3

CATALOG = "ha-road-discovery-nl-v0.18"
MAX_BATCH = 5000
MAX_PAGE = 2000
_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")
_SOURCE = re.compile(r"^[a-z0-9_-]{2,32}$")


def create_tables(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS discovery_segments (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog TEXT NOT NULL,
            segment_id TEXT NOT NULL,
            source TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            UNIQUE(catalog, segment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_discovery_catalog_seq
            ON discovery_segments(catalog, seq);
    """)


def _catalog(value: str) -> str:
    if value != CATALOG:
        raise ValueError("onbekende wegenkaartversie; synchronisatie gestopt")
    return value


def merge(db: sqlite3.Connection, payload: dict, now: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("JSON-object verwacht")
    catalog = _catalog(payload.get("catalog", CATALOG))
    source = payload.get("source", "driveflow")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise ValueError("ongeldige bron")
    ids = payload.get("ids")
    if not isinstance(ids, list) or len(ids) > MAX_BATCH:
        raise ValueError("ids moet een lijst van maximaal 5000 elementen zijn")
    if any(not isinstance(s, str) or not _ID.fullmatch(s) for s in ids):
        raise ValueError("ongeldige segment-ID")
    unique = list(dict.fromkeys(ids))
    before = db.total_changes
    db.executemany(
        "INSERT OR IGNORE INTO discovery_segments(catalog,segment_id,source,discovered_at) VALUES(?,?,?,?)",
        ((catalog, sid, source, now) for sid in unique),
    )
    inserted = db.total_changes - before
    return {"ok": True, "catalog": catalog, "received": len(ids),
            "added": inserted, "already_known": len(unique) - inserted,
            "cursor": _cursor(db, catalog)}


def _cursor(db: sqlite3.Connection, catalog: str) -> int:
    return db.execute(
        "SELECT COALESCE(MAX(seq),0) FROM discovery_segments WHERE catalog=?",
        (catalog,),
    ).fetchone()[0]


def status(db: sqlite3.Connection, catalog: str = CATALOG) -> dict:
    _catalog(catalog)
    count, cursor = db.execute(
        "SELECT COUNT(*),COALESCE(MAX(seq),0) FROM discovery_segments WHERE catalog=?",
        (catalog,),
    ).fetchone()
    return {"ok": True, "catalog": catalog, "discovered_segments": count,
            "cursor": cursor, "geometry_available": False}


def changes(db: sqlite3.Connection, since: int, limit: int = 1000,
            catalog: str = CATALOG) -> dict:
    _catalog(catalog)
    if not isinstance(since, int) or since < 0 or not 1 <= limit <= MAX_PAGE:
        raise ValueError("ongeldige cursor of paginagrootte")
    rows = db.execute(
        "SELECT seq,segment_id FROM discovery_segments WHERE catalog=? AND seq>? "
        "ORDER BY seq LIMIT ?", (catalog, since, limit + 1),
    ).fetchall()
    page = rows[:limit]
    next_cursor = page[-1][0] if page else since
    return {"ok": True, "catalog": catalog, "ids": [row[1] for row in page],
            "cursor": next_cursor, "has_more": len(rows) > limit}
