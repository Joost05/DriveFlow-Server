"""Synthetic no-network tests for read-only old/new geometry comparison."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compare_discovery2_catalogs import candidate_score, compare, coordinates, match_segment, old_index


def line(identifier, lon1, lat1, lon2, lat2, road="N-weg"):
    return {"id": identifier, "road": road, "a": [lon1, lat1], "b": [lon2, lat2],
            "length_m": 70.0}


class CompareRoadCatalogTests(unittest.TestCase):
    def test_mismatched_segmentations_and_reverse_direction(self):
        old = line("old1", 5.0, 52.0, 5.004, 52.0)
        new = line("new1", 5.001, 52.0, 5.002, 52.0)
        self.assertIsNotNone(candidate_score(coordinates(new), coordinates(old)))
        reversed_new = line("reversed", 5.002, 52.0, 5.001, 52.0)
        self.assertIsNotNone(candidate_score(coordinates(reversed_new), coordinates(old)))

    def test_nearby_parallel_road_does_not_match(self):
        old = line("old1", 5.0, 52.0, 5.004, 52.0)
        parallel = line("new1", 5.001, 52.0002, 5.002, 52.0002)
        self.assertIsNone(candidate_score(coordinates(parallel), coordinates(old)))
        crossing = line("cross", 5.002, 51.9999, 5.002, 52.0001)
        self.assertIsNone(candidate_score(coordinates(crossing), coordinates(old)))

    def test_same_location_two_old_lines_is_ambiguous(self):
        with sqlite3.connect(":memory:") as db:
            segments = [
                line("a", 5, 52, 5.003, 52),
                line("b", 5, 52.00001, 5.003, 52.00001),
            ]
            with patch("compare_discovery2_catalogs.stream_segments", return_value=iter(segments)):
                old_index(db, Path("unused.json"))
            status, old_id, _ = match_segment(
                db, line("new", 5.001, 52.000005, 5.002, 52.000005))
            self.assertEqual(status, "ambiguous")
            self.assertIsNone(old_id)

    def test_report_no_migration_and_optional_discovered(self):
        old = [line("old", 5, 52, 5.004, 52)]
        new = [line("same", 5.001, 52, 5.002, 52),
               line("different", 5.02, 52, 5.021, 52)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old_path, new_path = root / "old.json", root / "new.json"
            old_path.write_text("{}", encoding="utf-8")
            new_path.write_text("{}", encoding="utf-8")
            ids = root / "discovered.json"
            ids.write_text(json.dumps({"ids": ["old"]}), encoding="utf-8")
            report = root / "report.json"
            mapping = root / "mapping.csv"

            def mock_stream(path):
                yield from old if path == old_path else new

            with patch("compare_discovery2_catalogs.stream_segments", side_effect=mock_stream):
                result = compare(old_path, new_path, report, ids, mapping)

            self.assertEqual(result["counts"]["candidate"], 1)
            self.assertEqual(result["counts"]["new"], 1)
            self.assertEqual(result["counts"]["new_segments_candidate_discovered"], 1)
            self.assertEqual(result["counts"]["old_discovered_ids_without_candidate"], 0)
            self.assertFalse(result["migration_performed"])
            self.assertFalse(result["safe_to_auto_migrate"])
            self.assertEqual(json.loads(report.read_text())["status"], "review_only")
            self.assertEqual(len(mapping.read_text().splitlines()), 3)
            with self.assertRaises(FileExistsError):
                compare(old_path, new_path, report)


if __name__ == "__main__":
    unittest.main()
