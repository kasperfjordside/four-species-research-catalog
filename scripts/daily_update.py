#!/usr/bin/env python3
"""Conservative OpenAlex daily updater for the existing Four-Species catalog.

No change to published files unless all validations succeed. Existing records are
immutable in this first version; newly discovered IDs are appended only.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector import SPECIES, convert, request_json
from publish_v3 import canonical, repair, VALID_ID

UTC = dt.timezone.utc
CATALOG_ID = 'four-species-research-catalog'
MAX_RECORDS = 10000  # existing Android single-page record cap
MAX_BYTES = 32 * 1024 * 1024


def run(root, api_key, today=None, pages=3, dry_run=False):
    root = Path(root)
    catalog_path = root / 'docs/catalog.json'
    state_path = root / 'publisher_state.json'
    discovery_path = root / 'discovery_state.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    state = json.loads(state_path.read_text(encoding='utf-8'))
    discovery = json.loads(discovery_path.read_text(encoding='utf-8')) if discovery_path.exists() else {}
    if catalog.get('catalog_id') != CATALOG_ID or state.get('catalog_id') != CATALOG_ID:
        raise ValueError('Catalog identity mismatch')
    if catalog['catalog_version'] != state['catalog_version']:
        raise ValueError('Publisher state and catalog version mismatch')
    records = catalog['records']
    existing = {r['id']: r for r in records}
    if len(existing) != len(records) or len(existing) != len(state['entries']):
        raise ValueError('Baseline identity count mismatch')
    if set(existing) != set(state['entries']):
        raise ValueError('Baseline publisher state identity mismatch')
    if len(records) > MAX_RECORDS:
        raise ValueError('Existing catalog exceeds Android record cap')
    today = today or dt.datetime.now(UTC).date()
    
# Free-plan discovery uses bounded, newest-first searches.
# No date-based checkpoint or paid OpenAlex sync filter is used.
# Publication-date sorting does not guarantee complete discovery.

    now = dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    found = {}
    fetched = 0
    for scientific, phrases in SPECIES.items():
        for phrase in phrases:
            cursor = '*'
            for page in range(pages):
                params = {
    'search.title_and_abstract': phrase,
    'sort': 'publication_date:desc',                
    'per_page': '100',
    'cursor': cursor,
    'api_key': api_key,
}
                url = 'https://api.openalex.org/works?' + urllib.parse.urlencode(params)
                result = request_json(url)
                works = result.get('results') or []
                fetched += len(works)
                for work in works:
                    rec = convert(work)
                    if rec and rec['id'] not in existing and rec['id'] not in found:
                        found[rec['id']] = rec
                cursor = (result.get('meta') or {}).get('next_cursor')
                if not works or not cursor:
                    break
                
                if page + 1 == pages:
                    # This is a bounded newest-first search, not a complete
                    # historical scan. Keep the page limit to control API usage.
                    print(
                        f"Search page limit reached for {scientific}: {phrase}. "
                        "Continuing with the records retrieved."
                )
                break

    additions = []
    quarantine = []
    for rid, raw in sorted(found.items()):
        if not VALID_ID.fullmatch(rid):
            quarantine.append({'id': rid, 'reason': 'invalid ID'})
            continue
        try:
            rec = repair(raw, [])
        except ValueError as exc:
            quarantine.append({'id': rid, 'reason': str(exc)})
            continue
        digest = hashlib.sha256(canonical(rec).encode('utf-8')).hexdigest()
        state['entries'][rid] = {'digest': digest, 'updated_at': now}
        rec['updated_at'] = now
        rec['metadata_source'] = rec.get('source_provider') or 'OpenAlex'
        rec['provenance'] = {'source': rec['metadata_source'], 'source_reference': rec.get('source_record_url') or rec.get('source_url') or rid}
        additions.append(rec)
    new_records = sorted(records + additions, key=lambda r: r['id'])
    if len(new_records) > MAX_RECORDS:
        raise ValueError(f'Would exceed Android single-page limit: {len(new_records)} records. Pagination needed; no files changed.')
    if additions:
        catalog['records'] = new_records
        catalog['catalog_version'] += 1
        catalog['generated_at'] = now
        state['catalog_version'] = catalog['catalog_version']
        state['generated_at'] = now
    payload = (canonical(catalog) + '\n').encode('utf-8')
    if len(payload) > MAX_BYTES:
        raise ValueError('Would exceed Android 32 MiB page limit; no files changed')
    if len(new_records) != len({r['id'] for r in new_records}):
        raise ValueError('Duplicate publication ID')
    
    print(
        f'Bounded newest-first scan; OpenAlex results={fetched};'
        f'candidates={len(found)}; added={len(additions)}; '
        f'quarantined={len(quarantine)}; '
        f'version={catalog["catalog_version"]}'
    )

    if dry_run:
        print('DRY RUN: no files written')
        return len(additions)
    # All validation has completed. Write through temporary files.
    changes = {}
    if additions:
        changes[catalog_path] = payload
        changes[state_path] = (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    
    # Record a bounded search, not a complete daily synchronization.
    discovery.pop('last_successful_scan', None)
    discovery['last_bounded_scan'] = today.isoformat()
    discovery['scan_mode'] = 'newest_first_bounded'
    discovery['scan_complete'] = False
    discovery['last_added'] = len(additions)
    discovery['last_quarantined'] = quarantine
    changes[discovery_path] = (
        json.dumps(discovery, ensure_ascii=False, indent=2) + '\n'
    ).encode('utf-8')

    for path, data in changes.items():
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_bytes(data)
    for path in changes:
        path.with_suffix(path.suffix + '.tmp').replace(path)
    return len(additions)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    parser.add_argument('--pages', type=int, default=3)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.pages <= 100:
        parser.error('--pages must be 1..100')
    key = os.environ.get('OPENALEX_API_KEY')
    if not key:
        parser.error('OPENALEX_API_KEY environment variable is required')
    try:
        run(args.root, key, pages=args.pages, dry_run=args.dry_run)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
