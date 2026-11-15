"""Readiness must fail closed even when all preparatory data looks valid."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discovery2_readiness as readiness


class ReadinessTest(unittest.TestCase):
    def test_missing_everything_blocks_switch_and_play(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(readiness, "preview_status", return_value={"status": "not_built"}), \
                 patch.object(readiness, "client_status", return_value={
                     "registered_clients": 0, "clients_at_current_cursor": 0
                 }):
                result = readiness.evaluate(Path(folder), Path(folder) / "missing.db")
        self.assertFalse(result["ready_for_switch"])
        self.assertFalse(result["ready_for_google_play"])
        self.assertIn("catalogue_staged", result["blocking_gates"])
        self.assertIn("all_android_outboxes_drained", result["blocking_gates"])

    def test_complete_catalogue_report_and_preview_still_cannot_activate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(readiness, "staged_status", return_value={
                     "state": "staged", "revision": "road-discovery-2-osm-explicit-v1"
                 }), patch.object(readiness, "sha256_file", return_value=readiness.EXPECTED_SHA256), patch.object(readiness, "read_report", return_value={
                     "status": "review_only",
                     "counts": {"new_segments": 1_393_159}
                 }), patch.object(readiness, "preview_status", return_value={
                     "status": "preview_only", "legacy_cursor_processed": 0
                 }), patch.object(readiness, "client_status", return_value={
                     "registered_clients": 1, "clients_at_current_cursor": 1
                 }):
                result = readiness.evaluate(root, root / "missing.db")
        self.assertTrue(result["gates"]["catalogue_staged"])
        self.assertTrue(result["gates"]["comparison_completed"])
        self.assertFalse(result["gates"]["geographic_matches_validated"])
        self.assertFalse(result["ready_for_switch"])
        self.assertFalse(result["ready_for_google_play"])
        self.assertFalse(result["activated"])


if __name__ == "__main__":
    unittest.main()
