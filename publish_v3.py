#!/usr/bin/env python3
"""Validated version-1 feed publisher. Does not edit source OpenAlex batches."""
import argparse
import datetime as dt
import hashlib
import html
import json
import pathlib
import re
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit

UTC = dt.timezone.utc
SPECIES = {'Lutra lutra', 'Phoca vitulina', 'Halichoerus grypus', 'Phocoena phocoena'}
VALID_ID = re.compile(r'^[A-Za-z0-9._:-]{1,200}$')
DOI_PATTERN = re.compile(r'^10\.[0-9]{4,9}/\S+$', re.I)
BAD_HTML = re.compile(r'<[^>]*>|&(?:lt|gt|quot|amp|#\d+);', re.I)
PERCENT_BAD = re.compile(r'%(?![0-9A-Fa-f]{2})')
URL_FIELDS = ('source_url', 'oa_url', 'source_record_url')


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def safe_url(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or BAD_HTML.search(value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ('http', 'https') or not parts.netloc:
            return None
        if any(c.isspace() for c in parts.netloc) or '@' in parts.netloc:
            return None
        if PERCENT_BAD.search(value):
            # A literal % is escaped without changing existing valid %HH escapes.
            value = PERCENT_BAD.sub('%25', value)
            parts = urlsplit(value)
        path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~%")
        query = quote(parts.query, safe="=&/?%:@!$'()*+,;~-._")
        fragment = quote(parts.fragment, safe="/?%:@!$&'()*+,;=-._~")
        result = urlunsplit((parts.scheme, parts.netloc, path, query, fragment))
        return result if not any(c.isspace() for c in result) else None
    except ValueError:
        return None


DOI_HTML_TAG = re.compile(
    r'<!--|<!DOCTYPE\b|<\s*/?\s*[A-Za-z][A-Za-z0-9:_-]*(?=\s|/?>|$)',
    re.I,
)


def doi_contains_html(value):
    """Inspect decoded copies only; never repair or replace the source DOI."""
    # Preserve the publisher's existing raw-markup quarantine policy.
    if BAD_HTML.search(value):
        return True
    inspected = value
    while True:
        # Also catches incomplete fragments such as '</a' without a closing '>'.
        if DOI_HTML_TAG.search(inspected):
            return True
        decoded = html.unescape(unquote(inspected))
        if decoded == inspected:
            return False
        inspected = decoded


def normalize_doi(raw):
    if raw is None or raw == '':
        return None
    if not isinstance(raw, str):
        raise ValueError('DOI is not a string')
    doi = raw.strip()
    if doi_contains_html(doi):
        raise ValueError('HTML-contaminated DOI')
    doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi, flags=re.I)
    doi = re.sub(r'^doi:', '', doi, flags=re.I).strip()
    if not DOI_PATTERN.fullmatch(doi):
        raise ValueError('DOI fails syntax validation')
    return doi.lower()


def repair(raw, audit):
    rec = dict(raw)
    rid = str(rec['id'])
    doi = normalize_doi(rec.get('doi'))
    # Preserve source DOI representation if syntactically valid; the client owns identity.
    for field in URL_FIELDS:
        if field not in rec:
            continue
        old = rec[field]
        new = safe_url(old)
        if new != old:
            audit.append({'id': rid, 'field': field, 'action': 'encoded' if new else 'set_null',
                          'original': old, 'replacement': new})
            rec[field] = new
    if doi:
        resolver = 'https://doi.org/' + quote(doi, safe="/-._~!$&'()*+,;=:@%")
        # Source URLs produced from a DOI should use a properly escaped resolver.
        old = rec.get('source_url')
        if isinstance(old, str) and re.match(r'^https?://(?:dx\.)?doi\.org/', old, re.I):
            if old != resolver:
                audit.append({'id': rid, 'field': 'source_url', 'action': 'encoded_doi_resolver',
                              'original': old, 'replacement': resolver})
            rec['source_url'] = resolver
    for field in ('source_urls',):
        if field in rec and rec[field] is not None:
            if not isinstance(rec[field], list):
                raise ValueError(f'{field} must be an array')
            fixed = []
            for idx, item in enumerate(rec[field]):
                new = safe_url(item)
                if new != item:
                    audit.append({'id': rid, 'field': f'{field}[{idx}]',
                                  'action': 'encoded' if new else 'set_null',
                                  'original': item, 'replacement': new})
                if new is not None:
                    fixed.append(new)
            rec[field] = fixed
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input_dir', type=pathlib.Path)
    ap.add_argument('--out', type=pathlib.Path, default=pathlib.Path('docs/catalog.json'))
    ap.add_argument('--state', type=pathlib.Path, default=pathlib.Path('publisher_state.json'))
    ap.add_argument('--audit', type=pathlib.Path, default=pathlib.Path('publisher_audit.json'))
    args = ap.parse_args()
    paths = sorted(args.input_dir.glob('four_species_import_*.json'))
    if not paths:
        ap.error('No four_species_import_*.json files found')
    raw_records = {}
    for path in paths:
        batch = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(batch, list):
            raise ValueError(f'{path}: expected JSON array')
        for index, raw in enumerate(batch):
            if not isinstance(raw, dict) or not isinstance(raw.get('title'), str) or not raw['title'].strip():
                raise ValueError(f'{path}:{index}: missing title')
            rid = str(raw.get('id') or '')
            if not VALID_ID.fullmatch(rid):
                raise ValueError(f'{path}:{index}: invalid ID {rid!r}')
            if not isinstance(raw.get('species') or [], list) or any(s not in SPECIES for s in (raw.get('species') or [])):
                raise ValueError(f'{path}:{index}: invalid species')
            if rid in raw_records and canonical(raw_records[rid]) != canonical(raw):
                raise ValueError(f'Conflicting duplicate ID: {rid}')
            raw_records[rid] = raw
    if len(raw_records) > 10000:
        raise ValueError('More than 10,000 records; pagination required')
    old = json.loads(args.state.read_text(encoding='utf-8')) if args.state.exists() else {}
    catalog_id = 'four-species-research-catalog'
    if old and old.get('catalog_id') != catalog_id:
        raise ValueError('Catalog ID changed')
    now = dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    previous = old.get('entries', {})
    entries, output, corrections, quarantined = {}, [], [], []
    changed = False
    for rid, raw in sorted(raw_records.items()):
        try:
            rec = repair(raw, corrections)
        except ValueError as error:
            quarantined.append({'id': rid, 'reason': str(error), 'source_title': raw.get('title')})
            continue
        # Metadata digest excludes sync-managed fields and uses the corrected representation.
        digest = hashlib.sha256(canonical(rec).encode('utf-8')).hexdigest()
        prior = previous.get(rid, {})
        if prior.get('digest') == digest:
            updated_at = prior['updated_at']
        else:
            changed = True
            updated_at = now
            if prior and updated_at <= prior['updated_at']:
                raise ValueError('Clock must advance before changing an existing record')
        entries[rid] = {'digest': digest, 'updated_at': updated_at}
        rec['updated_at'] = updated_at
        rec['metadata_source'] = rec.get('source_provider') or 'OpenAlex'
        rec['provenance'] = {'source': rec['metadata_source'],
                             'source_reference': rec.get('source_record_url') or rec.get('source_url') or rid}
        output.append(rec)
    if set(entries) != set(previous):
        changed = True
    version = int(old.get('catalog_version', 0)) + (1 if changed or not old else 0)
    generated_at = now if changed or not old else old['generated_at']
    envelope = {'schema_version': 1, 'catalog_id': catalog_id,
                'catalog_version': version, 'generated_at': generated_at,
                'records': output, 'next_cursor': None}
    data = (canonical(envelope) + '\n').encode('utf-8')
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Feed exceeds 32 MiB per-page limit')
    audit = {'source_records': len(raw_records), 'published_records': len(output),
             'quarantined': quarantined, 'corrections': corrections}
    # Write all output only after validating all records and limits.
    for path in (args.out, args.state, args.audit):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    args.state.write_text(json.dumps({'catalog_id': catalog_id, 'catalog_version': version,
        'generated_at': generated_at, 'entries': entries}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    args.audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Published {len(output)} records; quarantined {len(quarantined)}')
    print(f'Catalog version: {version}; feed size: {len(data)/1048576:.2f} MiB')
    print(f'URL corrections: {len(corrections)}; audit: {args.audit}')
    print(f'Feed: {args.out}; state: {args.state}')
    if quarantined:
        print('Quarantined IDs:', ', '.join(x['id'] for x in quarantined))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
