import unittest
from parking_geoapify import normalize, search, ParkingError


class ParkingGeoapifyTests(unittest.TestCase):
    def test_real_features_deduplicated_and_sorted(self):
        payload = {"features": [
            {"properties": {"place_id": "a", "name": "Parkeergarage"}, "geometry": {"coordinates": [5.1215, 52.0908]}},
            {"properties": {"place_id": "a", "name": "duplicate"}, "geometry": {"coordinates": [5.1215, 52.0908]}},
            {"properties": {"place_id": "b", "name": "Far away"}, "geometry": {"coordinates": [6.0, 53.0]}}
        ]}
        result = normalize(payload, 52.0907, 5.1214)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Parkeergarage")
        self.assertGreater(result[0]["distance_m"], 0)

    def test_missing_features_is_invalid(self):
        with self.assertRaises(ParkingError):
            normalize({}, 52.09, 5.12)

    def test_zero_zero_is_rejected_before_network(self):
        with self.assertRaises(ParkingError) as result:
            search(0, 0, "fake-key")
        self.assertEqual(result.exception.kind, "INVALID_LOCATION")


if __name__ == "__main__":
    unittest.main()
