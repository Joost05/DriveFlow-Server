"""Exercise server HTTP handler contracts without opening a network port."""
import io
import json
import sqlite3
import threading
import unittest
import uuid
from unittest.mock import patch

import server
import road_discovery_store as roads
import discovery_places as places


class _Request:
    def __init__(self, path, payload=None, authenticated=True):
        self.path = path
        self._authenticated = authenticated
        self.response = None
        self.body = json.dumps(payload).encode() if payload is not None else b""
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.body)),
        }
        self.rfile = io.BytesIO(self.body)

    def authenticated(self):
        if not self._authenticated:
            self.json(401, {"error": "Unauthorized"})
        return self._authenticated

    def json(self, status, body):
        self.response = status, body


class DiscoveryHttpTest(unittest.TestCase):
    def setUp(self):
        self.db_uri = f"file:road-discovery-http-{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.hold = sqlite3.connect(self.db_uri, uri=True, check_same_thread=False)
        roads.create_tables(self.hold)
        places.create_tables(self.hold)
        self.hold.commit()

        def open_db():
            return sqlite3.connect(self.db_uri, uri=True, check_same_thread=False)

        self.connect_patch = patch("server.connect_db", side_effect=open_db)
        self.lock_patch = patch("server.LOCK", threading.RLock())
        self.connect_patch.start()
        self.lock_patch.start()

    def tearDown(self):
        self.lock_patch.stop()
        self.connect_patch.stop()
        self.hold.close()

    def send(self, path, payload=None, authorized=True):
        request = _Request(path, payload, authorized)
        if payload is None:
            server.Api.do_GET(request)
        else:
            server.Api.do_POST(request)
        return request.response

    def test_acknowledged_upload_replays_and_cursor_download(self):
        road_batch = {"catalog": roads.CATALOG, "source": "driveflow",
                      "ids": ["A1:001", "N50:002"]}
        place_batch = {"catalog": places.CATALOG, "source": "driveflow",
                       "places": [{"id": "cbs-123", "name": "Apeldoorn"}]}
        first = self.send("/v1/discovery/merge", road_batch)
        retry = self.send("/v1/discovery/merge", road_batch)
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1]["received"], 2)
        self.assertEqual(first[1]["added"], 2)
        self.assertEqual(retry[1]["received"], 2)
        self.assertEqual(retry[1]["added"], 0)
        self.assertEqual(self.send("/v1/discovery/status")[1]["discovered_segments"], 2)
        self.assertEqual(self.send("/v1/discovery/changes?since=0&limit=1")[1]["ids"], ["A1:001"])
        self.assertEqual(self.send("/v1/discovery/places/merge", place_batch)[1]["received"], 1)
        self.assertEqual(self.send("/v1/discovery/places/status")[1]["discovered_places"], 1)
        self.assertEqual(self.send("/v1/discovery/places/changes?since=0")[1]["places"],
                         [{"id": "cbs-123", "name": "Apeldoorn"}])

    def test_bad_catalog_rejected_before_any_write(self):
        status, _ = self.send("/v1/discovery/merge", {"catalog": "wrong", "ids": ["A1:1"]})
        self.assertEqual(status, 422)
        self.assertEqual(self.send("/v1/discovery/status")[1]["discovered_segments"], 0)

    def test_unauthorized_reads_and_writes(self):
        self.assertEqual(self.send("/v1/discovery/status", authorized=False)[0], 401)
        self.assertEqual(self.send("/v1/discovery/merge",
                                   {"ids": ["A1:1"]}, authorized=False)[0], 401)
        self.assertEqual(self.send("/v1/discovery/status")[1]["discovered_segments"], 0)


if __name__ == "__main__":
    unittest.main()
