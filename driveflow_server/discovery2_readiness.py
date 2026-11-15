"""Fail-closed Road Discovery 2.0 release gates.

This endpoint is advisory diagnostic output. It cannot activate Discovery 2.0,
and never writes to active or preview databases. No geometric candidate,
phone receipt, or checksum alone can authorize migration.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from discovery2_upload import staged_status, catalogue_path, sha256_file, EXPECTED_SHA256
from discovery2_analysis import read_report
from discovery2_shadow import preview_status
from discovery2_clients import status as client_status
from road_discovery_store import CATALOG as LEGACY


def evaluate(data_dir: Path, db_path: Path) -> dict:
    gates = {}
    catalogue = staged_status(data_dir)
    # Status only verifies file length. A release gate must also re-hash
    # the pinned 248MB file instead of trusting the directory entry.
    gates["catalogue_staged"] = (
        catalogue.get("state") == "staged"
        and sha256_file(catalogue_path(data_dir)) == EXPECTED_SHA256
    )
    report = read_report(data_dir)
    gates["comparison_completed"] = bool(
        isinstance(report, dict) and report.get("status") == "review_only"
        and isinstance(report.get("counts"), dict)
        and report["counts"].get("new_segments") == 1_393_159
    )
    preview = preview_status(data_dir)
    gates["shadow_preview_built"] = preview.get("status") == "preview_only"

    cursor = None
    try:
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            cursor = db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM discovery_segments WHERE catalog=?",
                (LEGACY,),
            ).fetchone()[0]
    except (sqlite3.Error, OSError):
        pass

    gates["legacy_server_available"] = cursor is not None
    gates["preview_caught_up"] = (
        cursor is not None and gates["shadow_preview_built"]
        and preview.get("legacy_cursor_processed") == cursor
    )
    clients = client_status(data_dir, cursor if cursor is not None else -1)
    gates["at_least_one_client_synced"] = clients.get("clients_at_current_cursor", 0) > 0

    # The following must be verified by human-reviewed geometry coverage,
    # real device tests, and an explicit two-phase activation protocol.
    # Fail closed: there is no approval or activation pathway in alpha1.
    gates["geographic_matches_validated"] = False
    gates["all_android_outboxes_drained"] = False
    gates["v2_server_sync_implemented"] = False
    gates["android_atomic_switch_and_rollback_tested"] = False
    gates["haos_and_device_integration_tests_passed"] = False

    return {
        "ok": True,
        "status": "development_only",
        "catalogue": catalogue.get("revision"),
        "legacy_server_cursor": cursor,
        "shadow_preview": preview,
        "clients": clients,
        "gates": gates,
        "blocking_gates": [key for key, passed in gates.items() if not passed],
        "ready_for_switch": False,
        "ready_for_google_play": False,
        "activated": False,
    }
