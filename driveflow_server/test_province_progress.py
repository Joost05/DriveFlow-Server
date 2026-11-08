"""Province indexing progress, retry state, and idempotent catalogue preparation."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import province_statistics as provinces
import server
from discovery_statistics import create_tables as length_tables
from discovery_places import create_tables as places_tables
from road_discovery_store import create_tables as roads_tables


class ProvinceProgressTests(unittest.TestCase):
    def test_server_progress_snapshot(self):
        server._province_progress("wegen_indelen", 2000, 10000)
        snapshot = server.province_status_snapshot()
        self.assertEqual(snapshot["status"], "bezig")
        self.assertEqual(snapshot["stage"], "wegen_indelen")
        self.assertEqual(snapshot["processed"], 2000)
        self.assertEqual(snapshot["total"], 10000)
        self.assertIsNone(snapshot["error"])
        server._province_progress("gereed", 12, 12)
        self.assertEqual(server.province_status_snapshot()["status"], "gereed")

    def test_catalogue_preparation_preserves_discoveries(self):
        db = sqlite3.connect(":memory:")
        roads_tables(db)
        places_tables(db)
        length_tables(db)
        provinces.create_tables(db)
        db.execute("INSERT INTO discovery_segments(catalog,segment_id,source,discovered_at) "
                   "VALUES (?,?,?,?)",
                   ("ha-road-discovery-nl-v0.18", "A1:a", "driveflow", "2026-11-08T00:00:00+00:00"))
        with tempfile.TemporaryDirectory() as folder:
            road_file = Path(folder) / "roads.json"
            road_file.write_text(json.dumps({"segments": [
                {"id": "A1:a", "a": [0.2, 0.2], "b": [0.8, 0.8], "length_m": 1000}
            ]}), encoding="utf-8")
            polygons = []
            for index in range(12):
                x = index * 3
                polygons.append({"properties": {"naam": "Provincie " + str(index)},
                                 "geometry": {"type": "Polygon", "coordinates": [[
                                     [x, 0], [x+1, 0], [x+1, 1], [x, 1], [x, 0]
                                 ]]}})
            def fake_page(url, prefix):
                if "provinciegebied" in url:
                    return {"features": polygons}
                return {"features": [{"id": "place1", "properties": {"naam": "Test"},
                    "geometry": {"type": "Polygon", "coordinates": [[
                        [0, 0], [1, 0], [1, 1], [0, 1], [0, 0]
                    ]]}}], "links": []}
            events = []
            with patch.object(provinces, "_page", side_effect=fake_page):
                result = provinces.prepare(db, road_file, lambda *args: events.append(args))
            self.assertTrue(result["ready"])
            self.assertTrue(any(e[0] == "wegen_indelen" for e in events))
            self.assertEqual(events[-1], ("gereed", 12, 12))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_segments").fetchone()[0], 1)
            self.assertTrue(provinces.statistics(db)["provinces_ready"])
            with patch.object(provinces, "_page", side_effect=AssertionError("should use cache")):
                self.assertTrue(provinces.prepare(db, road_file)["cached"])
        db.close()


if __name__ == "__main__":
    unittest.main()
