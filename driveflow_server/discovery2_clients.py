"""Discovery2 client sync attestations (informational only; never activation).

Client acknowledgements are not a trust boundary: no status from this module
can approve a migration. Even a currently empty outbox may change during
driving or reconnecting.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from road_discovery_store import CATALOG as LEGACY

DEVICE_RE = re.compile(r"^[a-zA-Z0-9_-]{8,80}$")


def _path(data_dir: Path) -> Path:
    return data_dir / "discovery2" / "client_attestations.sqlite"


def _create(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS client_sync_attestations (
        device_id TEXT PRIMARY KEY,
        catalog TEXT NOT NULL,
        legacy_server_cursor INTEGER NOT NULL,
        local_pending_count INTEGER NOT NULL,
        client_reported_complete INTEGER NOT NULL,
        observed_at TEXT NOT NULL
    )""")


def record(data_dir: Path, device_id: str, payload: dict,
           server_cursor: int) -> dict:
    if not DEVICE_RE.fullmatch(device_id):
        raise ValueError("Ongeldig apparaat-ID")
    if not isinstance(payload, dict) or payload.get("catalog") != LEGACY:
        raise ValueError("Ongeldige catalogus")
    pending = payload.get("pending_count")
    complete = payload.get("sync_complete")
    cursor = payload.get("downloaded_cursor")
    if type(pending) is not int or pending != 0:
        raise ValueError("Lokale ontdekkingen zijn nog niet volledig verzonden")
    if complete is not True:
        raise ValueError("Synchronisatie is niet voltooid")
    if type(cursor) is not int or cursor != server_cursor:
        raise ValueError("Synchronisatiecursor wijkt af van servercursor")
    if server_cursor < 0:
        raise ValueError("Ongeldige servercursor")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=15) as db:
        _create(db)
        db.execute("""INSERT INTO client_sync_attestations
            (device_id,catalog,legacy_server_cursor,local_pending_count,
             client_reported_complete,observed_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
                catalog=excluded.catalog,
                legacy_server_cursor=excluded.legacy_server_cursor,
                local_pending_count=excluded.local_pending_count,
                client_reported_complete=excluded.client_reported_complete,
                observed_at=excluded.observed_at""",
                   (device_id, LEGACY, cursor, pending, 1, now))
    return {"ok": True, "acknowledged": True, "device_id": device_id,
            "observed_at": now, "server_cursor": server_cursor,
            "ready_for_switch": False}


def status(data_dir: Path, server_cursor: int):
    path = _path(data_dir)
    if not path.is_file() or path.is_symlink():
        return {"registered_clients": 0, "clients_at_current_cursor": 0,
                "pending_android_outbox_verified_empty": False,
                "ready_for_switch": False}
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
        registered, current = db.execute(
            """SELECT COUNT(*),
                COALESCE(SUM(CASE WHEN legacy_server_cursor=? THEN 1 ELSE 0 END),0)
                FROM client_sync_attestations""", (server_cursor,)).fetchone()
    return {"registered_clients": int(registered),
            "clients_at_current_cursor": int(current),
            # No way to prove that every phone is online or no new GPS fix
            # was generated immediately after its acknowledgement.
            "pending_android_outbox_verified_empty": False,
            "ready_for_switch": False}
