"""Strict, offline review gate for Discovery2 shadow migration.

No writes to the live DriveFlow DB, no activation, and no auto-approval.
Review decisions must enumerate exact old/new pairs from the candidate map.
Only approved pairs are copied to a NEW, independent SQLite preview.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from discovery2_shadow import SCHEMA_VERSION, _meta
from discovery2_upload import REVISION
from road_discovery_store import CATALOG as LEGACY_CATALOG

REVIEW_SCHEMA = "discovery2-reviewed-pairs-v1"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_reviewed_preview(data_dir: Path, decisions_path: Path) -> dict:
    """Create no-clobber reviewed preview; input shadow and legacy DB are read-only.

    JSON example:
      {"schema":"discovery2-reviewed-pairs-v1",
       "catalog":"road-discovery-2-osm-explicit-v1",
       "candidate_map_sha256":"<64 hex>",
       "approvals":[{"new_id":"osm:w1:n2-n3","old_id":"A123"}]}
    The checksum pins decisions to the exact comparison CSV. Approvals are
    *human review records*, not assertions of automatic geometric safety.
    """
    root = data_dir / "discovery2"
    shadow = root / "shadow_preview.sqlite"
    mapping = root / "comparison_candidates.csv"
    destination = root / "reviewed_preview.sqlite"
    for path in (shadow, mapping, decisions_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or unsafe review input: {path.name}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Reviewed preview already exists; refusing overwrite")
    review = json.loads(decisions_path.read_text(encoding="utf-8"))
    if not isinstance(review, dict) or review.get("schema") != REVIEW_SCHEMA:
        raise ValueError("Unsupported review schema")
    if review.get("catalog") != REVISION:
        raise ValueError("Review catalogue revision mismatch")
    checksum = review.get("candidate_map_sha256")
    if not isinstance(checksum, str) or len(checksum) != 64 or checksum != file_sha256(mapping):
        raise ValueError("Review decisions do not match exact candidate CSV")
    approvals = review.get("approvals")
    if not isinstance(approvals, list) or not approvals:
        raise ValueError("Explicit nonempty approvals required")
    pairs = []
    new_ids = set()
    for entry in approvals:
        if not isinstance(entry, dict) or set(entry) != {"new_id", "old_id"}:
            raise ValueError("Approval must contain exact new_id and old_id")
        new_id, old_id = entry["new_id"], entry["old_id"]
        if not isinstance(new_id, str) or not isinstance(old_id, str):
            raise ValueError("Approval IDs must be strings")
        if not new_id or not old_id or new_id in new_ids:
            raise ValueError("Duplicate or empty review ID")
        new_ids.add(new_id)
        pairs.append((new_id, old_id))

    uri = shadow.resolve().as_uri() + "?mode=ro"
    fd, tmp_name = tempfile.mkstemp(prefix=".review-", suffix=".sqlite", dir=root)
    os.close(fd)
    temp = Path(tmp_name)
    try:
        with sqlite3.connect(uri, uri=True) as source, sqlite3.connect(temp) as target:
            source.execute("PRAGMA query_only=ON")
            if _meta(source, "schema_version") != SCHEMA_VERSION:
                raise ValueError("Shadow preview schema mismatch")
            if _meta(source, "catalog") != REVISION:
                raise ValueError("Shadow catalogue mismatch")
            target.executescript("""
                CREATE TABLE reviewed_pairs(
                    new_id TEXT PRIMARY KEY,
                    old_id TEXT NOT NULL
                );
                CREATE TABLE reviewed_progress(
                    new_id TEXT PRIMARY KEY,
                    old_id TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE review_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            with target:
                for new_id, old_id in pairs:
                    row = source.execute(
                        "SELECT relation FROM discovery2_candidate_map "
                        "WHERE new_id=? AND old_id=?", (new_id, old_id)
                    ).fetchone()
                    if row is None or row[0] != "needs_review":
                        raise ValueError(f"Not a valid candidate pair: {new_id}")
                    target.execute("INSERT INTO reviewed_pairs VALUES (?,?)", (new_id, old_id))
                    progress = source.execute(
                        "SELECT discovered_at,source FROM discovery2_preview_progress "
                        "WHERE new_id=? AND old_id=?", (new_id, old_id)
                    ).fetchone()
                    if progress:
                        target.execute("INSERT INTO reviewed_progress VALUES (?,?,?,?)",
                                       (new_id, old_id, *progress))
                metadata = {
                    "catalog": REVISION,
                    "candidate_map_sha256": checksum,
                    "review_decisions_sha256": file_sha256(decisions_path),
                    "shadow_cursor_snapshot": _meta(source, "last_legacy_seq") or "0",
                    "approved_pairs": str(len(pairs)),
                    "last_legacy_seq": "0",
                }
                target.executemany("INSERT INTO review_metadata VALUES (?,?)", metadata.items())
        # Never replace even when multiple builders race.
        os.link(temp, destination)
        with sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True) as counted:
            discovered_approved = counted.execute(
                "SELECT COUNT(*) FROM reviewed_progress"
            ).fetchone()[0]
        return {
            "status": "reviewed_preview_only",
            "approved_pairs": len(pairs),
            "discovered_approved_pairs": discovered_approved,
            "snapshot_only": True,
            "activated": False,
            "ready_for_switch": False,
        }
    finally:
        temp.unlink(missing_ok=True)

def refresh_reviewed_preview(data_dir: Path, server_db_path: Path, batch: int = 5000) -> dict:
    """Idempotent replay of server-synced legacy events into approved pairs only.

    The live discovery database is opened read-only; a committed cursor in the
    separate reviewed preview makes retries and offline catch-up safe. The
    client outbox is NOT considered empty or verified by this operation.
    """
    if not 1 <= batch <= 5000:
        raise ValueError("Invalid replay batch size")
    path = data_dir / "discovery2" / "reviewed_preview.sqlite"
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError("Reviewed preview is not available")
    if server_db_path.is_symlink() or not server_db_path.is_file():
        raise ValueError("Legacy server database unavailable")
    source_uri = server_db_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=15) as source, \
         sqlite3.connect(path, timeout=15) as target:
        source.execute("PRAGMA query_only=ON")
        target.execute("PRAGMA busy_timeout=15000")
        metadata = dict(target.execute("SELECT key,value FROM review_metadata"))
        if metadata.get("catalog") != REVISION:
            raise ValueError("Reviewed catalogue mismatch")
        if "last_legacy_seq" not in metadata:
            raise ValueError("Reviewed preview lacks replay cursor")
        last = int(metadata["last_legacy_seq"])
        if last < 0:
            raise ValueError("Invalid replay cursor")
        # Read a stable batch; a subsequent merge will schedule another pass.
        rows = source.execute(
            "SELECT seq,segment_id,discovered_at,source FROM discovery_segments "
            "WHERE catalog=? AND seq>? ORDER BY seq LIMIT ?",
            (LEGACY_CATALOG, last, batch)
        ).fetchall()
        with target:
            for seq, old_id, discovered_at, origin in rows:
                target.execute(
                    "INSERT OR IGNORE INTO reviewed_progress "
                    "(new_id,old_id,discovered_at,source) "
                    "SELECT new_id, ?, ?, ? FROM reviewed_pairs WHERE old_id=?",
                    (old_id, discovered_at, origin, old_id)
                )
            if rows:
                target.execute(
                    "UPDATE review_metadata SET value=? WHERE key='last_legacy_seq'",
                    (str(rows[-1][0]),)
                )
        cursor = rows[-1][0] if rows else last
        latest = source.execute(
            "SELECT COALESCE(MAX(seq),0) FROM discovery_segments WHERE catalog=?",
            (LEGACY_CATALOG,)
        ).fetchone()[0]
        count = target.execute("SELECT COUNT(*) FROM reviewed_progress").fetchone()[0]
        return {
            "status": "reviewed_preview_only",
            "processed_legacy_events": len(rows),
            "legacy_cursor_processed": cursor,
            "current_server_cursor": latest,
            "caught_up_with_server": cursor >= latest,
            "approved_discovered_segments": count,
            "pending_android_outbox_verified_empty": False,
            "activated": False,
            "ready_for_switch": False,
        }
