#!/usr/bin/env python3
"""OpenAlex-backed, conservative four-species bibliographic collector.

Produces v0.6 legacy import arrays. Requires Python 3.10+, standard library only.
This is a first provider, NOT a claim of comprehensive multi-provider coverage.
"""
import argparse, json, re, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

SPECIES = {
 'Lutra lutra': ['"Lutra lutra"', '"Eurasian otter"', '"European otter"'],
 'Phoca vitulina': ['"Phoca vitulina"', '"harbour seal"', '"harbor seal"'],
 'Halichoerus grypus': ['"Halichoerus grypus"', '"grey seal"', '"gray seal"'],
 'Phocoena phocoena': ['"Phocoena phocoena"', '"harbour porpoise"', '"harbor porpoise"'],
}
CATEGORY_RULES = {
 'Hearing & Acoustics': r'\b(hearing|auditory|audiogram|acoustic|echolocation|sonar|vocali[sz]|noise|sound)\b',
 'Reproduction': r'\b(reproduc|pregnan|gestat|lactat|breeding|fertilit|parturit|ovulat)\w*',
 'Physiology': r'\b(physiolog|metaboli|thermoregulat|respirat|cardiac|hormone|cortisol|energetic)\w*',
 'Veterinary Medicine & Disease': r'\b(pathogen|disease|infection|parasite|veterinar|mortality|necrops|health|virus|bacteri)\w*',
 'Animal Welfare & Captive Management': r'\b(captiv|husbandry|welfare|aquari|rehabilitat|enclosure|training)\w*',
 'Diet & Foraging': r'\b(diet|prey|forag|feeding|stomach content|trophic)\w*',
 'Population Biology & Genetics': r'\b(population|genetic|genom|demograph|abundance|density|phylogen)\w*',
 'Conservation & Human Impacts': r'\b(conserv|bycatch|pollut|contamin|fishery|anthropogenic|threat|shipping)\w*',
 'Ecology & Habitat': r'\b(ecolog|habitat|distribution|movement|migration|home range|spatial)\w*',
 'Behaviour': r'\b(behavio|social|activity|sleep|play|aggression|interaction)\w*',
}
DOI_RE = re.compile(r'^10\.\d{4,9}/\S+$', re.I)

def reconstruct(index):
    if not index: return None
    size = max((max(positions) for positions in index.values() if positions), default=-1) + 1
    words = [''] * size
    for word, positions in index.items():
        for pos in positions:
            if 0 <= pos < size: words[pos] = word
    return ' '.join(words).strip() or None

def classify(text):
    return [name for name, pattern in CATEGORY_RULES.items() if re.search(pattern, text, re.I)] or ['Other']

def match_species(text):
    result = []
    for species, terms in SPECIES.items():
        if any(re.search(r'(?<!\w)' + re.escape(t.strip('"')) + r'(?!\w)', text, re.I) for t in terms):
            result.append(species)
    return result

def normalize_doi(value):
    if not value: return None
    doi = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:)', '', str(value).strip(), flags=re.I).lower()
    return doi if DOI_RE.fullmatch(doi) else None

def convert(work):
    title = (work.get('title') or work.get('display_name') or '').strip()
    if not title: return None
    abstract = reconstruct(work.get('abstract_inverted_index'))
    # Use only title and available abstract for species classification; broad API matches may be false positives.
    searchable = title + ' ' + (abstract or '')
    species = match_species(searchable)
    if not species: return None
    doi = normalize_doi(work.get('doi'))
    wid = work.get('id')
    if not doi and not wid: return None
    journal = ((work.get('primary_location') or {}).get('source') or {}).get('display_name')
    authors = [a.get('author', {}).get('display_name') for a in work.get('authorships', [])]
    authors = [a for a in authors if a]
    year = work.get('publication_year')
    if not isinstance(year, int) or not 1000 <= year <= 2100: year = None
    url = 'https://doi.org/' + doi if doi else wid
    oa = work.get('open_access') or {}
    oa_url = oa.get('oa_url')
    # OA status is metadata, not permission to redistribute the PDF.
    access = 'open_access' if oa.get('is_oa') is True else 'unknown'
    record = {
      'id': 'openalex:' + str(wid).rsplit('/', 1)[-1], 'title': title, 'doi': doi,
      'year': year, 'authors': authors or None, 'journal': journal,
      'abstract': abstract, 'source_url': url, 'oa_url': oa_url,
      'species': species, 'categories': classify(searchable), 'keywords': [],
      'summary_en': None, 'summary_basis': 'none', 'access_status': access,
      'publication_review_status': 'Metadata Only',
      'publication_type': work.get('type'),
      'source_provider': 'OpenAlex', 'source_record_url': wid,
    }
    return record

def request_json(url, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FourSpeciesResearchCollector/0.1 (academic bibliography; Python urllib)', 'Accept':'application/json'})
            with urllib.request.urlopen(req, timeout=40) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries - 1: raise
            time.sleep(min(30, 2 ** attempt * 3))
        except (TimeoutError, urllib.error.URLError):
            if attempt == retries - 1: raise
            time.sleep(min(30, 2 ** attempt * 3))

def collect(args):
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    records = {}; stats = []; api_key = args.api_key
    for species, phrases in SPECIES.items():
        for phrase in phrases:
            cursor = '*'; fetched = 0; kept = 0
            for page in range(args.pages_per_query):
                params = {'search.title_and_abstract': phrase, 'per_page': str(args.per_page), 'cursor': cursor}
                if api_key: params['api_key'] = api_key
                url = 'https://api.openalex.org/works?' + urllib.parse.urlencode(params)
                response = request_json(url)
                works = response.get('results') or []
                if not works: break
                fetched += len(works)
                for work in works:
                    record = convert(work)
                    if not record: continue
                    key = 'doi:' + record['doi'] if record['doi'] else 'id:' + record['id']
                    if key in records:
                        old = records[key]
                        old['species'] = sorted(set(old['species']) | set(record['species']))
                        old['categories'] = sorted(set(old['categories']) | set(record['categories']))
                        if not old.get('abstract') and record.get('abstract'): old['abstract'] = record['abstract']
                    else: records[key] = record
                    kept += 1
                cursor = (response.get('meta') or {}).get('next_cursor')
                if not cursor: break
                if not api_key and page + 1 < args.pages_per_query:
                    raise SystemExit('OpenAlex requires a free API key for cursor pages after the first. Set --api-key or OPENALEX_API_KEY.')
                time.sleep(args.delay)
            stats.append({'species':species, 'query':phrase, 'fetched':fetched, 'matched':kept})
            print(f'{species}: {phrase}: fetched={fetched}, matched={kept}', flush=True)
    ordered = sorted(records.values(), key=lambda x: (-(x['year'] or 0), x['title'].lower()))
    # Stay below Android 32 MiB decoded-text import limit, even for large abstracts.
    batch = []; size = 2; index = 1
    def write(items, i):
        name = out / f'four_species_import_{i:03d}.json'
        name.write_text(json.dumps(items, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'Wrote {name} ({len(items)} records)')
    for record in ordered:
        item_size = len(json.dumps(record, ensure_ascii=False).encode('utf-8')) + 4
        if batch and (len(batch) >= args.batch_size or size + item_size > 28 * 1024 * 1024):
            write(batch, index); index += 1; batch=[]; size=2
        batch.append(record); size += item_size
    if batch: write(batch, index)
    (out/'collection_report.json').write_text(json.dumps({'provider':'OpenAlex', 'total_unique':len(ordered), 'query_stats':stats, 'notes':['Title/abstract species matching is conservative but not human-verified.','This is a bounded collection, not exhaustive historical coverage.','No AI summaries or full-text verification performed.']}, indent=2), encoding='utf-8')
    print(f'Total unique: {len(ordered)}')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', default='output')
    p.add_argument('--api-key', default=__import__('os').environ.get('OPENALEX_API_KEY', ''))
    p.add_argument('--pages-per-query', type=int, default=1, help='Start with 1; increase for historical collection')
    p.add_argument('--per-page', type=int, default=100)
    p.add_argument('--batch-size', type=int, default=1000)
    p.add_argument('--delay', type=float, default=0.25)
    a = p.parse_args()
    if a.pages_per_query < 1 or not 1 <= a.per_page <= 100 or a.batch_size < 1: p.error('invalid limits')
    collect(a)
if __name__ == '__main__': main()
