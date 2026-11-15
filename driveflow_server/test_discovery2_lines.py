"""Safety tests for DriveFlow connected line server map preview."""
import hashlib
import gzip
import sys
import types
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discovery2_lines as lines
from road_discovery_store import CATALOG, create_tables, merge


class ConnectedLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index = lines.index_path(self.root)
        self.index.parent.mkdir()
        self.live = self.root / "driveflow_server.db"
        with sqlite3.connect(self.live) as db:
            create_tables(db)
            merge(db, {"catalog": CATALOG, "ids": ["A1-one"]},
                  "2026-11-15T21:00:00Z")
            db.commit()
        self.before = self.live.read_bytes()
        with sqlite3.connect(self.index) as db:
            lines._schema(db)
            props = {"road": "A1", "source_segment_ids": ["A1-one", "A1-two"],
                     "segment_count": 2}
            coords = [[5.0, 52.0], [5.001, 52.0], [5.002, 52.0]]
            db.execute("INSERT INTO lines VALUES (1,?,?,?)",
                       ("legacy", json.dumps(props), json.dumps(coords)))
            db.execute("INSERT INTO bounds VALUES (1,5.0,5.002,52.0,52.0)")
            db.execute("INSERT INTO lines VALUES (2,?,?,?)",
                       ("osm", json.dumps(props), json.dumps(coords)))
            db.execute("INSERT INTO bounds VALUES (2,5.0,5.002,52.0,52.0)")
            db.commit()

    def test_partial_line_mixed_discovery_does_not_turn_whole_line_green(self):
        result = lines.tiles(self.root, self.live, "legacy", (4.99, 51.99, 5.01, 52.01))
        parts = result["lines"][0]["parts"]
        self.assertEqual([part["state"] for part in parts], ["discovered", "undiscovered"])
        self.assertEqual([part["id"] for part in parts], ["A1-one", "A1-two"])
        self.assertEqual(self.before, self.live.read_bytes())

    def test_osm_never_auto_inherits_legacy_discovery(self):
        result = lines.tiles(self.root, self.live, "osm", (4.99, 51.99, 5.01, 52.01))
        self.assertEqual([p["state"] for p in result["lines"][0]["parts"]],
                         ["unverified", "unverified"])
        self.assertEqual(self.before, self.live.read_bytes())

    def test_bbox_and_limit_rejected(self):
        for bbox in [(0, 0, 1, 1), (5, 52, 4, 53), (float("nan"), 52, 5, 53)]:
            with self.assertRaises(ValueError):
                lines.tiles(self.root, self.live, "legacy", bbox)
        with self.assertRaises(ValueError):
            lines.tiles(self.root, self.live, "legacy", (5, 52, 5.1, 52.1), 10000)

    def test_staging_fails_for_wrong_content_without_overwriting(self):
        payload = b"invalid gz stream"
        checksum = hashlib.sha256(payload).hexdigest()
        with patch.dict(lines.LAYERS, {"legacy": ("synthetic.gz", len(payload),
                                                    checksum, 1, 1)}):
            out = lines.stage(io.BytesIO(payload), len(payload), checksum, self.root, "legacy")
            self.assertEqual(out["state"], "staged")
            self.assertEqual(lines.source_path(self.root, "legacy").read_bytes(), payload)
            with self.assertRaises(FileExistsError):
                lines.stage(io.BytesIO(payload), len(payload), checksum, self.root, "legacy")
            with self.assertRaises(ValueError):
                lines.stage(io.BytesIO(payload), len(payload), "0" * 64, self.root, "legacy")
        self.assertEqual(self.before, self.live.read_bytes())

    def test_build_connected_index_checks_cardinality_and_preserves_live(self):
        features = {
            "legacy": {"type": "Feature", "properties": {
                "road": "A1", "source_segment_ids": ["A1-one", "A1-two"]},
                "geometry": {"type": "LineString",
                             "coordinates": [[5.0, 52.0], [5.001, 52.0], [5.002, 52.0]]}},
            "osm": {"type": "Feature", "properties": {
                "road": "A1", "source_segment_ids": ["osm:first"]},
                "geometry": {"type": "LineString",
                             "coordinates": [[5.0, 52.0], [5.001, 52.0]]}}
        }
        for layer in ("legacy", "osm"):
            source = lines.source_path(self.root, layer)
            with gzip.open(source, "wt") as out:
                json.dump({"features": [features[layer]]}, out)
        self.index.unlink()
        def read_features(handle, *_args, **_kwargs):
            return iter(json.load(handle)["features"])
        stub = types.SimpleNamespace(items=read_features)
        tiny = {"legacy": ("roads_nl_legacy_connected.geojson.gz", 0, "ignored", 1, 2),
                "osm": ("roads_nl_osm_connected.geojson.gz", 0, "ignored", 1, 1)}
        with patch.dict(lines.LAYERS, tiny):
            with patch.object(lines, "_verified", return_value=True):
                with patch.dict(sys.modules, {"ijson": stub}):
                    lines._build(self.root)
        self.assertEqual(lines.status(self.root)["state"], "complete")
        self.assertEqual(lines.tiles(
            self.root, self.live, "legacy", (4.99, 51.99, 5.01, 52.01)
        )["lines"][0]["parts"][0]["state"], "discovered")
        self.assertEqual(self.before, self.live.read_bytes())

    def test_staging_truncated_fails_without_file(self):
        payload = b"abcdef"
        digest = hashlib.sha256(payload).hexdigest()
        with patch.dict(lines.LAYERS, {"legacy": ("synthetic.gz", 6, digest, 1, 1)}):
            with self.assertRaises(ValueError):
                lines.stage(io.BytesIO(b"abc"), 6, digest, self.root, "legacy")
            self.assertFalse(lines.source_path(self.root, "legacy").exists())


if __name__ == "__main__":
    unittest.main()
