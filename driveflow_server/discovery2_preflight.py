"""Non-destructive Discovery2 HAOS migration preflight.

Only checks local filesystem and reads SQLite using immutable read-only access
where possible; never creates or modifies the active database or catalogues.
No activation, migration, upload or sync is started here.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

from discovery2_upload import EXPECTED_BYTES, EXPECTED_SHA256, catalogue_path
from road_discovery_store import CATALOG as LEGACY_CATALOG

LEGACY_EXPECTED_SIZE = 38_566_582
LEGACY_EXPECTED_GIT_BLOB = "fbee6ec0eb55051e94d0459e36f86c428afe08e0"
MIN_FREE_BYTES = 1024 * 1024 * 1024
CHUNK = 1024 * 1024


def _git_blob_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {path.stat().st_size}".encode("ascii") + bytes([0]))
    with path.open("rb") as handle:
        for piece in iter(lambda: handle.read(CHUNK), b""):
            digest.update(piece)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for piece in iter(lambda: handle.read(CHUNK), b""):
            digest.update(piece)
    return digest.hexdigest()


def _safe_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def evaluate(data_dir: Path, legacy_file: Path, db_path: Path) -> dict:
    """Return a fail-closed migration preflight diagnostic.

    This reads the real legacy DB strictly read-only. SQLite's ?mode=ro
    prevents creating a missing database. query_only protects further.
    The preflight avoids reporting full paths, secrets, or specific IDs.
    """
    gates = {}
    diagnostics = {}
    gates["legacy_catalogue_exists"] = _safe_file(legacy_file)
    gates["legacy_catalogue_verified"] = False
    if gates["legacy_catalogue_exists"]:
        try:
            size = legacy_file.stat().st_size
            diagnostics["legacy_catalogue_bytes"] = size
            if size == LEGACY_EXPECTED_SIZE:
                gates["legacy_catalogue_verified"] = (
                    _git_blob_sha1(legacy_file) == LEGACY_EXPECTED_GIT_BLOB
                )
        except OSError:
            pass

    gates["discovery_database_exists"] = _safe_file(db_path)
    gates["discovery_database_readable"] = False
    diagnostics["legacy_discoveries_synced"] = None
    diagnostics["legacy_discovery_cursor"] = None
    if gates["discovery_database_exists"]:
        try:
            with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=10) as db:
                db.execute("PRAGMA query_only=ON")
                # COUNT is read-only; no modifications or schema migrations.
                count, cursor = db.execute(
                    "SELECT COUNT(*), COALESCE(MAX(seq),0) "
                    "FROM discovery_segments WHERE catalog=?",
                    (LEGACY_CATALOG,)
                ).fetchone()
                diagnostics["legacy_discoveries_synced"] = int(count)
                diagnostics["legacy_discovery_cursor"] = int(cursor)
                gates["discovery_database_readable"] = True
        except (sqlite3.Error, OSError, ValueError):
            pass

    gates["storage_available"] = False
    try:
        free = shutil.disk_usage(data_dir).free
        diagnostics["free_storage_bytes"] = free
        gates["storage_available"] = free >= MIN_FREE_BYTES
    except OSError:
        pass

    staged = catalogue_path(data_dir)
    gates["discovery2_catalogue_staged_verified"] = False
    diagnostics["discovery2_catalogue_state"] = "not_staged"
    if _safe_file(staged):
        try:
            if staged.stat().st_size != EXPECTED_BYTES:
                diagnostics["discovery2_catalogue_state"] = "invalid_size"
            elif _sha256(staged) != EXPECTED_SHA256:
                diagnostics["discovery2_catalogue_state"] = "checksum_mismatch"
            else:
                diagnostics["discovery2_catalogue_state"] = "verified_staged"
                gates["discovery2_catalogue_staged_verified"] = True
        except OSError:
            diagnostics["discovery2_catalogue_state"] = "io_error"

    # This preflight must never authorize activation. A missing staged file
    # is expected *before* the first upload and should be shown separately.
    return {
        "ok": True,
        "status": "preflight_only",
        "gates": gates,
        "diagnostics": diagnostics,
        "ready_for_catalogue_upload": all(
            gates[k] for k in (
                "legacy_catalogue_verified",
                "discovery_database_readable",
                "storage_available",
            )
        ),
        "ready_for_migration": False,
        "ready_for_switch": False,
        "ready_for_google_play": False,
        "activated": False,
        "legacy_database_modified": False,
    }
