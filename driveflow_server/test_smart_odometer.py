"""Run with `python3 -m unittest test_smart_odometer` in the add-on source directory."""
import sqlite3
import unittest
from pathlib import Path
from smart_odometer import create_tables, store, records


class SmartOdometerTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        create_tables(self.db)
        self.row={'id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','vehicle':'SEAT Leon',
                  'time_ms':1793450000000,'km':123456.0,'source':'manual',
                  'ocr_text':'', 'estimated_km':123450.0,'difference_km':6.0}

    def tearDown(self):self.db.close()

    def test_duplicate_is_idempotent(self):
        first=store(self.db,Path('/tmp'),'device-123',self.row,'now')
        second=store(self.db,Path('/tmp'),'device-123',self.row,'now')
        self.assertFalse(first['already_present'])
        self.assertTrue(second['already_present'])
        self.assertEqual(len(records(self.db)),1)

    def test_conflicting_duplicate_rejected(self):
        store(self.db,Path('/tmp'),'device-123',self.row,'now')
        with self.assertRaises(ValueError):
            store(self.db,Path('/tmp'),'device-123',{**self.row,'km':123457.0},'now')

    def test_invalid_reading_rejected(self):
        with self.assertRaises(ValueError):
            store(self.db,Path('/tmp'),'device-123',{**self.row,'km':-1},'now')

    def test_no_trip_table_mutation(self):
        store(self.db,Path('/tmp'),'device-123',self.row,'now')
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('trips',tables)


if __name__=='__main__':unittest.main()
