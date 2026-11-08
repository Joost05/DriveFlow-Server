"""End-to-end storage contract tests for DriveFlow Road Discovery sync.

Simulates two devices and an offline outbox using only Python and SQLite.
No Home Assistant installation, secrets or network connection required.
"""
import os
import sqlite3
import tempfile
import unittest

import road_discovery_store as roads
import discovery_places as places


class DiscoveryRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "driveflow_server.db")
        self.db = sqlite3.connect(self.path)
        roads.create_tables(self.db)
        places.create_tables(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_offline_replay_and_server_restart_preserve_both_catalogues(self):
        # While offline, Device A retains these IDs in its local outbox.
        pending_roads = ["A1:001", "N50:002", "A12:003"]
        pending_places = [("cbs-100", "Apeldoorn"), ("cbs-200", "Heerde")]
        # First upload: one retry after a lost HTTP acknowledgement.
        road_payload = {"catalog": roads.CATALOG, "source": "driveflow",
                        "ids": pending_roads}
        place_payload = {"catalog": places.CATALOG, "source": "driveflow",
                         "places": [{"id": ident, "name": name}
                                    for ident, name in pending_places]}
        first_roads = roads.merge(self.db, road_payload, "2026-11-08T10:00:00Z")
        first_places = places.merge(self.db, place_payload, "2026-11-08T10:00:00Z")
        self.db.commit()
        retry_roads = roads.merge(self.db, road_payload, "2026-11-08T10:01:00Z")
        retry_places = places.merge(self.db, place_payload, "2026-11-08T10:01:00Z")
        self.db.commit()
        self.assertEqual((first_roads["added"], first_places["added"]), (3, 2))
        self.assertEqual((retry_roads["added"], retry_places["added"]), (0, 0))
        self.assertEqual(retry_roads["received"], len(pending_roads))
        self.assertEqual(retry_places["received"], len(pending_places))

        self.db.close()
        self.db = sqlite3.connect(self.path)
        # Device B starts at cursor 0 after server restart.
        road_page = roads.changes(self.db, 0, 2)
        self.assertTrue(road_page["has_more"])
        road_tail = roads.changes(self.db, road_page["cursor"], 2)
        self.assertFalse(road_tail["has_more"])
        self.assertEqual(set(road_page["ids"] + road_tail["ids"]), set(pending_roads))
        place_page = places.changes(self.db, 0, 1)
        self.assertTrue(place_page["has_more"])
        place_tail = places.changes(self.db, place_page["cursor"], 1)
        self.assertFalse(place_tail["has_more"])
        self.assertEqual({p["id"] for p in place_page["places"] + place_tail["places"]},
                         {ident for ident, _ in pending_places})

        # Independent Device B discovers one additional segment and place.
        roads.merge(self.db, {"catalog": roads.CATALOG, "source": "driveflow",
                              "ids": ["N50:004", "A1:001"]}, "later")
        places.merge(self.db, {"catalog": places.CATALOG, "source": "driveflow",
                               "places": [{"id": "cbs-300", "name": "Zwolle"},
                                          {"id": "cbs-100", "name": "Apeldoorn"}]}, "later")
        self.db.commit()
        self.assertEqual(roads.status(self.db)["discovered_segments"], 4)
        self.assertEqual(places.status(self.db)["discovered_places"], 3)
        self.assertEqual(roads.changes(self.db, road_tail["cursor"])["ids"], ["N50:004"])
        self.assertEqual([p["id"] for p in places.changes(
            self.db, place_tail["cursor"])["places"]], ["cbs-300"])

    def test_invalid_batch_cannot_partially_commit(self):
        with self.assertRaises(ValueError):
            roads.merge(self.db, {"ids": ["A1:1", "bad space"]}, "now")
        with self.assertRaises(ValueError):
            places.merge(self.db, {"places": [
                {"id": "cbs-100", "name": "Apeldoorn"},
                {"id": "", "name": "Invalid"}
            ]}, "now")
        self.assertEqual(roads.status(self.db)["discovered_segments"], 0)
        self.assertEqual(places.status(self.db)["discovered_places"], 0)


if __name__ == "__main__":
    unittest.main()
