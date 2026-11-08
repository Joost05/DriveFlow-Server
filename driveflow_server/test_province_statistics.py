"""Province breakdown uses only the DriveFlow-owned discovery tables."""
import sqlite3
import unittest

from road_discovery_store import create_tables as roads_tables, merge as merge_roads
from discovery_places import create_tables as places_tables, merge as merge_places
from discovery_statistics import create_tables as stats_tables
from province_statistics import create_tables, statistics, _contains, _match, REVISION


class ProvinceStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        roads_tables(self.db)
        places_tables(self.db)
        stats_tables(self.db)
        create_tables(self.db)
        self.db.executemany(
            "INSERT INTO discovery_road_lengths(segment_id,length_m) VALUES (?,?)",
            [("A1:a", 1000), ("A1:b", 2000)])
        self.db.executemany(
            "INSERT INTO discovery_province_roads(segment_id,province) VALUES (?,?)",
            [("A1:a", "Gelderland"), ("A1:b", "Overijssel")])
        self.db.executemany(
            "INSERT INTO discovery_province_places(place_id,province) VALUES (?,?)",
            [("p1", "Gelderland"), ("p2", "Overijssel")])
        self.db.executemany("INSERT INTO discovery_province_names(name) VALUES (?)",
                            [("Gelderland",), ("Overijssel",)])

    def tearDown(self):
        self.db.close()

    def test_zero_baseline_and_existing_discoveries(self):
        self.assertFalse(statistics(self.db)["provinces_ready"])
        self.db.execute(
            "INSERT INTO discovery_province_meta(key,value) VALUES('revision',?)",
            (REVISION,))
        empty = statistics(self.db)
        self.assertEqual(empty["places_total"], 2)
        self.assertEqual(empty["places_percentage"], 0)
        self.assertEqual(empty["provinces"][0]["percentage"], 0)
        merge_roads(self.db, {"ids": ["A1:a"]}, "2026-11-08T12:00:00+00:00")
        merge_places(self.db, {"places": [{"id": "p1", "name": "Plaats"}]},
                     "2026-11-08T12:00:00+00:00")
        loaded = statistics(self.db)
        gelderland = next(p for p in loaded["provinces"] if p["name"] == "Gelderland")
        self.assertEqual(gelderland["road_km_discovered"], 1)
        self.assertEqual(gelderland["places_discovered"], 1)
        self.assertEqual(gelderland["percentage"], 100)
        overijssel = next(p for p in loaded["provinces"] if p["name"] == "Overijssel")
        self.assertEqual(overijssel["percentage"], 0)

    def test_polygon_hole_and_province_match(self):
        polygon = {"type": "Polygon", "coordinates": [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]],
        ]}
        self.assertTrue(_contains(.5, .5, polygon))
        self.assertFalse(_contains(1.5, 1.5, polygon))
        self.assertEqual(_match(.5, .5, [("Gelderland", polygon, (0, 0, 4, 4))]),
                         "Gelderland")


if __name__ == "__main__":
    unittest.main()
