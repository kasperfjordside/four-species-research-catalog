#!/usr/bin/env python3
"""Bounded OpenAlex discovery and conservative static GitHub Pages publication.

The published entrypoint remains docs/catalog.json. Further pages use file: cursors.
Existing publication records are never edited. A bounded newest-first scan is NOT
an exhaustive discovery scan. The workflow should deploy the entire docs directory.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector import SPECIES, convert, request_json
from publish_v3 import canonical, repair, VALID_ID

UTC = dt.timezone.utc
CATALOG_ID = 'four-species-research-catalog'
PAGE_SIZE = 4000
MAX_PAGE_RECORDS = 10000
MAX_PAGES = 20
MAX_TOTAL_RECORDS = 50000
MAX_PAGE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
PAGE_FILE = re.compile(r'catalog-page-([2-9][0-9]*)\.json\Z')


def page_name(number):
    return 'catalog.json' if number == 1 else f'catalog-page-{number}.json'


def load_baseline(root, state):
    """Read all existing pages, failing closed on broken chains or mismatches."""
    docs = root / 'docs'
    records = []
    seen = set()
    cursor = 'catalog.json'
    first = None
    total_bytes = 0
    for index in range(1, MAX_PAGES + 1):
        expected = page_name(index)
        if cursor != expected:
            raise ValueError(f'Unexpected catalog page: {cursor}')
        path = docs / cursor
        if not path.is_file():
            raise ValueError(f'Missing catalog page: {cursor}')
        raw = path.read_bytes()
        total_bytes += len(raw)
        if len(raw) > MAX_PAGE_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise ValueError('Existing catalog exceeds Android payload limits')
        page = json.loads(raw)
        if first is None:
            first = {k: page.get(k) for k in ('schema_version', 'catalog_id', 'catalog_version', 'generated_at')}
            if first['catalog_id'] != CATALOG_ID or first['catalog_version'] != state.get('catalog_version'):
                raise ValueError('Catalog identity/version mismatch')
            if first['generated_at'] != state.get('generated_at'):
                raise ValueError('Catalog generated_at and publisher state mismatch')
        elif any(page.get(k) != v for k, v in first.items()):
            raise ValueError('Inconsistent catalog page metadata')
        batch = page.get('records')
        if not isinstance(batch, list) or len(batch) > MAX_PAGE_RECORDS:
            raise ValueError('Invalid catalog page records')
        for rec in batch:
            rid = rec['id']
            if rid in seen:
                raise ValueError(f'Duplicate publication ID: {rid}')
            seen.add(rid)
            records.append(rec)
        token = page.get('next_cursor')
        if token is None:
            break
        if token != f'file:{page_name(index + 1)}':
            raise ValueError(f'Invalid static page cursor: {token!r}')
        cursor = token[5:]
    else:
        raise ValueError('Existing catalog exceeds 20 pages')
    if len(records) > MAX_TOTAL_RECORDS:
        raise ValueError('Existing catalog exceeds Android sync record limit')
    if len(seen) != len(state.get('entries', {})) or seen != set(state['entries']):
        raise ValueError('Baseline publisher state identity mismatch')
    # Detect stale orphan pages; never silently publish them alongside the active chain.
    expected_files = {page_name(i) for i in range(1, index + 1)}
    present = {'catalog.json'} | {p.name for p in docs.glob('catalog-page-*.json')}
    if present != expected_files:
        raise ValueError('Unexpected or orphan catalog page files')
    return first, records


def normalized_doi(record):
    raw = record.get('doi')
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().lower().removeprefix('https://doi.org/').removeprefix('http://doi.org/')


def build_pages(meta, records):
    """Construct and validate a complete snapshot before writing any files."""
    if len(records) > MAX_TOTAL_RECORDS:
        raise ValueError(f'Would exceed Android sync record limit: {len(records)}')
    ids = [r['id'] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate publication ID')
    # Keep the final snapshot strict: no duplicated DOI may be published.
    doi_owners = {}
    for rec in records:
        doi = normalized_doi(rec)
        if doi is None:
            continue
        if doi in doi_owners:
            first = doi_owners[doi]
            raise ValueError(
                f'Duplicate normalized DOI across catalog pages: DOI={doi!r}; '
                f'first_id={first!r}; second_id={rec["id"]!r}'
            )
        doi_owners[doi] = rec['id']
    if len(records) == 0:
        raise ValueError('Cannot publish empty catalog')
    batches = [records[i:i + PAGE_SIZE] for i in range(0, len(records), PAGE_SIZE)]
    if len(batches) > MAX_PAGES:
        raise ValueError('Would exceed Android page limit')
    result = {}
    total_bytes = 0
    for idx, batch in enumerate(batches, 1):
        next_cursor = f'file:{page_name(idx + 1)}' if idx < len(batches) else None
        page = dict(meta, next_cursor=next_cursor, records=batch)
        data = (canonical(page) + '\n').encode('utf-8')
        if len(batch) > MAX_PAGE_RECORDS or len(data) > MAX_PAGE_BYTES:
            raise ValueError(f'Would exceed Android per-page limit: {page_name(idx)}')
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError('Would exceed Android 64 MiB total sync limit')
        result[page_name(idx)] = data
    return result


def run(root, api_key, today=None, pages=3, dry_run=False):
    root = Path(root)
    docs = root / 'docs'
    state_path = root / 'publisher_state.json'
    discovery_path = root / 'discovery_state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    discovery = json.loads(discovery_path.read_text(encoding='utf-8')) if discovery_path.exists() else {}
    meta, records = load_baseline(root, state)
    existing = {r['id'] for r in records}
    today = today or dt.datetime.now(UTC).date()
    now = dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

    found = {}
    fetched = 0
    for scientific, phrases in SPECIES.items():
        for phrase in phrases:
            cursor = '*'
            for page_index in range(pages):
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
                if page_index + 1 == pages:
                    print(f'Bounded page cap reached for {scientific}: {phrase}; not an exhaustive scan')

    additions = []
    quarantine = []
    # Existing catalog records always win. Never alter their identifiers or metadata.
    doi_owners = {normalized_doi(r): r for r in records if normalized_doi(r)}
    for rid, raw in sorted(found.items()):
        if not VALID_ID.fullmatch(rid):
            quarantine.append({'id': rid, 'reason': 'invalid ID'})
            continue
        try:
            rec = repair(raw, [])
        except ValueError as exc:
            quarantine.append({'id': rid, 'reason': str(exc)})
            continue
        doi = normalized_doi(rec)
        if doi and doi in doi_owners:
            owner = doi_owners[doi]
            quarantine.append({
                'id': rid,
                'title': rec.get('title'),
                'doi': doi,
                'conflicting_id': owner['id'],
                'conflicting_title': owner.get('title'),
                'reason': 'duplicate_normalized_doi',
            })
            continue
        if doi:
            doi_owners[doi] = rec
        digest = hashlib.sha256(canonical(rec).encode('utf-8')).hexdigest()
        state['entries'][rid] = {'digest': digest, 'updated_at': now}
        rec['updated_at'] = now
        rec['metadata_source'] = rec.get('source_provider') or 'OpenAlex'
        rec['provenance'] = {
            'source': rec['metadata_source'],
            'source_reference': rec.get('source_record_url') or rec.get('source_url') or rid,
        }
        additions.append(rec)

    new_records = sorted(records + additions, key=lambda r: r['id'])
    if additions:
        meta['catalog_version'] += 1
        meta['generated_at'] = now
        state['catalog_version'] = meta['catalog_version']
        state['generated_at'] = now

    # Even on a no-change run, migrate the old one-file snapshot to static pages.
    page_payloads = build_pages(meta, new_records)
    changed_pages = {
        docs / name: data for name, data in page_payloads.items()
        if not (docs / name).exists() or (docs / name).read_bytes() != data
    }
    discovery.pop('last_successful_scan', None)
    discovery.update({
        'last_bounded_scan': today.isoformat(),
        'scan_mode': 'newest_first_bounded',
        'scan_complete': False,
        'last_added': len(additions),
        'last_quarantined': quarantine,
    })
    print(f'Bounded scan: fetched={fetched}; candidates={len(found)}; '
          f'added={len(additions)}; quarantined={len(quarantine)}; '
          f'pages={len(page_payloads)}; version={meta["catalog_version"]}')
    for item in quarantine:
        if item.get('reason') == 'duplicate_normalized_doi':
            print(f'DOI quarantine: {json.dumps(item, ensure_ascii=False, sort_keys=True)}')
    if dry_run:
        print('DRY RUN: no files written')
        return len(additions)

    changes = dict(changed_pages)
    if additions:
        changes[state_path] = (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    changes[discovery_path] = (json.dumps(discovery, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    # Stage every payload first. GitHub Pages deploys the resulting docs snapshot.
    temps = {}
    try:
        for path, data in changes.items():
            temp = path.with_suffix(path.suffix + '.tmp')
            temp.write_bytes(data)
            temps[path] = temp
        # Commit other pages before the entrypoint; a failed job never deploys Pages.
        for path in sorted(changes, key=lambda p: (p.name == 'catalog.json', str(p))):
            temps[path].replace(path)
    finally:
        for temp in temps.values():
            if temp.exists():
                temp.unlink()
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
