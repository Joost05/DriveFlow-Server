"""Non-destructive HAOS preflight tests."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discovery2_preflight as preflight
from road_discovery_store import CATALOG


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name)
        self.legacy = self.root / "roads_nl.json"
        self.db_path = self.root / "driveflow_server.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE discovery_segments (seq INTEGER PRIMARY KEY,"
                       " catalog TEXT, segment_id TEXT)")
            db.execute("INSERT INTO discovery_segments VALUES (3,?,?)", (CATALOG, "a"))
        self.before = self.db_path.read_bytes()

    def test_no_catalogue_must_fail_closed_but_read_server_db(self):
        result = preflight.evaluate(self.root, self.legacy, self.db_path)
        self.assertFalse(result["ready_for_catalogue_upload"])
        self.assertEqual(result["diagnostics"]["legacy_discoveries_synced"], 1)
        self.assertEqual(result["diagnostics"]["legacy_discovery_cursor"], 3)
        self.assertFalse(result["ready_for_migration"])
        self.assertFalse(result["ready_for_switch"])
        self.assertEqual(self.db_path.read_bytes(), self.before)

    def test_verified_legacy_catalogue_and_free_disk_allow_upload(self):
        self.legacy.write_bytes(b"example")
        with patch.object(preflight, "LEGACY_EXPECTED_SIZE", len(b"example")), \\
             patch.object(preflight, "LEGACY_EXPECTED_GIT_BLOB",
                          preflight._git_blob_sha1(self.legacy)):
            result = preflight.evaluate(self.root, self.legacy, self.db_path)
        self.assertTrue(result["ready_for_catalogue_upload"])
        self.assertFalse(result["ready_for_migration"])
        self.assertEqual(self.db_path.read_bytes(), self.before)

    def test_mismatched_catalogue_never_allows_upload(self):
        self.legacy.write_bytes(b"wrong")
        with patch.object(preflight, "LEGACY_EXPECTED_SIZE", 5):
            result = preflight.evaluate(self.root, self.legacy, self.db_path)
        self.assertFalse(result["gates"]["legacy_catalogue_verified"])
        self.assertFalse(result["ready_for_catalogue_upload"])

    def test_missing_db_does_not_create_file(self):
        missing = self.root / "never-existed.db"
        result = preflight.evaluate(self.root, self.legacy, missing)
        self.assertFalse(result["gates"]["discovery_database_readable"])
        self.assertFalse(missing.exists())

    def test_invalid_staged_catalogue_detected_without_changes(self):
        staged = self.root / "discovery2"
        staged.mkdir()
        (staged / "roads_nl_2_alpha1.json").write_bytes(b"not-the-catalogue")
        result = preflight.evaluate(self.root, self.legacy, self.db_path)
        self.assertEqual(result["diagnostics"]["discovery2_catalogue_state"], "invalid_size")
        self.assertFalse(result["gates"]["discovery2_catalogue_staged_verified"])


if __name__ == "__main__":
    unittest.main()
