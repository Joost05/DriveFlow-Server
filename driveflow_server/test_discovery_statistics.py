"""The HA statistics start at zero and derive from server-owned discoveries."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from road_discovery_store import create_tables as roads_tables, merge as merge_roads
from discovery_places import create_tables as places_tables, merge as merge_places
from discovery_statistics import index_roads, statistics


class DiscoveryStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        roads_tables(self.db)
        places_tables(self.db)
        self.temp = tempfile.TemporaryDirectory()
        self.roads = Path(self.temp.name) / "roads_nl.json"
        self.roads.write_text(json.dumps({"segments": [
            {"id": "A1:a", "length_m": 100},
            {"id": "A1:b", "length_m": 200},
        ]}), encoding="utf-8")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_zero_baseline_and_repeatable_totals(self):
        self.assertEqual(index_roads(self.db, self.roads), 2)
        before = statistics(self.db)
        self.assertEqual(before["road_percentage"], 0)
        self.assertEqual(before["road_km_total"], 0.3)
        self.assertEqual(before["road_km_discovered"], 0)
        self.assertEqual(before["places_discovered"], 0)
        merge_roads(self.db, {"ids": ["A1:a", "A1:a"]}, "2026-11-08T11:00:00Z")
        merge_places(self.db, {"places": [
            {"id": "p1", "name": "Nieuwe plaats"}]}, "2026-11-08T11:00:00Z")
        after = statistics(self.db, datetime(2026, 11, 8, 12, tzinfo=timezone.utc))
        self.assertEqual(after["road_segments_discovered"], 1)
        self.assertEqual(after["road_km_discovered"], 0.1)
        self.assertAlmostEqual(after["road_percentage"], 33.3333, places=3)
        self.assertEqual(after["places_discovered"], 1)
        self.assertEqual(after["last_place"], "Nieuwe plaats")
        self.assertEqual(index_roads(self.db, self.roads), 2)
        self.assertEqual(statistics(self.db)["road_segments_discovered"], 1)

    def test_no_partial_catalog_after_failure(self):
        index_roads(self.db, self.roads)
        with self.assertRaises(ValueError):
            bad = Path(self.temp.name) / "invalid.json"
            bad.write_text('{"segments":[]}', encoding="utf-8")
            index_roads(self.db, bad, revision="invalid")
        self.assertEqual(statistics(self.db)["road_km_total"], 0.3)


if __name__ == "__main__":
    unittest.main()
