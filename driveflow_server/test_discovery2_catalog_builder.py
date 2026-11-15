"""Contract checks; no live server, network or OSM dump required."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
