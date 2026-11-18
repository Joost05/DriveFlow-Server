"""Append-only, namespace-separated Road Discovery catalogue index.

This is *not* the active DriveFlow discovery database and cannot activate V2.
Legacy roads always remain distinct; OSM roads are added in their own
namespace. Geometric similarity NEVER grants discovered/verified status.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from compare_discovery2_catalogs import stream_segments
from discovery2_upload import EXPECTED_BYTES, EXPECTED_SHA256, REVISION, sha256_file
from discovery2_preflight import (
    LEGACY_EXPECTED_GIT_BLOB, LEGACY_EXPECTED_SIZE, _git_blob_sha1,
)
from road_discovery_store import CATALOG as LEGACY_CATALOG

SCHEMA_VERSION = "discovery2-append-catalog-v1"
BUILD_BATCH = 5000
MAX_ID_LENGTH = 256


def _verified_file(path: Path, size: int, digest: str, *, git_blob: bool = False) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise ValueError("Missing or invalid catalogue size")
    actual = _git_blob_sha1(path) if git_blob else sha256_file(path)
    if actual != digest:
        raise ValueError("Catalogue checksum mismatch")


def _db_setup(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS catalog_segments (
            namespace TEXT NOT NULL CHECK(namespace IN ('legacy', 'osm')),
            segment_id TEXT NOT NULL CHECK(length(segment_id) BETWEEN 1 AND 256),
            payload TEXT NOT NULL,
            PRIMARY KEY(namespace, segment_id)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS catalog_imports (
            namespace TEXT PRIMARY KEY CHECK(namespace IN ('legacy', 'osm')),
            revision TEXT NOT NULL,
            checksum TEXT NOT NULL,
            segments_count INTEGER NOT NULL CHECK(segments_count >= 0)
        );
        CREATE TRIGGER IF NOT EXISTS no_catalog_segment_update
        BEFORE UPDATE ON catalog_segments
        BEGIN SELECT RAISE(ABORT, 'append-only: updates forbidden'); END;
        CREATE TRIGGER IF NOT EXISTS no_catalog_segment_delete
        BEFORE DELETE ON catalog_segments
        BEGIN SELECT RAISE(ABORT, 'append-only: deletes forbidden'); END;
        CREATE TRIGGER IF NOT EXISTS no_catalog_import_update
        BEFORE UPDATE ON catalog_imports
        BEGIN SELECT RAISE(ABORT, 'append-only: import updates forbidden'); END;
        CREATE TRIGGER IF NOT EXISTS no_catalog_import_delete
        BEFORE DELETE ON catalog_imports
        BEGIN SELECT RAISE(ABORT, 'append-only: import deletes forbidden'); END;
    """)


def append_file(db: sqlite3.Connection, source: Path, *, namespace: str,
                revision: str, checksum: str) -> dict:
    """Insert segments; existing identical IDs remain unchanged.

    A whole source import is atomic. A duplicate ID with DIFFERENT payload
    causes rollback rather than silently replacing previously known roads.
    Existing immutable imports are only accepted for the same revision/hash.
    """
    if namespace not in ("legacy", "osm"):
        raise ValueError("Invalid namespace")
    before = db.execute(
        "SELECT revision,checksum,segments_count FROM catalog_imports WHERE namespace=?",
        (namespace,),
    ).fetchone()
    if before is not None:
        if before[0] != revision or before[1] != checksum:
            raise ValueError("Existing catalogue source identity differs; refusing replacement")
        return {"namespace": namespace, "added": 0, "already_imported": before[2]}

    # One transaction for all records: no published partial import if interrupted.
    db.execute("BEGIN IMMEDIATE")
    try:
        added = 0
        total = 0
        for segment in stream_segments(source):
            sid = segment.get("id") if isinstance(segment, dict) else None
            if not isinstance(sid, str) or not (1 <= len(sid) <= MAX_ID_LENGTH):
                raise ValueError("Invalid road segment ID")
            # Keep the original geometry and metadata. No remapping of IDs.
            payload = json.dumps(segment, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                                 allow_nan=False)
            row = db.execute(
                "SELECT payload FROM catalog_segments WHERE namespace=? AND segment_id=?",
                (namespace, sid),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO catalog_segments(namespace,segment_id,payload) VALUES(?,?,?)",
                    (namespace, sid, payload),
                )
                added += 1
            elif row[0] != payload:
                raise ValueError("Duplicate segment ID with different geometry or attributes")
            total += 1
        db.execute(
            "INSERT INTO catalog_imports(namespace,revision,checksum,segments_count) VALUES(?,?,?,?)",
            (namespace, revision, checksum, total),
        )
        db.commit()
        return {"namespace": namespace, "added": added, "already_imported": total - added}
    except BaseException:
        db.rollback()
        raise


def build(data_dir: Path, legacy_file: Path, new_file: Path, *,
          check_pinned: bool = True) -> dict:
    """Build a new, no-clobber combined index in /data/discovery2/.

    The live DriveFlow DB and source JSON files are never modified.
    Pinned hashes are mandatory for normal production inputs. The unpinned
    option is reserved for deterministic, artificial unit tests.
    """
    if legacy_file.resolve() == new_file.resolve():
        raise ValueError("Original and new catalogues must be different")
    if check_pinned:
        _verified_file(legacy_file, LEGACY_EXPECTED_SIZE, LEGACY_EXPECTED_GIT_BLOB, git_blob=True)
        _verified_file(new_file, EXPECTED_BYTES, EXPECTED_SHA256)
    output_dir = data_dir / "discovery2"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "append_catalog.sqlite"
    if target.exists() or target.is_symlink():
        raise FileExistsError("Append catalogue already exists; refusing overwrite")
    # Exclusive temporary file to protect other concurrent build attempts.
    temp = output_dir / (".append-catalog-" + os.urandom(12).hex() + ".sqlite")
    try:
        with sqlite3.connect(temp) as db:
            _db_setup(db)
            db.execute("PRAGMA busy_timeout=15000")
            legacy = append_file(db, legacy_file, namespace="legacy",
                                 revision=LEGACY_CATALOG, checksum=LEGACY_EXPECTED_GIT_BLOB)
            newer = append_file(db, new_file, namespace="osm",
                                revision=REVISION, checksum=EXPECTED_SHA256)
            totals = db.execute(
                "SELECT namespace, COUNT(*) FROM catalog_segments GROUP BY namespace"
            ).fetchall()
        # Hard link is atomic and fails closed if destination already exists.
        os.link(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return {
        "status": "append_catalog_preview_only",
        "legacy": legacy, "new": newer,
        "counts_by_namespace": dict(totals),
        "old_segments_deleted": 0,
        "legacy_discoveries_modified": False,
        "physical_road_deduplication_verified": False,
        "activated": False, "ready_for_switch": False,
    }


def status(data_dir: Path) -> dict:
    path = data_dir / "discovery2" / "append_catalog.sqlite"
    if path.is_symlink() or not path.is_file():
        return {"status": "not_built", "activated": False}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        versions = dict(db.execute(
            "SELECT namespace, revision FROM catalog_imports"
        ))
        counts = dict(db.execute(
            "SELECT namespace, COUNT(*) FROM catalog_segments GROUP BY namespace"
        ))
    return {"status": "append_catalog_preview_only", "versions": versions,
            "counts_by_namespace": counts, "physical_road_deduplication_verified": False,
            "activated": False, "ready_for_switch": False}
