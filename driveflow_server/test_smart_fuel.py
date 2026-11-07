"""Run `python3 -B -m unittest test_smart_fuel` in the server folder."""
import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from smart_fuel import create_tables, save, records


class SmartFuelTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        create_tables(self.db)
        self.temp=tempfile.TemporaryDirectory()
        self.payload={
            'id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            'fuel_id':12,'vehicle':'SEAT Leon','time_ms':1793450000000,
            'station':'Test Tankstation','source':'camera','liters':43.2,
            'total_eur':79.99,'unit_price':79.99/43.2,'odometer_km':123456.0,
            'eur_per_km':0.14,'fuel_type':'Euro 95 (E10)','ocr_text':'originele herkenning',
            'photo_extension':'jpg','photo_base64':base64.b64encode(b'original-jpeg-content').decode('ascii')}

    def tearDown(self):self.temp.cleanup();self.db.close()

    def test_idempotent_and_photo_preserved(self):
        base=Path(self.temp.name)
        first=save(self.db,base,'test-device',self.payload,'now')
        second=save(self.db,base,'test-device',self.payload,'later')
        self.assertFalse(first['already_present']);self.assertTrue(second['already_present'])
        self.assertEqual(len(records(self.db)),1)
        self.assertEqual((base/'fuel_receipts'/'test-device'/(self.payload['id']+'.jpg')).read_bytes(),b'original-jpeg-content')

    def test_conflicting_id_rejected(self):
        base=Path(self.temp.name);save(self.db,base,'test-device',self.payload,'now')
        with self.assertRaises(ValueError):
            save(self.db,base,'test-device',{**self.payload,'total_eur':81.0},'now')

    def test_invalid_amount_rejected(self):
        with self.assertRaises(ValueError):
            save(self.db,Path(self.temp.name),'test-device',{**self.payload,'liters':-1},'now')


if __name__=='__main__':unittest.main()
