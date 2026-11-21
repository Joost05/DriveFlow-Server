"""NDW/WKD importer safety tests; no large GeoPackage download required."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from convert_ndw_gpkg import convert_feature, speed_value, clock_value


def sample(**properties):
    values = {"FK_VELD1": "segment-123", "MAXSHD": 80, "KENM_RICHT": "B"}
    values.update(properties)
    return {
        "type": "Feature",
        "properties": values,
        "geometry": {"type": "LineString",
                     "coordinates": [[5.9, 52.0], [5.91, 52.0]]}
    }


class NdwConverterTest(unittest.TestCase):
    def test_fixed_limit(self):
        features = convert_feature(sample(), 1)
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["speed_kmh"], 80)

    def test_numeric_gpkg_values(self):
        self.assertEqual(speed_value("100.0"), 100)
        self.assertIsNone(speed_value("NVT"))

    def test_timed_limit(self):
        features = convert_feature(sample(MAXSHD=100, BEGINTIJD="06:00:00",
                                          EINDTIJD="19:00:00", MAXSHD_ALT=130), 2)
        self.assertEqual(features[0]["alternate_kmh"], 130)
        self.assertEqual(features[0]["time_begin"], "06:00:00")

    def test_incomplete_time_limit_skipped(self):
        self.assertEqual(convert_feature(sample(BEGINTIJD="06:00"), 2), [])

    def test_direction_specific_limit_skipped(self):
        self.assertEqual(convert_feature(sample(KENM_RICHT="H"), 3), [])

    def test_uncertain_partial_road_skipped(self):
        self.assertEqual(convert_feature(sample(VAN=25, NAAR=200, LENGTE=500), 4), [])

    def test_invalid_time_rejected(self):
        with self.assertRaises(ValueError):
            clock_value("28:75:00")


if __name__ == "__main__":
    unittest.main()
