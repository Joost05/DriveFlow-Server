import sqlite3
import unittest

from road_discovery_store import CATALOG, changes, create_tables, merge, status


class DiscoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        create_tables(self.db)

    def tearDown(self):
        self.db.close()

    def test_home_assistant_import_and_idempotent_replay(self):
        payload = {'catalog': CATALOG, 'source': 'home_assistant',
                   'ids': ['A50:abc', 'N35:def', 'A50:abc']}
        first = merge(self.db, payload, '2026-11-07T12:00:00Z')
        self.assertEqual(first['added'], 2)
        second = merge(self.db, payload, '2026-11-07T12:00:01Z')
        self.assertEqual(second['added'], 0)
        self.assertEqual(status(self.db)['discovered_segments'], 2)

    def test_incremental_sync_and_page_cursor(self):
        merge(self.db, {'ids': ['A7:a', 'A7:b', 'A7:c']}, 'now')
        first = changes(self.db, 0, 2)
        self.assertEqual(first['ids'], ['A7:a', 'A7:b'])
        self.assertTrue(first['has_more'])
        second = changes(self.db, first['cursor'], 2)
        self.assertEqual(second['ids'], ['A7:c'])
        self.assertFalse(second['has_more'])

    def test_reject_untrusted_or_oversized_input_without_changes(self):
        invalid = ({'catalog': 'other-map', 'ids': ['A7:a']},
                   {'ids': ['bad space']}, {'ids': ['A7:a'] * 5001},
                   {'ids': 'A7:a'}, {'source': 'user space', 'ids': ['A7:a']})
        for payload in invalid:
            with self.assertRaises(ValueError):
                merge(self.db, payload, 'now')
        self.assertEqual(status(self.db)['discovered_segments'], 0)

    def test_reject_negative_cursor_and_incompatible_catalog(self):
        with self.assertRaises(ValueError):
            changes(self.db, -1)
        with self.assertRaises(ValueError):
            changes(self.db, 0, catalog='wrong')


if __name__ == '__main__':
    unittest.main()
