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
