
import datetime as dt
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from daily_update import run

BASE = Path(__file__).resolve().parents[1]


class DailyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'docs').mkdir()

        shutil.copy(
            BASE / 'docs/catalog.json',
            self.root / 'docs/catalog.json'
        )
        shutil.copy(
            BASE / 'publisher_state.json',
            self.root / 'publisher_state.json'
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_changes_preserve_catalog_bytes_and_version(self):
        before = (self.root / 'docs/catalog.json').read_bytes()

        with patch(
            'daily_update.request_json',
            return_value={'results': [], 'meta': {}}
        ):
            self.assertEqual(
                run(self.root, 'fake', today=dt.date(2026, 11, 2)),
                0
            )

        self.assertEqual(
            before,
            (self.root / 'docs/catalog.json').read_bytes()
        )

        catalog = json.loads(
            (self.root / 'docs/catalog.json').read_text()
        )
        state = json.loads(
            (self.root / 'publisher_state.json').read_text()
        )

        self.assertEqual(catalog['catalog_version'], 3)
        self.assertEqual(state['catalog_version'], 3)

    def test_new_record_appended(self):
        work = {
            'id': 'https://openalex.org/W999999999999',
            'title': 'A new study of Lutra lutra',
            'doi': None,
            'publication_year': 2026,
            'authorships': [],
            'abstract_inverted_index': None,
            'open_access': {'is_oa': False}
        }

        calls = 0

        def response(_):
            nonlocal calls
            calls += 1
            return {
                'results': [work] if calls == 1 else [],
                'meta': {}
            }

        with patch('daily_update.request_json', side_effect=response):
            self.assertEqual(
                run(self.root, 'fake', today=dt.date(2026, 11, 2)),
                1
            )

        catalog = json.loads(
            (self.root / 'docs/catalog.json').read_text()
        )

        self.assertEqual(catalog['catalog_version'], 4)
        self.assertEqual(len(catalog['records']), 9737)
        self.assertIn(
            'openalex:W999999999999',
            {r['id'] for r in catalog['records']}
        )

    def test_bounded_search_is_marked_incomplete(self):
        before = (self.root / 'docs/catalog.json').read_bytes()

        with patch(
            'daily_update.request_json',
            return_value={
                'results': [],
                'meta': {'next_cursor': 'next'}
            }
        ):
            result = run(
                self.root,
                'fake',
                today=dt.date(2026, 11, 2),
                pages=1
            )

        self.assertEqual(result, 0)

        # No publications were added or removed.
        self.assertEqual(
            before,
            (self.root / 'docs/catalog.json').read_bytes()
        )

        discovery = json.loads(
            (self.root / 'discovery_state.json').read_text()
        )

        self.assertEqual(
            discovery['scan_mode'],
            'newest_first_bounded'
        )
        self.assertFalse(discovery['scan_complete'])
        self.assertEqual(
            discovery['last_bounded_scan'],
            '2026-11-02'
        )

    def test_dry_run_does_not_write_files(self):
        before = (self.root / 'docs/catalog.json').read_bytes()

        with patch(
            'daily_update.request_json',
            return_value={'results': [], 'meta': {}}
        ):
            result = run(
                self.root,
                'fake',
                today=dt.date(2026, 11, 2),
                dry_run=True
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            before,
            (self.root / 'docs/catalog.json').read_bytes()
        )
        self.assertFalse(
            (self.root / 'discovery_state.json').exists()
        )


if __name__ == '__main__':
    unittest.main()
