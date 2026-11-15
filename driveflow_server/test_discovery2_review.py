"""Tests for explicit, non-destructive Discovery2 mapping approvals."""
import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from discovery2_review import build_reviewed_preview, refresh_reviewed_preview, REVIEW_SCHEMA
from discovery2_shadow import create_tables, SCHEMA_VERSION
from discovery2_upload import REVISION
from road_discovery_store import CATALOG


class ReviewedMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.d2 = self.root / "discovery2"
        self.d2.mkdir()
        self.map = self.d2 / "comparison_candidates.csv"
        with self.map.open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(("new_id", "status", "old_id", "offset_m", "old_discovered"))
            writer.writerow(("v2-good", "candidate", "legacy-good", "1.0", "1"))
            writer.writerow(("v2-ambiguous", "ambiguous", "", "1.0", "0"))
        self.shadow = self.d2 / "shadow_preview.sqlite"
        with sqlite3.connect(self.shadow) as db:
            create_tables(db)
            db.execute("INSERT INTO discovery2_candidate_map(new_id,old_id,offset_m)"
                       " VALUES ('v2-good','legacy-good',1)")
            db.execute("INSERT INTO discovery2_preview_progress"
                       "(new_id,old_id,discovered_at,source)"
                       " VALUES ('v2-good','legacy-good','2026-11-15T10:00:00Z','driveflow')")
            db.executemany("INSERT INTO discovery2_preview_meta VALUES (?,?)",
                           [("schema_version", SCHEMA_VERSION), ("catalog", REVISION),
                            ("last_legacy_seq", "42")])
        self.live = self.root / "driveflow.db"
        with sqlite3.connect(self.live) as db:
            db.execute("CREATE TABLE legacy (id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO legacy VALUES ('untouched')")
        self.review_path = self.root / "review.json"

    def decisions(self, approvals, checksum=None):
        data = {
            "schema": REVIEW_SCHEMA,
            "catalog": REVISION,
            "candidate_map_sha256": checksum or hashlib.sha256(self.map.read_bytes()).hexdigest(),
            "approvals": approvals,
        }
        self.review_path.write_text(json.dumps(data))
        return self.review_path

    def test_approved_pair_copies_provenance_without_touching_live(self):
        self.decisions([{"new_id": "v2-good", "old_id": "legacy-good"}])
        result = build_reviewed_preview(self.root, self.review_path)
        self.assertFalse(result["ready_for_switch"])
        self.assertFalse(result["activated"])
        self.assertEqual(result["discovered_approved_pairs"], 1)
        with sqlite3.connect(self.d2 / "reviewed_preview.sqlite") as db:
            self.assertEqual(db.execute("SELECT * FROM reviewed_progress").fetchall(),
                             [("v2-good", "legacy-good", "2026-11-15T10:00:00Z", "driveflow")])
            self.assertEqual(db.execute("SELECT value FROM review_metadata "
                                        "WHERE key='shadow_cursor_snapshot'").fetchone()[0], "42")
        with sqlite3.connect(self.live) as db:
            self.assertEqual(db.execute("SELECT id FROM legacy").fetchall(), [("untouched",)])
        with self.assertRaises(FileExistsError):
            build_reviewed_preview(self.root, self.review_path)

    def test_ambiguous_pair_fails_closed_and_no_output(self):
        self.decisions([{"new_id": "v2-ambiguous", "old_id": "legacy-good"}])
        with self.assertRaises(ValueError):
            build_reviewed_preview(self.root, self.review_path)
        self.assertFalse((self.d2 / "reviewed_preview.sqlite").exists())

    def test_wrong_old_id_is_rejected(self):
        self.decisions([{"new_id": "v2-good", "old_id": "legacy-wrong"}])
        with self.assertRaises(ValueError):
            build_reviewed_preview(self.root, self.review_path)

    def test_stale_csv_hash_rejected(self):
        self.decisions([{"new_id": "v2-good", "old_id": "legacy-good"}], checksum="0" * 64)
        with self.assertRaises(ValueError):
            build_reviewed_preview(self.root, self.review_path)

    def test_duplicate_review_rejected(self):
        self.decisions([{"new_id": "v2-good", "old_id": "legacy-good"}] * 2)
        with self.assertRaises(ValueError):
            build_reviewed_preview(self.root, self.review_path)

    def test_incremental_replay_including_offline_arrivals_and_retry(self):
        # This scenario begins with no previously discovered approved pair.
        with sqlite3.connect(self.shadow) as db:
            db.execute("DELETE FROM discovery2_preview_progress")
        self.decisions([{"new_id": "v2-good", "old_id": "legacy-good"}])
        build_reviewed_preview(self.root, self.review_path)
        with sqlite3.connect(self.live) as db:
            db.execute("CREATE TABLE discovery_segments (seq INTEGER PRIMARY KEY, "
                       "catalog TEXT,segment_id TEXT,discovered_at TEXT,source TEXT)")
            db.execute("INSERT INTO discovery_segments VALUES (1,?,?,?,?)",
                       (CATALOG, "other", "2026-11-15T11:00:00Z", "android"))
        first = refresh_reviewed_preview(self.root, self.live, batch=1)
        self.assertEqual(first["approved_discovered_segments"], 0)
        self.assertTrue(first["caught_up_with_server"])
        # Simulate an Android offline outbox upload arriving later.
        with sqlite3.connect(self.live) as db:
            db.execute("INSERT INTO discovery_segments VALUES (2,?,?,?,?)",
                       (CATALOG, "legacy-good", "2026-11-14T07:00:00Z", "android-offline"))
        second = refresh_reviewed_preview(self.root, self.live, batch=1)
        self.assertEqual(second["approved_discovered_segments"], 1)
        self.assertEqual(second["legacy_cursor_processed"], 2)
        self.assertFalse(second["pending_android_outbox_verified_empty"])
        retry = refresh_reviewed_preview(self.root, self.live)
        self.assertEqual(retry["processed_legacy_events"], 0)
        self.assertEqual(retry["approved_discovered_segments"], 1)
        with sqlite3.connect(self.d2 / "reviewed_preview.sqlite") as db:
            self.assertEqual(
                db.execute("SELECT discovered_at,source FROM reviewed_progress").fetchone(),
                ("2026-11-14T07:00:00Z", "android-offline")
            )
        with sqlite3.connect(self.live) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_segments").fetchone()[0], 2)

    def test_replay_error_does_not_advance_cursor(self):
        self.decisions([{"new_id": "v2-good", "old_id": "legacy-good"}])
        build_reviewed_preview(self.root, self.review_path)
        with self.assertRaises(sqlite3.OperationalError):
            refresh_reviewed_preview(self.root, self.live)
        with sqlite3.connect(self.d2 / "reviewed_preview.sqlite") as db:
            self.assertEqual(
                db.execute("SELECT value FROM review_metadata "
                           "WHERE key='last_legacy_seq'").fetchone()[0], "0"
            )


if __name__ == "__main__":
    unittest.main()
