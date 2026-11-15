"""Background *read-only* server comparison for Road Discovery 2.0.

Never activates v2, edits live discovery rows or changes client sync cursors.
Output is a report and an untrusted candidate map for manual verification.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from compare_discovery2_catalogs import compare
from discovery2_upload import (
    EXPECTED_BYTES, EXPECTED_SHA256, catalogue_path, sha256_file,
)
from road_discovery_store import CATALOG as LEGACY_CATALOG

log = logging.getLogger("driveflow.discovery2")
LOCK = threading.Lock()
WORKER = None
STATUS = {"state": "idle", "stage": "not_started", "processed": 0,
          "total": None, "error": None, "report_available": False}
MIN_FREE_BYTES = 1_024 * 1_024 * 1_024


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _paths(data_dir: Path):
    directory = data_dir / "discovery2"
    return directory / "comparison_report.json", directory / "comparison_candidates.csv"


def status(data_dir: Path):
    with LOCK:
        state = dict(STATUS)
    report, candidate_map = _paths(data_dir)
    if report.is_file() and not report.is_symlink():
        state["report_available"] = True
        state["candidate_map_available"] = candidate_map.is_file() and not candidate_map.is_symlink()
        if state["state"] == "idle":
            state["state"] = "complete"
            state["stage"] = "report_available"
    return {"ok": True, **state, "activated": False, "migration_performed": False}


def read_report(data_dir: Path):
    report, _ = _paths(data_dir)
    if not report.is_file() or report.is_symlink():
        return None
    with report.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _progress(stage, processed, total=None):
    with LOCK:
        STATUS.update(stage=stage, processed=processed, total=total,
                      updated_at=_now())


def _work(data_dir: Path, legacy_file: Path, db_path: Path):
    global WORKER
    report, candidate_map = _paths(data_dir)
    partial_map = candidate_map.with_suffix(".csv.partial")
    partial_report = report.with_suffix(".json.partial")
    try:
        _progress("checking_files", 0)
        new = catalogue_path(data_dir)
        if new.is_symlink() or not new.is_file() or new.stat().st_size != EXPECTED_BYTES:
            raise ValueError("De nieuwe catalogus is niet correct geüpload")
        if sha256_file(new) != EXPECTED_SHA256:
            raise ValueError("De nieuwe catalogus voldoet niet aan de SHA256-controle")
        if not legacy_file.is_file() or legacy_file.is_symlink():
            raise ValueError("De oorspronkelijke wegenkaart ontbreekt")
        if report.exists() or candidate_map.exists() or partial_map.exists() or partial_report.exists():
            raise FileExistsError("Vergelijkingsresultaat bestaat al; bestaande bestanden worden niet overschreven")
        if shutil.disk_usage(data_dir).free < MIN_FREE_BYTES:
            raise ValueError("Minimaal 1 GiB vrije schijfruimte vereist voor index en rapport")
        _progress("reading_synchronized_discoveries", 0)
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=15) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=15000")
            discovered = {row[0] for row in db.execute(
                "SELECT segment_id FROM discovery_segments WHERE catalog=?",
                (LEGACY_CATALOG,),
            )}
            cursor = db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM discovery_segments WHERE catalog=?",
                (LEGACY_CATALOG,),
            ).fetchone()[0]
        _progress("indexing_old", 0)
        result = compare(legacy_file, new, partial_report,
                         discovered_ids=discovered, mapping=partial_map,
                         progress=_progress, temporary_parent=data_dir / "discovery2")
        # The report is only a snapshot: newer synchronized or offline data
        # may have arrived while running. No automatic migration may use it.
        result["legacy_sync_cursor_snapshot"] = cursor
        result["synchronized_discoveries_snapshot"] = len(discovered)
        result["requires_offline_outbox_reconciliation"] = True
        result["safe_to_auto_migrate"] = False
        report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        os.replace(partial_map, candidate_map)
        os.replace(partial_report, report)
        with LOCK:
            STATUS.update(state="complete", stage="report_ready",
                          processed=result["counts"]["new_segments"],
                          total=result["counts"]["new_segments"],
                          report_available=True, error=None, updated_at=_now())
    except Exception as exc:
        log.warning("Discovery2 comparison failed: %s", type(exc).__name__)
        # Reports from a failed worker are not trusted.
        partial_map.unlink(missing_ok=True)
        partial_report.unlink(missing_ok=True)
        with LOCK:
            STATUS.update(state="error", stage="failed",
                          error=str(exc)[:180], updated_at=_now())
    finally:
        with LOCK:
            WORKER = None


def start(data_dir: Path, legacy_file: Path, db_path: Path):
    global WORKER
    with LOCK:
        if WORKER is not None and WORKER.is_alive():
            return False, "already_running"
        report, candidate_map = _paths(data_dir)
        if report.exists() or candidate_map.exists():
            return False, "report_already_exists"
        directory = report.parent
        directory.mkdir(parents=True, exist_ok=True)
        STATUS.update(state="running", stage="queued", processed=0,
                      total=None, report_available=False, error=None,
                      updated_at=_now())
        WORKER = threading.Thread(target=_work,
                                  args=(data_dir, legacy_file, db_path),
                                  daemon=True, name="discovery2-compare")
        WORKER.start()
    return True, "started"
