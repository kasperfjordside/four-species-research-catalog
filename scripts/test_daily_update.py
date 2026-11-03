"""Offline regression tests for static paginated catalog publishing."""
import datetime as dt
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from daily_update import run, load_baseline, build_pages, PAGE_SIZE, MAX_TOTAL_RECORDS

BASE = Path(__file__).resolve().parents[1]


class DailyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'docs').mkdir()
        shutil.copy(BASE / 'docs/catalog.json', self.root / 'docs/catalog.json')
        shutil.copy(BASE / 'publisher_state.json', self.root / 'publisher_state.json')

    def tearDown(self):
        self.tmp.cleanup()

    def catalog(self, name='catalog.json'):
        return json.loads((self.root / 'docs' / name).read_text())

    def state(self):
        return json.loads((self.root / 'publisher_state.json').read_text())

    def test_no_changes_migrate_to_pages_without_bumping_version(self):
        before = self.catalog()
        original_records = before['records']
        with patch('daily_update.request_json', return_value={'results': [], 'meta': {}}):
            self.assertEqual(run(self.root, 'fake', today=dt.date(2026, 11, 2)), 0)
        first = self.catalog()
        self.assertEqual(first['catalog_version'], before['catalog_version'])
        self.assertEqual(self.state()['catalog_version'], before['catalog_version'])
        self.assertEqual(first['next_cursor'], 'file:catalog-page-2.json')
        self.assertEqual(len(first['records']), PAGE_SIZE)
        _, recovered = load_baseline(self.root, self.state())
        self.assertEqual({r['id'] for r in recovered}, {r['id'] for r in original_records})
        self.assertEqual(len(recovered), len(original_records))
        self.assertEqual(len(recovered), 9736)

    def test_new_record_appended_after_migration(self):
        work = {'id': 'https://openalex.org/W999999999999', 'title': 'A new study of Lutra lutra',
                'doi': None, 'publication_year': 2026, 'authorships': [],
                'abstract_inverted_index': None, 'open_access': {'is_oa': False}}
        calls = 0
        def response(_):
            nonlocal calls
            calls += 1
            return {'results': [work] if calls == 1 else [], 'meta': {}}
        with patch('daily_update.request_json', side_effect=response):
            self.assertEqual(run(self.root, 'fake', today=dt.date(2026, 11, 2)), 1)
        meta, records = load_baseline(self.root, self.state())
        self.assertEqual(meta['catalog_version'], 4)
        self.assertEqual(len(records), 9737)
        self.assertIn('openalex:W999999999999', {r['id'] for r in records})

    def test_repeat_run_preserves_pages_and_version(self):
        with patch('daily_update.request_json', return_value={'results': [], 'meta': {}}):
            run(self.root, 'fake')
            before = {p.name: p.read_bytes() for p in (self.root / 'docs').glob('*.json')}
            version = self.state()['catalog_version']
            run(self.root, 'fake')
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / 'docs').glob('*.json')})
        self.assertEqual(version, self.state()['catalog_version'])

    def test_failed_api_does_not_write_anything(self):
        before = (self.root / 'docs/catalog.json').read_bytes()
        with patch('daily_update.request_json', side_effect=ValueError('API failed')):
            with self.assertRaisesRegex(ValueError, 'API failed'):
                run(self.root, 'fake')
        self.assertEqual(before, (self.root / 'docs/catalog.json').read_bytes())
        self.assertFalse((self.root / 'discovery_state.json').exists())

    def test_dry_run_does_not_write(self):
        before = (self.root / 'docs/catalog.json').read_bytes()
        with patch('daily_update.request_json', return_value={'results': [], 'meta': {}}):
            self.assertEqual(run(self.root, 'fake', dry_run=True), 0)
        self.assertEqual(before, (self.root / 'docs/catalog.json').read_bytes())
        self.assertFalse((self.root / 'docs/catalog-page-2.json').exists())

    def test_10499_records_across_three_pages(self):
        original = self.catalog()
        records = original['records'] + [dict(original['records'][0], id=f'openalex:W{9000000000+i}') for i in range(763)]
        payloads = build_pages({k: original[k] for k in ('catalog_id', 'catalog_version', 'generated_at', 'schema_version')}, records)
        self.assertEqual(list(payloads), ['catalog.json', 'catalog-page-2.json', 'catalog-page-3.json'])
        self.assertEqual([len(json.loads(x)['records']) for x in payloads.values()], [4000, 4000, 2499])
        self.assertEqual(json.loads(payloads['catalog.json'])['next_cursor'], 'file:catalog-page-2.json')
        self.assertEqual(json.loads(payloads['catalog-page-3.json'])['next_cursor'], None)

    def test_duplicate_ids_rejected(self):
        rec = self.catalog()['records'][0]
        with self.assertRaisesRegex(ValueError, 'Duplicate publication ID'):
            build_pages(self.catalog(), [rec, rec])

    def test_duplicate_doi_across_pages_rejected(self):
        original = self.catalog()
        records = original['records'][:4001]
        duplicated = dict(records[-1], id='openalex:W999999999998', doi=records[0].get('doi'))
        if not duplicated['doi']:
            duplicated['doi'] = 'https://doi.org/10.1234/test'
            records[0] = dict(records[0], doi=duplicated['doi'])
        records[-1] = duplicated
        with self.assertRaisesRegex(ValueError, 'Duplicate normalized DOI'):
            build_pages(original, records)

    def test_new_doi_conflict_is_quarantined_and_existing_record_preserved(self):
        original = self.catalog()
        owner = next(r for r in original['records'] if r.get('doi'))
        work = {'id': 'https://openalex.org/W999999999997',
                'title': 'Different Lutra lutra publication with same DOI',
                'doi': 'https://doi.org/' + owner['doi'],
                'publication_year': 2026, 'authorships': [],
                'abstract_inverted_index': None, 'open_access': {'is_oa': False}}
        with patch('daily_update.request_json', return_value={'results': [work], 'meta': {}}):
            self.assertEqual(run(self.root, 'fake'), 0)
        _, recovered = load_baseline(self.root, self.state())
        self.assertEqual(len(recovered), len(original['records']))
        self.assertEqual(next(r for r in recovered if r['id'] == owner['id']), owner)
        self.assertNotIn('openalex:W999999999997', self.state()['entries'])
        discovery = json.loads((self.root / 'discovery_state.json').read_text())
        conflicts = [q for q in discovery['last_quarantined'] if q['reason'] == 'duplicate_normalized_doi']
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['conflicting_id'], owner['id'])
        self.assertEqual(conflicts[0]['doi'], owner['doi'])
        self.assertEqual(conflicts[0]['title'], work['title'])
        self.assertEqual(conflicts[0]['conflicting_title'], owner['title'])

    def test_two_new_works_with_same_doi_quarantines_second(self):
        works = [
            {'id': 'https://openalex.org/W999999999991', 'title': 'First new Lutra lutra work',
             'doi': 'https://doi.org/10.9999/new-test', 'publication_year': 2026,
             'authorships': [], 'abstract_inverted_index': None, 'open_access': {'is_oa': False}},
            {'id': 'https://openalex.org/W999999999992', 'title': 'Second new Lutra lutra work',
             'doi': 'https://doi.org/10.9999/new-test', 'publication_year': 2026,
             'authorships': [], 'abstract_inverted_index': None, 'open_access': {'is_oa': False}},
        ]
        with patch('daily_update.request_json', return_value={'results': works, 'meta': {}}):
            self.assertEqual(run(self.root, 'fake'), 1)
        _, recovered = load_baseline(self.root, self.state())
        self.assertEqual(len(recovered), 9737)
        discovery = json.loads((self.root / 'discovery_state.json').read_text())
        conflicts = [q for q in discovery['last_quarantined'] if q['reason'] == 'duplicate_normalized_doi']
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['conflicting_id'], 'openalex:W999999999991')
        self.assertEqual(conflicts[0]['id'], 'openalex:W999999999992')

    def test_dry_run_conflict_writes_nothing(self):
        original = (self.root / 'docs/catalog.json').read_bytes()
        owner = next(r for r in self.catalog()['records'] if r.get('doi'))
        work = {'id': 'https://openalex.org/W999999999996', 'title': 'Lutra lutra conflict',
                'doi': 'https://doi.org/' + owner['doi'], 'publication_year': 2026,
                'authorships': [], 'abstract_inverted_index': None, 'open_access': {'is_oa': False}}
        with patch('daily_update.request_json', return_value={'results': [work], 'meta': {}}):
            self.assertEqual(run(self.root, 'fake', dry_run=True), 0)
        self.assertEqual((self.root / 'docs/catalog.json').read_bytes(), original)
        self.assertFalse((self.root / 'discovery_state.json').exists())
        self.assertFalse((self.root / 'docs/catalog-page-2.json').exists())

    def test_excessive_total_records_rejected(self):
        rec = self.catalog()['records'][0]
        with self.assertRaisesRegex(ValueError, 'record limit'):
            build_pages(self.catalog(), [rec] * (MAX_TOTAL_RECORDS + 1))

    def test_missing_page_rejected(self):
        with patch('daily_update.request_json', return_value={'results': [], 'meta': {}}):
            run(self.root, 'fake')
        (self.root / 'docs/catalog-page-2.json').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing catalog page'):
            load_baseline(self.root, self.state())

    def test_inconsistent_page_version_rejected(self):
        with patch('daily_update.request_json', return_value={'results': [], 'meta': {}}):
            run(self.root, 'fake')
        path = self.root / 'docs/catalog-page-2.json'
        page = json.loads(path.read_text())
        page['catalog_version'] += 1
        path.write_text(json.dumps(page))
        with self.assertRaisesRegex(ValueError, 'Inconsistent catalog page'):
            load_baseline(self.root, self.state())

    def test_bounded_search_is_marked_incomplete(self):
        with patch('daily_update.request_json', return_value={'results': [], 'meta': {'next_cursor': 'next'}}):
            run(self.root, 'fake', today=dt.date(2026, 11, 2), pages=1)
        discovery = json.loads((self.root / 'discovery_state.json').read_text())
        self.assertEqual(discovery['scan_mode'], 'newest_first_bounded')
        self.assertFalse(discovery['scan_complete'])


if __name__ == '__main__':
    unittest.main()
