"""Verify isolated, authenticated OSM identity storage never mutates legacy DB."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import discovery2_osm_discoveries as osm
from discovery2_lines import _schema, index_path


class OsmDiscoveriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        index = index_path(self.root)
        index.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(index) as db:
            _schema(db)
            db.execute("INSERT INTO lines(fid,layer,properties,coordinates) VALUES(?,?,?,?)",
                (10,"osm",json.dumps({"source_segment_ids":["osm:a","osm:b"]}),
                 json.dumps([[5,52],[5.001,52],[5.002,52]])))
            db.execute("INSERT INTO bounds VALUES(10,5,5.002,52,52)")
            db.execute("INSERT INTO lines(fid,layer,properties,coordinates) VALUES(?,?,?,?)",
                (11,"legacy",json.dumps({"source_segment_ids":["legacy:a"]}),
                 json.dumps([[5,52],[5.001,52]])))
            db.execute("INSERT INTO bounds VALUES(11,5,5.001,52,52)")
            db.commit()
        self.legacy = self.root/"driveflow_server.db"
        self.legacy.write_bytes(b"legacy DB must remain untouched")
        self.before = self.legacy.read_bytes()

    def test_merge_idempotent_and_cursor(self):
        batch = {"namespace":"osm","segments":[
            {"id":"osm:a","line_id":10},
            {"id":"osm:b","line_id":10}]}
        first = osm.merge(self.root,batch,"test-device","2026-11-18T10:00:00Z")
        self.assertEqual((first["received"],first["added"]),(2,2))
        second = osm.merge(self.root,batch,"test-device","2026-11-18T10:00:00Z")
        self.assertEqual((second["received"],second["added"]),(2,0))
        page = osm.changes(self.root,0,1)
        self.assertEqual(page["ids"],["osm:a"])
        self.assertTrue(page["has_more"])
        last = osm.changes(self.root,page["cursor"],1)
        self.assertEqual(last["ids"],["osm:b"])
        self.assertFalse(last["has_more"])
        self.assertEqual(osm.status(self.root)["discovered_segments"],2)
        self.assertEqual(self.legacy.read_bytes(),self.before)

    def test_legacy_or_wrong_osm_line_rejected_without_partial_write(self):
        for candidate in [
            {"id":"legacy:a","line_id":11},
            {"id":"osm:missing","line_id":10},
            {"id":"osm:a","line_id":11},
        ]:
            with self.assertRaises(ValueError):
                osm.merge(self.root,{"namespace":"osm","segments":[
                    {"id":"osm:a","line_id":10},candidate]},
                    "test-device","2026-11-18T10:00:00Z")
        self.assertEqual(osm.status(self.root)["discovered_segments"],0)
        self.assertEqual(self.legacy.read_bytes(),self.before)

    def test_no_legacy_inheritance_from_shared_geometry(self):
        self.assertEqual(osm.status(self.root)["discovered_segments"],0)
        self.assertEqual(osm.changes(self.root,0)["ids"],[])
        self.assertEqual(self.legacy.read_bytes(),self.before)


if __name__ == "__main__":
    unittest.main()
