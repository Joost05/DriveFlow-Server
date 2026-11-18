"""Safety and coverage tests for Discovery2 mapping audit."""
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from discovery2_audit import audit
from road_discovery_store import CATALOG


class CandidateAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mapping = self.root / "candidates.csv"
        self.report = self.root / "review-report.json"
        self.db = self.root / "legacy.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE discovery_segments "
                         "(seq INTEGER PRIMARY KEY,catalog TEXT,segment_id TEXT,"
                         "discovered_at TEXT,source TEXT)")
            conn.execute("INSERT INTO discovery_segments VALUES (1,?,?,?,?)",
                         (CATALOG, "old-a", "2026-11-14T12:00:00Z", "android"))
        self.rows = [
            ["a", "candidate", "old-a", "1.0", "1"],
            ["b", "candidate", "old-a", "7.5", "1"],
            ["c", "candidate", "old-b", "0.1", "0"],
            ["d", "ambiguous", "", "1.0", "0"],
            ["e", "new", "", "", "0"],
            ["f", "invalid_geometry", "", "", "0"],
        ]
        self.write_rows()

    def write_rows(self):
        with self.mapping.open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["new_id", "status", "old_id", "offset_m", "old_discovered"])
            writer.writerows(self.rows)

    def test_audit_counts_flags_and_never_changes_discovery(self):
        before = self.db.read_bytes()
        result = audit(self.mapping, self.report, self.db)
        self.assertEqual(result["status"], "manual_review_required")
        self.assertFalse(result["catalogue_activation_allowed"])
        self.assertEqual(result["counts"]["candidate"], 3)
        self.assertEqual(result["counts"]["ambiguous"], 1)
        self.assertEqual(result["counts"]["near_10m"], 1)
        self.assertEqual(result["counts"]["legacy_segments_with_multiple_candidates"], 1)
        self.assertEqual(result["counts"]["server_discovered_legacy_segments_with_candidates"], 1)
        self.assertEqual(result["samples"]["shared_legacy_segment"][0]["old_id"], "old-a")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(json.loads(self.report.read_text())["status"], "manual_review_required")
        with self.assertRaises(FileExistsError):
            audit(self.mapping, self.report, self.db)

    def test_bad_offset_never_writes_report(self):
        self.rows[0][3] = "nan"
        self.write_rows()
        with self.assertRaises(ValueError):
            audit(self.mapping, self.report)
        self.assertFalse(self.report.exists())

    def test_unknown_status_never_writes_report(self):
        self.rows[0][1] = "approved"
        self.write_rows()
        with self.assertRaises(ValueError):
            audit(self.mapping, self.report)
        self.assertFalse(self.report.exists())

    def test_readonly_without_live_db(self):
        result = audit(self.mapping, self.report)
        self.assertNotIn("server_discovered_legacy_segments", result["counts"])
        self.assertFalse(result["safe_to_auto_approve"])


if __name__ == "__main__":
    unittest.main()
