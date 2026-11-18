"""In-place v0.6.11 -> v0.6.13-beta1 HAOS upgrade contract.

Uses a persistent /data-style directory and runs the real server init twice,
including a new process-like import. Neither legacy roads nor discoveries may
be reset, and the pre-existing API token must remain valid.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from road_discovery_store import CATALOG as LEGACY_CATALOG


class InPlaceUpgradeTests(unittest.TestCase):
    def test_manifest_keeps_existing_slug_port_backup_and_options(self):
        config = (Path(__file__).resolve().parent / "config.yaml").read_text()
        docker = (Path(__file__).resolve().parent / "Dockerfile").read_text()
        self.assertIn('version: "0.6.13-beta1"', config)
        self.assertIn("slug: driveflow_server", config)
        self.assertIn("  8098/tcp: 8098", config)
        self.assertIn("backup: cold", config)
        self.assertIn("  - share:ro", config)
        self.assertIn("  api_token: password(32,)", config)
        self.assertIn("  geoapify_api_key: password?", config)
        self.assertIn('io.hass.version="0.6.13-beta1"', docker)
        self.assertEqual(server.VERSION, "0.6.13-beta1")
        self.assertEqual(server.PORT, 8098)

    def test_existing_data_and_token_survive_repeated_init(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = "existing-DriveFlow-API-secret-over-32-characters"
            options = root / "options.json"
            options.write_text(json.dumps({
                "api_token": token,
                "geoapify_api_key": "existing-geoapify-secret"
            }), encoding="utf-8")
            db_path = root / "driveflow_server.db"
            legacy_road = root / "roads_nl.json"
            legacy_road.write_bytes(b"original immutable road catalogue marker")
            with (patch.object(server, "DATA_DIR", root),
                  patch.object(server, "FILES_DIR", root / "backups"),
                  patch.object(server, "DB_PATH", db_path),
                  patch.dict(os.environ, {"DRIVEFLOW_TOKEN": "", "GEOAPIFY_API_KEY": ""})):
                server.init_db()
                with sqlite3.connect(db_path) as db:
                    db.execute(
                        "INSERT INTO discovery_segments "
                        "(catalog,segment_id,source,discovered_at) VALUES(?,?,?,?)",
                        (LEGACY_CATALOG, "N50:already-driven", "driveflow",
                         "2026-11-14T12:34:56Z")
                    )
                    db.execute("CREATE TABLE IF NOT EXISTS preserved_user_data("
                               "id INTEGER PRIMARY KEY, content TEXT NOT NULL)")
                    db.execute("INSERT INTO preserved_user_data VALUES(1, 'existing trips remain')")
                    db.commit()
                original = legacy_road.read_bytes()
                self.assertEqual(server.api_token(), token)
                self.assertEqual(server.geoapify_key(), "existing-geoapify-secret")
                server.init_db()  # in-place add-on upgrade/startup
                self.assertEqual(server.api_token(), token)
                self.assertEqual(options.read_text(encoding="utf-8"),
                                 json.dumps({"api_token": token,
                                             "geoapify_api_key": "existing-geoapify-secret"}))
                with sqlite3.connect(db_path) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    row = db.execute(
                        "SELECT segment_id,discovered_at FROM discovery_segments "
                        "WHERE catalog=?", (LEGACY_CATALOG,)
                    ).fetchone()
                    self.assertEqual(row, ("N50:already-driven", "2026-11-14T12:34:56Z"))
                    self.assertEqual(db.execute(
                        "SELECT content FROM preserved_user_data WHERE id=1"
                    ).fetchone()[0], "existing trips remain")
                    self.assertFalse(db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='discovery2'").fetchone())
                self.assertEqual(legacy_road.read_bytes(), original)
                self.assertFalse((root / "discovery2" / "append_catalog.sqlite").exists())
                self.assertFalse((root / "discovery2" / "connected_lines.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
