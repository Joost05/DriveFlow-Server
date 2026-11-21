"""Regression tests for official WKD archive discovery and safe catalog builds."""
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_speed_release


class OfficialArchiveDiscoveryTest(unittest.TestCase):
    def test_latest_2026_archive_is_selected(self):
        page = b'''<html><a href="01-10-2026/">October folder</a>
        <a href="01-10-2026.zip">October zip</a>
        <a href="01-11-2026/">November folder</a>
        <a href="01-11-2026.zip">November zip</a></html>'''
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, exc_type, exc_value, tb): self.close()
        with patch.object(build_speed_release, "urlopen", return_value=Response(page)):
            version, url = build_speed_release.discover_latest()
        self.assertEqual(version, "2026-11")
        self.assertEqual(url,
                         build_speed_release.INDEX + "01-11-2026.zip")

    def test_missing_archives_are_rejected(self):
        with self.assertRaises(FileNotFoundError):
            build_speed_release.extract_gpkg(Path("nonexistent.zip"),
                                             Path("output.gpkg"))


if __name__ == "__main__":
    unittest.main()
