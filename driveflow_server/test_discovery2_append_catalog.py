"""Unit tests for append-only independent Road Discovery catalogues."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discovery2_append_catalog as store
from road_discovery_store import CATALOG, create_tables, merge


def make_catalogue(path, segments):
    path.write_text(json.dumps({"segments": segments}), encoding="utf-8")


def segment(sid, road):
    return {"id": sid, "road": road, "a": [5.1, 52.1], "b": [5.2, 52.2]}


class AppendCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.legacy = self.root / "old.json"
        self.osm = self.root / "new.json"
        make_catalogue(self.legacy, [segment("same-id", "A1"), segment("old-only", "N50")])
        make_catalogue(self.osm, [segment("same-id", "OSM road"), segment("new-only", "OSM 7")])
        self.live = self.root / "driveflow_server.db"
        with sqlite3.connect(self.live) as db:
            create_tables(db)
            merge(db, {"catalog": CATALOG, "ids": ["same-id", "old-only"]},
                  "2026-11-15T12:00:00+00:00")
            db.commit()
        self.before = self.live.read_bytes()
        self.reader = patch.object(store, "stream_segments",
                                   side_effect=lambda p: iter(json.loads(p.read_text())["segments"]))
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def test_independent_namespace_same_id_both_survive_and_live_db_untouched(self):
        result = store.build(self.root, self.legacy, self.osm, check_pinned=False)
        self.assertEqual(result["old_segments_deleted"], 0)
        self.assertEqual(result["counts_by_namespace"], {"legacy": 2, "osm": 2})
        self.assertFalse(result["physical_road_deduplication_verified"])
        self.assertFalse(result["activated"])
        self.assertEqual(self.live.read_bytes(), self.before)
        path = self.root / "discovery2" / "append_catalog.sqlite"
        with sqlite3.connect(path) as db:
            roads = db.execute(
                "SELECT namespace,payload FROM catalog_segments WHERE segment_id='same-id' "
                "ORDER BY namespace"
            ).fetchall()
            self.assertEqual(len(roads), 2)
            self.assertNotEqual(roads[0][1], roads[1][1])
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM catalog_segments WHERE namespace='legacy'"
            ).fetchone()[0], 2)
            for statement in (
                "DELETE FROM catalog_segments WHERE namespace='legacy'",
                "UPDATE catalog_segments SET payload='{}' WHERE namespace='legacy'",
                "DELETE FROM catalog_imports",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)
                db.rollback()

    def test_atomic_import_idempotent_and_duplicate_conflict_rollback(self):
        path = self.root / "test.sqlite"
        with sqlite3.connect(path) as db:
            store._db_setup(db)
            first = store.append_file(db, self.legacy, namespace="legacy",
                                      revision="rev1", checksum="digest1")
            self.assertEqual(first["added"], 2)
            repeat = store.append_file(db, self.legacy, namespace="legacy",
                                       revision="rev1", checksum="digest1")
            self.assertEqual(repeat["added"], 0)
            with self.assertRaises(ValueError):
                store.append_file(db, self.legacy, namespace="legacy",
                                  revision="rev2", checksum="digest2")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM catalog_segments").fetchone()[0], 2)

    def test_bad_source_duplicate_id_fails_without_partial_records(self):
        make_catalogue(self.osm, [segment("x", "a"), segment("x", "b")])
        path = self.root / "test.sqlite"
        with sqlite3.connect(path) as db:
            store._db_setup(db)
            with self.assertRaises(ValueError):
                store.append_file(db, self.osm, namespace="osm",
                                  revision="r", checksum="hash")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM catalog_segments").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM catalog_imports").fetchone()[0], 0)

    def test_invalid_hash_and_no_overwriting_existing_build(self):
        with self.assertRaises(ValueError):
            store.build(self.root, self.legacy, self.osm)
        self.assertEqual(store.status(self.root)["status"], "not_built")
        store.build(self.root, self.legacy, self.osm, check_pinned=False)
        with self.assertRaises(FileExistsError):
            store.build(self.root, self.legacy, self.osm, check_pinned=False)
        self.assertEqual(store.status(self.root)["counts_by_namespace"]["legacy"], 2)
        self.assertEqual(self.live.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
