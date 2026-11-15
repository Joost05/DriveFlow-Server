"""Contract checks; no live server, network or OSM dump required."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from build_discovery2_catalog import (
    REVISION, eligible, length_metres, parse_maxspeed, segment_id,
    validate_segment,
)


class CatalogueV2Tests(unittest.TestCase):
    def test_explicit_limits(self):
        self.assertEqual(parse_maxspeed({"maxspeed": "80"}), 80)
        self.assertEqual(parse_maxspeed({"maxspeed": "60 km/h"}), 60)
        self.assertEqual(parse_maxspeed({"maxspeed": "50 mph"}), 80)
        for speed in ("NL:rural", "signals", "none", "80;100", "", "150"):
            self.assertIsNone(parse_maxspeed({"maxspeed": speed}), speed)
        self.assertIsNone(parse_maxspeed({"maxspeed": "80", "maxspeed:conditional": "60 @ (Mo-Fr)"}))
        self.assertIsNone(parse_maxspeed({"maxspeed": "80", "maxspeed:forward": "80"}))

    def test_classification_and_threshold(self):
        self.assertTrue(eligible({"highway": "tertiary", "maxspeed": "60"}))
        self.assertTrue(eligible({"highway": "unclassified", "maxspeed": "80"}))
        self.assertTrue(eligible({"highway": "secondary_link", "maxspeed": "70"}))
        for highway in ("residential", "living_street", "service", "track", "footway"):
            self.assertFalse(eligible({"highway": highway, "maxspeed": "80"}))
        self.assertFalse(eligible({"highway": "secondary", "maxspeed": "50"}))
        self.assertFalse(eligible({"highway": "secondary"}))
        self.assertFalse(eligible({"highway": "secondary", "maxspeed": "80", "access": "private"}))

    def test_stable_ids_and_geographic_lengths(self):
        self.assertEqual(segment_id(1234, 8, 9), segment_id(1234, 9, 8))
        self.assertNotEqual(segment_id(1234, 8, 9), segment_id(1235, 8, 9))
        self.assertTrue(50 < length_metres((5.0, 52.0), (5.001, 52.0)) < 90)
        self.assertTrue(REVISION.startswith("road-discovery-2"))

    def test_segment_validation(self):
        good = {"id": segment_id(1, 2, 3), "road": "Binnenweg",
                "a": (5.0, 52.0), "b": (5.001, 52.0),
                "length_m": 68.0, "highway": "tertiary",
                "maxspeed_kmh": 80}
        validate_segment(good)
        for field, value in (("maxspeed_kmh", 30), ("highway", "residential"),
                             ("length_m", 0.0), ("b", (200.0, 52.0))):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_segment({**good, field: value})


    def test_generated_file_and_manifest_with_fake_osmium(self):
        from build_discovery2_catalog import generate

        class Location:
            def __init__(self, lon, lat):
                self.lon = lon
                self.lat = lat
            def valid(self):
                return True

        class Node:
            def __init__(self, identifier, lon, lat):
                self.ref = identifier
                self.location = Location(lon, lat)

        class Way:
            def __init__(self, identifier, nodes, tags):
                self.id = identifier
                self.nodes = nodes
                self.tags = tags

        class FakeHandler:
            def apply_file(self, filename, locations=False):
                self.way(Way(5, [Node(10, 5, 52), Node(11, 5.001, 52)],
                             {"highway": "tertiary", "maxspeed": "80", "name": "Landweg"}))
                self.way(Way(6, [Node(12, 5, 52), Node(13, 5.001, 52)],
                             {"highway": "residential", "maxspeed": "80"}))
                self.way(Way(7, [Node(14, 5, 52), Node(15, 5.001, 52)],
                             {"highway": "secondary", "maxspeed": "50"}))

        fake = types.SimpleNamespace(SimpleHandler=FakeHandler)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"osmium": fake}):
            inp = Path(tmp) / "test.osm.pbf"
            inp.write_bytes(b"fixture")
            out = Path(tmp) / "roads_2.json"
            manifest = generate(inp, out)
            self.assertEqual(manifest["segments"], 1)
            self.assertEqual(manifest["eligible_ways"], 1)
            self.assertEqual(manifest["excluded_ways"], 2)
            self.assertFalse(manifest["release_ready"])
            self.assertFalse(manifest["sync_compatible_with_legacy"])
            body = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(body["revision"], REVISION)
            self.assertEqual(len(body["segments"]), 1)
            self.assertEqual(body["segments"][0]["id"], "osm:w5:n10-11")
            self.assertEqual(body["segments"][0]["maxspeed_kmh"], 80)
            self.assertEqual(json.loads(Path(str(out) + ".manifest.json").read_text())["sha256"],
                             manifest["sha256"])
            with self.assertRaises(FileExistsError):
                generate(inp, out)


if __name__ == "__main__":
    unittest.main()
