"""Unit tests for a local speed catalog (no network, no trip database)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speed_catalog import import_jsonl, lookup


class SpeedCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "roads.jsonl"
        self.db = self.root / "catalog.sqlite"

    def write(self, *items):
        self.source.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf8")

    def segment(self, key, speed, coordinates):
        return {"id": key, "speed_kmh": speed, "coordinates": coordinates}

    def test_matching_and_version(self):
        self.write(self.segment("road-a", 50, [[5.9, 52.0], [5.91, 52.0]]))
        self.assertEqual(import_jsonl(self.source, self.db, "test-1")["segments"], 1)
        result = lookup(self.db, 52.0, 5.905)
        self.assertEqual(result["limit_kmh"], 50)
        self.assertEqual(result["dataset_version"], "test-1")
        self.assertTrue(result["cacheable"])

    def test_daytime_and_nighttime_speeds(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        road = self.segment("a1", 100, [[5.9, 52.0], [5.91, 52.0]])
        road.update(time_begin="06:00:00", time_end="19:00:00", alternate_kmh=130)
        self.write(road)
        import_jsonl(self.source, self.db, "timed")
        amsterdam = ZoneInfo("Europe/Amsterdam")
        daytime = datetime(2026, 11, 21, 12, 0, tzinfo=amsterdam)
        nighttime = datetime(2026, 11, 21, 23, 0, tzinfo=amsterdam)
        day = lookup(self.db, 52.0, 5.905, at=daytime)
        night = lookup(self.db, 52.0, 5.905, at=nighttime)
        self.assertEqual(day["limit_kmh"], 100)
        self.assertEqual(night["limit_kmh"], 130)
        self.assertFalse(day["cacheable"])
        self.assertFalse(night["cacheable"])

    def test_reject_incomplete_time_window(self):
        road = self.segment("bad-schedule", 100, [[5.9, 52.0], [5.91, 52.0]])
        road["time_begin"] = "06:00"
        self.write(road)
        with self.assertRaises(ValueError):
            import_jsonl(self.source, self.db, "bad-schedule")

    def test_unknown_without_catalog(self):
        self.assertEqual(lookup(self.db, 52.0, 5.905)["status"], "catalog_unavailable")

    def test_parallel_conflicts_are_unknown(self):
        self.write(
            self.segment("road-a", 50, [[5.9, 52.0], [5.91, 52.0]]),
            self.segment("road-b", 80, [[5.9, 52.00002], [5.91, 52.00002]])
        )
        import_jsonl(self.source, self.db, "parallel")
        result = lookup(self.db, 52.0, 5.905, accuracy_m=15)
        self.assertIsNone(result["limit_kmh"])
        self.assertEqual(result["status"], "ambiguous_parallel_roads")

    def test_reject_bad_update_preserving_catalog(self):
        self.write(self.segment("road-a", 50, [[5.9, 52.0], [5.91, 52.0]]))
        import_jsonl(self.source, self.db, "good")
        self.write(self.segment("bad", 999, [[5.9, 52.0], [5.91, 52.0]]))
        with self.assertRaises(ValueError):
            import_jsonl(self.source, self.db, "bad")
        self.assertEqual(lookup(self.db, 52.0, 5.905)["dataset_version"], "good")

    def test_bad_location_rejected(self):
        with self.assertRaises(ValueError):
            lookup(self.db, 0.0, 0.0)


if __name__ == "__main__":
    unittest.main()
