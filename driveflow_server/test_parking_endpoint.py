"""Regression: all GET routes must use the module-level parse_qs import."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server import Api


class _Request:
    path = "/v1/parking/nearby?lat=52.0907&lon=5.1214"
    server = SimpleNamespace(geoapify_key="test-key")

    def __init__(self):
        self.response = None

    def authenticated(self):
        return True

    def json(self, status, body):
        self.response = (status, body)


class ParkingEndpointTests(unittest.TestCase):
    def test_parking_success_does_not_shadow_query_parser(self):
        self.assertNotIn("parse_qs", Api.do_GET.__code__.co_varnames)
        request = _Request()
        with patch("server.search_parking", return_value=[{"name": "Test"}]) as lookup:
            Api.do_GET(request)
        self.assertEqual(request.response[0], 200)
        self.assertEqual(request.response[1]["places"][0]["name"], "Test")
        lookup.assert_called_once_with(52.0907, 5.1214, "test-key")


if __name__ == "__main__":
    unittest.main()
