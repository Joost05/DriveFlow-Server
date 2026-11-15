"""Discovery2 shadow migration: append-only legacy progress + candidate review.

This module intentionally NEVER inserts into discovery_segments for v2 and
NEVER changes legacy state. It maintains a separate, rebuildable preview
derived from the latest server-synchronized legacy discoveries. Crucially,
new legacy discoveries automatically become visible in the preview after
incremental refresh; no early snapshot is mistaken for final progress.

Geographic candidates are NOT approved road identities. All candidates are
labelled 'needs_review' (not confirmed green), even when their source is known
to be discovered. Parallel or ambiguous roads are excluded entirely.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from road_discovery_store import CATALOG as LEGACY_CATALOG
from discovery2_upload import REVISION as V2_CATALOGUE

BATCH = 5000
SCHEMA_VERSION = "discovery2-shadow-v1"


class NotReady(RuntimeError):
    pass


def create_tables(db: sqlite3.Connection):
    # Not the active discovery storage: solely a disposable preview.
    db.executescript("""
    CREATE TABLE IF NOT EXISTS discovery2_candidate_map (
        new_id TEXT PRIMARY KEY,
        old_id TEXT NOT NULL,
        offset_m REAL NOT NULL,
        relation TEXT NOT NULL DEFAULT 'needs_review'
    );
    CREATE INDEX IF NOT EXISTS idx_discovery2_candidate_old
        ON discovery2_candidate_map(old_id);
    CREATE TABLE IF NOT EXISTS discovery2_preview_progress (
        new_id TEXT PRIMARY KEY,
        old_id TEXT NOT NULL,
        discovered_at TEXT NOT NULL,
        source TEXT NOT NULL,
        relation TEXT NOT NULL DEFAULT 'needs_review'
    );
    CREATE TABLE IF NOT EXISTS discovery2_preview_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """) 


def _meta(db, key):
    row = db.execute("SELECT value FROM discovery2_preview_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _put_meta(db, key, value):
    db.execute("INSERT INTO discovery2_preview_meta(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _mapping_file(data_dir: Path):
    mapping = data_dir / "discovery2" / "comparison_candidates.csv"
    report = data_dir / "discovery2" / "comparison_report.json"
    if (not mapping.is_file() or mapping.is_symlink() or
            not report.is_file() or report.is_symlink()):
        raise NotReady("Vergelijkingsrapport ontbreekt; start eerst de serververgelijking")
    with report.open("r", encoding="utf-8") as handle:
        summary = json.load(handle)
    if summary.get("status") != "review_only" or summary.get("safe_to_auto_migrate") is not False:
        raise NotReady("Het vergelijkingsrapport heeft een onverwachte status")
    return mapping


def candidate_import(data_dir: Path, db_path: Path):
    """Build a *separate* preview DB on disk and publish atomically.

    Uses a fresh SQLite file, never mutates the live DriveFlow database.
    Existing preview must be absent to prevent accidental reset.
    """
    mapping = _mapping_file(data_dir)
    destination = data_dir / "discovery2" / "shadow_preview.sqlite"
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Preview bestaat al; deze wordt niet overschreven")
    fd, temp_name = tempfile.mkstemp(prefix=".shadow-", suffix=".sqlite", dir=destination.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        with sqlite3.connect(temporary) as db:
            create_tables(db)
            count = 0
            rejected = 0
            with mapping.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != ["new_id", "status", "old_id", "offset_m", "old_discovered"]:
                    raise ValueError("CSV-schema van vergelijkingsrapport komt niet overeen")
                for row in reader:
                    if row["status"] != "candidate":
                        rejected += 1
                        continue
                    new_id, old_id = row["new_id"], row["old_id"]
                    if not new_id or not old_id:
                        raise ValueError("Ongeldige segment-ID in kandidaatkaart")
                    distance = float(row["offset_m"])
                    if not (0 <= distance <= 10):
                        raise ValueError("Ongeldige afstand in kandidaatkaart")
                    db.execute(
                        "INSERT INTO discovery2_candidate_map(new_id,old_id,offset_m) VALUES(?,?,?)",
                        (new_id, old_id, distance),
                    )
                    count += 1
                    if count % BATCH == 0:
                        db.commit()
            _put_meta(db, "schema_version", SCHEMA_VERSION)
            _put_meta(db, "catalog", V2_CATALOGUE)
            _put_meta(db, "candidate_count", count)
            _put_meta(db, "excluded_count", rejected)
            _put_meta(db, "last_legacy_seq", 0)
            db.commit()
        # Link is no-clobber on the same volume.
        os.link(temporary, destination)
        return {"status": "preview_built", "candidate_count": count,
                "excluded_count": rejected, "activated": False}
    finally:
        temporary.unlink(missing_ok=True)


def refresh(data_dir: Path, server_db_path: Path, batch: int = BATCH):
    """Replay newly synced legacy discoveries into the separate preview.

    If Android still has offline items, they will reach the legacy server
    through the *existing* merge endpoint and can be included on the
    next refresh. A transactionally stored legacy cursor permits retry.
    """
    path = data_dir / "discovery2" / "shadow_preview.sqlite"
    if not path.is_file() or path.is_symlink():
        raise NotReady("Er is nog geen preview gemaakt")
    if not 1 <= batch <= BATCH:
        raise ValueError("Ongeldige batchgrootte")
    uri = server_db_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=15) as source, \
         sqlite3.connect(path, timeout=15) as target:
        source.execute("PRAGMA query_only=ON")
        target.execute("PRAGMA busy_timeout=15000")
        if _meta(target, "schema_version") != SCHEMA_VERSION:
            raise NotReady("Onbekende previewversie")
        last = int(_meta(target, "last_legacy_seq") or "0")
        rows = source.execute(
            "SELECT seq,segment_id,discovered_at,source FROM discovery_segments "
            "WHERE catalog=? AND seq>? ORDER BY seq ASC LIMIT ?",
            (LEGACY_CATALOG, last, batch),
        ).fetchall()
        with target:
            for seq, old_id, discovered_at, origin in rows:
                target.execute(
                    "INSERT OR IGNORE INTO discovery2_preview_progress "
                    "(new_id, old_id, discovered_at, source) "
                    "SELECT new_id, ?, ?, ? FROM discovery2_candidate_map WHERE old_id=?",
                    (old_id, discovered_at, origin, old_id),
                )
            if rows:
                _put_meta(target, "last_legacy_seq", rows[-1][0])
        latest_source_cursor = source.execute(
            "SELECT COALESCE(MAX(seq),0) FROM discovery_segments WHERE catalog=?",
            (LEGACY_CATALOG,),
        ).fetchone()[0]
        totals = target.execute(
            "SELECT COUNT(*) FROM discovery2_preview_progress",
        ).fetchone()[0]
        candidates = int(_meta(target, "candidate_count") or "0")
        return {
            "status": "preview_only", "catalog": V2_CATALOGUE,
            "processed_legacy_events": len(rows),
            "cursor": rows[-1][0] if rows else last,
            "current_server_cursor": latest_source_cursor,
            "caught_up_with_server": (rows[-1][0] if rows else last) >= latest_source_cursor,
            "pending_android_outbox_verified_empty": False,
            "candidate_discovered_segments": totals,
            "candidate_segments_total": candidates,
            "confirmed_discovered_segments": 0,
            "activated": False,
            "ready_for_switch": False,
        }


def preview_status(data_dir: Path):
    path = data_dir / "discovery2" / "shadow_preview.sqlite"
    if not path.is_file() or path.is_symlink():
        return {"status": "not_built", "activated": False, "ready_for_switch": False}
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
        if _meta(db, "schema_version") != SCHEMA_VERSION:
            raise NotReady("Onbekende previewversie")
        return {
            "status": "preview_only", "catalog": V2_CATALOGUE,
            "candidate_segments_total": int(_meta(db, "candidate_count") or 0),
            "candidate_discovered_segments": db.execute(
                "SELECT COUNT(*) FROM discovery2_preview_progress").fetchone()[0],
            "legacy_cursor_processed": int(_meta(db, "last_legacy_seq") or 0),
            "pending_android_outbox_verified_empty": False,
            "confirmed_discovered_segments": 0,
            "activated": False, "ready_for_switch": False,
        }
