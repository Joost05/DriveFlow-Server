"""Tests for advisory Android synchronization preflight."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from discovery2_clients import record, status
from road_discovery_store import CATALOG


class ClientAttestationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.payload = {
            "catalog": CATALOG,
            "downloaded_cursor": 7,
            "pending_count": 0,
            "sync_complete": True,
        }

    def test_valid_handshake_is_advisory_only(self):
        result = record(self.folder, "12345678-aaaa-1111", self.payload, 7)
        self.assertTrue(result["acknowledged"])
        self.assertFalse(result["ready_for_switch"])
        current = status(self.folder, 7)
        self.assertEqual(current["registered_clients"], 1)
        self.assertEqual(current["clients_at_current_cursor"], 1)
        self.assertFalse(current["ready_for_switch"])
        self.assertFalse(current["pending_android_outbox_verified_empty"])
        stale = status(self.folder, 8)
        self.assertEqual(stale["clients_at_current_cursor"], 0)

    def test_pending_local_outbox_rejected(self):
        changed = dict(self.payload, pending_count=1, sync_complete=False)
        with self.assertRaises(ValueError):
            record(self.folder, "12345678", changed, 7)
        self.assertEqual(status(self.folder, 7)["registered_clients"], 0)

    def test_wrong_cursor_and_identity_rejected(self):
        with self.assertRaises(ValueError):
            record(self.folder, "12345678", self.payload, 8)
        with self.assertRaises(ValueError):
            record(self.folder, "bad", self.payload, 7)

    def test_boolean_cursor_not_accepted_and_record_can_be_replaced(self):
        bad = dict(self.payload, downloaded_cursor=True)
        with self.assertRaises(ValueError):
            record(self.folder, "12345678", bad, 7)
        record(self.folder, "12345678", self.payload, 7)
        update = dict(self.payload, downloaded_cursor=8)
        record(self.folder, "12345678", update, 8)
        self.assertEqual(status(self.folder, 8)["registered_clients"], 1)
        self.assertEqual(status(self.folder, 8)["clients_at_current_cursor"], 1)


if __name__ == "__main__":
    unittest.main()
