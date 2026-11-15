"""Tests for the non-destructive Discovery2 migration preview and replay."""
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import discovery2_shadow as preview
from road_discovery_store import CATALOG


class ShadowMigrationTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name)
        self.out = self.root / "discovery2"
        self.out.mkdir()
        self.main_db = self.root / "driveflow_server.db"
        with sqlite3.connect(self.main_db) as db:
            db.execute("CREATE TABLE discovery_segments("
                       "seq INTEGER PRIMARY KEY AUTOINCREMENT, catalog TEXT, "
                       "segment_id TEXT, discovered_at TEXT, source TEXT)")
        (self.out / "comparison_report.json").write_text(json.dumps(
            {"status": "review_only", "safe_to_auto_migrate": False, "counts": {"new_segments": 1393159}}))
        with (self.out / "comparison_candidates.csv").open("w", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["new_id", "status", "old_id", "offset_m", "old_discovered"])
            writer.writerow(["v2-a", "candidate", "legacy-a", "1.5", "0"])
            writer.writerow(["v2-b", "candidate", "legacy-a", "2.0", "0"])
            writer.writerow(["v2-parallel", "ambiguous", "", "1", "0"])
            writer.writerow(["v2-new", "new", "", "", "0"])

    def add(self, sid, timestamp="2026-11-15T12:00:00Z"):
        with sqlite3.connect(self.main_db) as db:
            db.execute("INSERT INTO discovery_segments(catalog,segment_id,discovered_at,source)"
                       " VALUES(?,?,?,?)", (CATALOG, sid, timestamp, "driveflow"))

    def test_preview_replays_late_synchronization_without_touching_legacy(self):
        self.add("unrelated")
        original = sqlite3.connect(self.main_db).execute(
            "SELECT COUNT(*) FROM discovery_segments").fetchone()[0]
        build = preview.candidate_import(self.root, self.main_db)
        self.assertEqual(build["candidate_count"], 2)
        self.assertEqual(build["excluded_count"], 2)
        first = preview.refresh(self.root, self.main_db, batch=1)
        self.assertEqual(first["candidate_discovered_segments"], 0)
        self.assertTrue(first["caught_up_with_server"])
        self.add("legacy-a", "2026-11-16T10:00:00Z")
        later = preview.refresh(self.root, self.main_db, batch=1)
        self.assertEqual(later["candidate_discovered_segments"], 2)
        self.assertTrue(later["caught_up_with_server"])
        self.assertFalse(later["ready_for_switch"])
        self.assertEqual(later["confirmed_discovered_segments"], 0)
        again = preview.refresh(self.root, self.main_db)
        self.assertEqual(again["processed_legacy_events"], 0)
        self.assertEqual(again["candidate_discovered_segments"], 2)
        with sqlite3.connect(self.main_db) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM discovery_segments").fetchone()[0], original + 1)
        with sqlite3.connect(self.out / "shadow_preview.sqlite") as db:
            rows = db.execute(
                "SELECT new_id,old_id,discovered_at FROM discovery2_preview_progress "
                "ORDER BY new_id").fetchall()
        self.assertEqual(rows, [
            ("v2-a", "legacy-a", "2026-11-16T10:00:00Z"),
            ("v2-b", "legacy-a", "2026-11-16T10:00:00Z"),
        ])

    def test_no_overwrite_and_no_ambiguous_false_green(self):
        preview.candidate_import(self.root, self.main_db)
        with self.assertRaises(FileExistsError):
            preview.candidate_import(self.root, self.main_db)
        self.assertFalse(preview.preview_status(self.root)["ready_for_switch"])
        self.assertEqual(preview.preview_status(self.root)["candidate_discovered_segments"], 0)
        with sqlite3.connect(self.out / "shadow_preview.sqlite") as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM discovery2_candidate_map "
                "WHERE new_id='v2-parallel'").fetchone()[0], 0)

    def test_reject_nonfinite_geometric_offsets(self):
        with (self.out / "comparison_candidates.csv").open("a", newline="") as out:
            csv.writer(out).writerow(["invalid-geometry", "candidate", "legacy-a", "nan", "0"])
        with self.assertRaises(ValueError):
            preview.candidate_import(self.root, self.main_db)
        self.assertFalse((self.out / "shadow_preview.sqlite").exists())

    def test_reject_incomplete_catalogue_report(self):
        (self.out / "comparison_report.json").write_text(json.dumps(
            {"status": "review_only", "safe_to_auto_migrate": False,
             "counts": {"new_segments": 100}}))
        with self.assertRaises(preview.NotReady):
            preview.candidate_import(self.root, self.main_db)

    def test_reject_wrong_compare_report(self):
        (self.out / "comparison_report.json").write_text(json.dumps(
            {"status": "ready", "safe_to_auto_migrate": True}))
        with self.assertRaises(preview.NotReady):
            preview.candidate_import(self.root, self.main_db)


    def test_background_refresh_includes_newly_synced_legacy_events(self):
        preview.candidate_import(self.root, self.main_db)
        self.add("legacy-a")
        self.assertTrue(preview.schedule_refresh(self.root, self.main_db))
        with preview._SCHEDULE_LOCK:
            worker = preview._REFRESH_THREAD
        if worker:
            worker.join(timeout=5)
        self.assertEqual(preview.preview_status(self.root)["candidate_discovered_segments"], 2)
        self.add("unrelated")
        self.assertTrue(preview.schedule_refresh(self.root, self.main_db))
        with preview._SCHEDULE_LOCK:
            worker = preview._REFRESH_THREAD
        if worker:
            worker.join(timeout=5)
        self.assertEqual(preview.preview_status(self.root)["legacy_cursor_processed"], 2)
        self.assertFalse(preview.preview_status(self.root)["ready_for_switch"])



if __name__ == "__main__":
    unittest.main()
