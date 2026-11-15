"""Unit tests for immutable Road Discovery 2.0 staging; no 248 MB fixture needed."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discovery2_upload as up


class StagingTests(unittest.TestCase):
    def test_size_or_digest_header_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream = io.BytesIO(b"xyz")
            with self.assertRaises(up.UploadError):
                up.receive_catalogue(stream, 3, root, up.EXPECTED_SHA256)
            self.assertEqual(stream.tell(), 0)
            with patch.object(up, "EXPECTED_BYTES", 3):
                with self.assertRaises(up.UploadError):
                    up.receive_catalogue(stream, 3, root, "wrong")
                self.assertEqual(stream.tell(), 0)

    def test_valid_upload_staged_and_never_overwritten(self):
        payload = b'{"revision":"test","segments":[]}'
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(up, "EXPECTED_BYTES", len(payload)), \
                 patch.object(up, "EXPECTED_SHA256", digest), \
                 patch.object(up, "free_space_ok", return_value=True):
                result = up.receive_catalogue(io.BytesIO(payload), len(payload), root, digest)
                self.assertEqual(result["state"], "staged")
                self.assertFalse(result["activated"])
                self.assertFalse(result["migration_performed"])
                with self.assertRaises(FileExistsError):
                    up.receive_catalogue(io.BytesIO(payload), len(payload), root, digest)
                self.assertEqual(up.catalogue_path(root).read_bytes(), payload)

    def test_truncated_stream_cleans_up_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(up, "EXPECTED_BYTES", 20), \
                 patch.object(up, "free_space_ok", return_value=True):
                with self.assertRaises(up.UploadError):
                    up.receive_catalogue(io.BytesIO(b"short"), 20, root, up.EXPECTED_SHA256)
            self.assertFalse(any(root.rglob("*.upload")))
            self.assertFalse(up.catalogue_path(root).exists())

    def test_invalid_checksum_cleans_up_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(up, "EXPECTED_BYTES", 3), \
                 patch.object(up, "free_space_ok", return_value=True):
                with self.assertRaises(up.UploadError):
                    up.receive_catalogue(io.BytesIO(b"abc"), 3, root, up.EXPECTED_SHA256)
            self.assertFalse(up.catalogue_path(root).exists())


if __name__ == "__main__":
    unittest.main()
