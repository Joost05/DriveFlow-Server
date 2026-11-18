"""Isolated Docker HTTP smoke test for Discovery 2.0 preflight.

Run against a newly started *test* DriveFlow container, never production.
No real HAOS secrets, routes or catalogues are used.
"""
import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("SMOKE_URL", "http://127.0.0.1:18098")
TOKEN = os.environ["SMOKE_TOKEN"]
DB = Path(os.environ["SMOKE_DATA_DIR"]) / "driveflow_server.db"


def call(path, token=None, method="GET", data=None, extra=None):
    headers = dict(extra or {})
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    for _ in range(40):
        try:
            if call("/v1/health")[0] == 200:
                break
        except (OSError, TimeoutError):
            pass
        time.sleep(0.25)
    else:
        raise AssertionError("Test container did not start")

    assert call("/v1/discovery2/preflight")[0] == 401
    assert call("/v1/discovery2/preflight", "incorrect")[0] == 401
    assert DB.is_file(), "Container did not initialize test database"
    with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as db:
        before_rows = db.execute("SELECT COUNT(*) FROM discovery_segments").fetchone()[0]
    baseline = fingerprint(DB)
    status, report = call("/v1/discovery2/preflight", TOKEN)
    assert status == 200, (status, report)
    assert report["status"] == "preflight_only"
    assert report["gates"]["discovery_database_readable"] is True
    assert report["gates"]["legacy_catalogue_exists"] is False
    assert report["gates"]["legacy_catalogue_verified"] is False
    assert report["ready_for_catalogue_upload"] is False
    assert report["ready_for_migration"] is False
    assert report["ready_for_switch"] is False
    assert report["activated"] is False
    assert report["diagnostics"]["legacy_discoveries_synced"] == 0
    assert report["diagnostics"]["discovery2_catalogue_state"] == "not_staged"
    assert call("/v1/discovery2/compare/status", TOKEN)[0] == 200
    status, body = call("/v1/discovery2/catalog/stage", TOKEN,
                        method="PUT", data=b"wrong",
                        extra={"Content-Type": "application/json",
                               "X-Content-SHA256": "0" * 64})
    assert status in (400, 413, 422), (status, body)
    # Read-only endpoints and an invalid staging attempt must not alter data.
    assert fingerprint(DB) == baseline, "Legacy SQLite file changed"
    with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("SELECT COUNT(*) FROM discovery_segments").fetchone()[0] == before_rows
    assert not (DB.parent / "discovery2" / "roads_nl_2_alpha1.json").exists()
    print("PASS: HTTP auth, fail-closed preflight, invalid upload, legacy DB unchanged")


if __name__ == "__main__":
    main()
