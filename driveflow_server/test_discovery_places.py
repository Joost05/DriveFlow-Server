"""Tests for idempotent, durable CBS population-core discovery."""
import sqlite3
import unittest

from discovery_places import CATALOG, create_tables, merge, status, changes


class DiscoveryPlacesTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        create_tables(self.db)

    def tearDown(self):
        self.db.close()

    def test_merge_and_idempotency(self):
        payload = {"catalog": CATALOG, "source": "driveflow",
                   "places": [{"id": "CBS-123", "name": "Apeldoorn"},
                              {"id": "CBS-456", "name": "Heerde"}]}
        first = merge(self.db, payload, "2026-11-07T12:00:00Z")
        second = merge(self.db, payload, "2026-11-07T12:00:10Z")
        self.assertEqual(first["added"], 2)
        self.assertEqual(second["added"], 0)
        self.assertEqual(status(self.db)["discovered_places"], 2)
        self.assertEqual(first["received"], 2)

    def test_pages_and_cursor(self):
        for i in range(3):
            merge(self.db, {"places": [{"id": str(i), "name": "Plaats " + str(i)}]},
                  "2026-11-07T12:00:00Z")
        first = changes(self.db, 0, 2)
        self.assertTrue(first["has_more"])
        self.assertEqual([p["id"] for p in first["places"]], ["0", "1"])
        second = changes(self.db, first["cursor"], 2)
        self.assertFalse(second["has_more"])
        self.assertEqual([p["id"] for p in second["places"]], ["2"])

    def test_bad_catalog_and_payload_do_not_write(self):
        for payload in (
            {"catalog": "other", "places": []},
            {"places": [{"id": "", "name": "X"}]},
            {"places": [{"id": "x", "name": ""}]},
            {"places": [{"id": "x", "name": "OK"}, {"id": "", "name": "BAD"}]},
        ):
            with self.assertRaises(ValueError):
                merge(self.db, payload, "now")
        self.assertEqual(status(self.db)["discovered_places"], 0)

    def test_same_name_different_core_id_is_distinct(self):
        result = merge(self.db, {"places": [
            {"id": "a", "name": "Heerde"}, {"id": "b", "name": "Heerde"}]}, "now")
        self.assertEqual(result["added"], 2)

    def test_duplicate_id_first_accepted_metadata_is_preserved(self):
        merge(self.db, {"places": [{"id": "id", "name": "Oude naam"}]}, "old")
        merge(self.db, {"places": [{"id": "id", "name": "Nieuwe naam"}]}, "new")
        self.assertEqual(changes(self.db, 0)["places"][0]["name"], "Oude naam")


if __name__ == "__main__":
    unittest.main()
