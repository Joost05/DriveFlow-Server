"""Synthetic tests: server-side comparison never edits synchronized discoveries."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discovery2_analysis as analysis
from road_discovery_store import CATALOG


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "discovery2").mkdir()
        self.old = self.root / "roads_nl.json"
        self.old.write_text('{"segments":[]}', encoding="utf-8")
        self.staged = analysis.catalogue_path(self.root)
        self.staged.write_bytes(b'{"segments":[]}')
        self.db_path = self.root / "driveflow_server.db"
        db = sqlite3.connect(self.db_path)
        db.execute("CREATE TABLE discovery_segments ("
                   "seq INTEGER PRIMARY KEY, catalog TEXT, segment_id TEXT, "
                   "source TEXT, discovered_at TEXT)")
        db.execute("INSERT INTO discovery_segments VALUES (1, ?, 'old-1', 'driveflow', '2026-01-01')",
                   (CATALOG,))
        db.commit()
        db.close()
        with analysis.LOCK:
            analysis.STATUS.update(state="idle", stage="not_started", processed=0,
                                   total=None, error=None, report_available=False)

    def test_successful_analysis_exports_snapshot_without_modification(self):
        before = self.db_path.read_bytes()
        observed = {}

        def fake_compare(legacy, newer, report, **kwargs):
            observed["discovered"] = kwargs["discovered_ids"]
            report.write_text('{"status":"review_only"}', encoding="utf-8")
            kwargs["mapping"].write_text("new_id,status,old_id\n", encoding="utf-8")
            return {"counts": {"new_segments": 2}, "safe_to_auto_migrate": False,
                    "migration_performed": False}

        with patch.object(analysis, "EXPECTED_BYTES", self.staged.stat().st_size), \
             patch.object(analysis, "EXPECTED_SHA256", hashlib.sha256(self.staged.read_bytes()).hexdigest()), \
             patch.object(analysis, "compare", side_effect=fake_compare), \
             patch.object(analysis.shutil, "disk_usage") as disk:
            disk.return_value.free = analysis.MIN_FREE_BYTES + 1
            analysis._work(self.root, self.old, self.db_path)

        self.assertEqual(observed["discovered"], {"old-1"})
        self.assertEqual(before, self.db_path.read_bytes())
        result = analysis.read_report(self.root)
        self.assertEqual(result["legacy_sync_cursor_snapshot"], 1)
        self.assertTrue(result["requires_offline_outbox_reconciliation"])
        self.assertFalse(result["safe_to_auto_migrate"])
        self.assertTrue(analysis.status(self.root)["report_available"])
        self.assertTrue((self.root / "discovery2/comparison_candidates.csv").exists())
        self.assertFalse((self.root / "discovery2/comparison_report.json.partial").exists())

    def test_bad_digest_blocks_analysis_and_keeps_discoveries(self):
        before = self.db_path.read_bytes()
        with patch.object(analysis, "EXPECTED_BYTES", self.staged.stat().st_size):
            analysis._work(self.root, self.old, self.db_path)
        self.assertEqual(analysis.status(self.root)["state"], "error")
        self.assertFalse(analysis.status(self.root)["report_available"])
        self.assertEqual(before, self.db_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
