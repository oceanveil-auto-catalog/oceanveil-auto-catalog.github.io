#!/usr/bin/env python3
"""Build a static Stremio/Nuvio addon that mirrors OceanVeil's homepage rows.

Each run:
1. Reads the full public title list and the homepage row layout from oceanveil.net.
2. Matches titles to IMDb/TMDB IDs (cached in state.json, so the API key is
   only used for new or unmatched titles).
3. Publishes one catalog per homepage row, in the site's order, plus
   SFW / 16+ / NSFW / Mix filter catalogs.

Matched titles use IMDb `tt` IDs (or `tmdb:` when IMDb is unknown) so other
installed addons supply metadata and streams. Unmatched titles get their own
`oceanveil_<id>` meta pages so they still show up with details.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from enrichment import ENRICH_VERSION, Enricher, lookup_title, norm, preview_metadata, series_key
import oceanveil
from oceanveil import OceanVeil, TAG_DUBBED, TAG_NSFW

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / 'state.json'
OUT = ROOT / 'public'
VERSION = '2.2.0'
OWN_PREFIX = 'oceanveil_'
PREFIX = 'OceanVeil • '

# The six filters used to be six board rows that mostly repeated each other. They are now one
# "Browse" catalog: the board shows All, and Discover offers the rest as a filter dropdown.
# Option values double as file names, so they stay URL-safe (no spaces, '+' or '/').
BROWSE_ID = 'oceanveil-browse'
BROWSE_FILTERS = [
    ('All', 'oceanveil-mix-all'),
    ('SFW', 'oceanveil-sfw'),
    ('16-Plus', 'oceanveil-16plus'),
    ('NSFW', 'oceanveil-nsfw'),
    ('Mix-SFW-NSFW', 'oceanveil-mix'),
    ('NSFW-Dubbed', 'oceanveil-nsfw-dubbed'),
]
# Page offsets published for each catalog. Stremio asks for skip=100, 200, ...; some clients page
# by 20 or 25, and a missing file would end their list early.
PAGE = 100
SKIP_STEPS = (20, 25, 100)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def pages_base_url() -> str:
    if os.environ.get('PAGES_BASE_URL'):
        return os.environ['PAGES_BASE_URL'].rstrip('/')
    repo = os.environ.get('GITHUB_REPOSITORY', 'example/oceanveil-auto-catalog')
    owner, name = repo.split('/', 1)
    host = f'{owner.lower()}.github.io'
    return f'https://{host}' if name.lower() == host else f'https://{host}/{name}'


def dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n', encoding='utf8')


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {'version': 2, 'titles': {}}
    data = json.loads(STATE_FILE.read_text(encoding='utf8'))
    if data.get('version') == 2:
        return data
    # Migrate older state: keep any enrichment we already paid API calls for.
    legacy = {}
    for row in (data.get('titles') or {}).values():
        e = row.get('enrichment') or {}
        if e.get('tmdb_id') or e.get('imdb_id'):
            legacy[norm(lookup_title(row.get('title')))] = e
    return {'version': 2, 'titles': {}, 'legacy_enrichment': legacy}


def classify(t: dict) -> str:
    if t.get('nsfw') or t.get('rating') == 'age_18_plus':
        return 'nsfw'
    if t.get('rating') in ('pg', 'age_13_plus'):
        return 'sfw'
    return '16plus'


def interleave(a: list, b: list) -> list:
    out = []
    for n in range(max(len(a), len(b))):
        out += [x for x in (a[n:n + 1] + b[n:n + 1])]
    return out


# ---------------------------------------------------------------- matching

def trusted_art(url) -> str:
    host = (urlparse(str(url or '')).hostname or '').lower()
    return url if host in {'image.tmdb.org', 'media.kitsu.io', 's4.anilist.co'} else ''


def strength(e: dict) -> int:
    """How useful a match is for playback: IMDb > Kitsu > TMDB > nothing."""
    e = e or {}
    return (8 if re.fullmatch(r'tt\d+', str(e.get('imdb_id') or '')) else 0) + \
           (4 if e.get('kitsu_id') else 0) + (2 if e.get('tmdb_id') else 0) + (1 if e.get('anilist_id') else 0)


def share_between_versions(titles: dict) -> None:
    """'[Dub] X' and 'X' are the same show: give every version the best match any of them has."""
    best = {}
    for t in titles.values():
        e = t.get('enrichment') or {}
        k = series_key(t['name'])
        if strength(e) > strength(best.get(k)):
            best[k] = e
    for t in titles.values():
        e = best.get(series_key(t['name']))
        if e and strength(e) > strength(t.get('enrichment')):
            t['enrichment'] = dict(e)


def enrich_titles(titles: dict, enricher, legacy: dict, budget: int) -> int:
    attempts = 0
    today = datetime.now(timezone.utc).date().isoformat()
    share_between_versions(titles)
    done = set()  # one lookup per show, not per dub/sub copy
    for oid, t in titles.items():
        prior = t.get('enrichment') or legacy.get(norm(lookup_title(t['name']))) or {}
        # IMDb matches stay valid; weaker cached results get one retry when matching improves.
        fresh = prior.get('refresh_after', '') >= today and (prior.get('v') == ENRICH_VERSION or strength(prior) >= 8)
        if (prior.get('id') and fresh) or series_key(t['name']) in done or (prior and attempts >= budget):
            t['enrichment'] = prior
            continue
        if attempts >= budget:
            continue
        attempts += 1
        done.add(series_key(t['name']))
        alts = [x for x in (t.get('romaji'), t.get('japanese')) if x]
        e = enricher.enrich(t['name'], prior, alts) or prior
        if not (e.get('tmdb_id') or e.get('imdb_id') or e.get('kitsu_id')) and t.get('romaji'):
            alt = enricher.enrich(t['romaji'], {}, [t['name']]) or {}
            if strength(alt) > strength(e):
                e = alt
        if e:
            t['enrichment'] = e
    share_between_versions(titles)
    return attempts


def canonical_oids(titles: dict) -> dict:
    """Map every OceanVeil ID to one ID per show (the original, i.e. non-dub, lowest ID)."""
    groups = {}
    for oid in titles:
        groups.setdefault(series_key(titles[oid]['name']), []).append(oid)
    out = {}
    for oids in groups.values():
        main = sorted(oids, key=lambda o: (bool(titles[o].get('dubbed')), int(o) if o.isdigit() else 0))[0]
        for o in oids:
            out[o] = main
    return out


def preview_for(oid: str, t: dict, main: str | None = None) -> tuple[dict, bool]:
    """Return (meta preview, served_by_us).

    IMDb-matched titles use their tt ID so Cinemeta/Torrentio etc. handle them. Otherwise we
    serve our own meta page. If we know the Kitsu ID, its episodes use kitsu:<id>:<ep> video IDs,
    which anime stream addons (Torrentio and others) can play. TMDB-only titles keep tmdb: IDs
    for clients like Nuvio. Anything else gets an "Open on OceanVeil" link as its stream.
    """
    main = main or oid
    e = t.get('enrichment') or {}
    imdb = e.get('imdb_id') or ''
    ov_poster = oceanveil.poster_url(oid)
    art = (trusted_art(e.get('poster')) or trusted_art(e.get('kitsu_poster'))
           or trusted_art(e.get('anilist_poster')) or ov_poster)
    kind = 'movie' if e.get('tmdb_type') == 'movie' else 'series'
    meta = dict(e)
    meta['description'] = e.get('description') or t.get('summary') or e.get('anilist_description')
    meta.setdefault('background', trusted_art(e.get('anilist_background')) or None)
    meta.setdefault('releaseInfo', e.get('anilist_year'))
    meta.setdefault('genres', e.get('anilist_genres'))
    if re.fullmatch(r'tt\d+', imdb):
        item_id, own = imdb, False
    elif e.get('kitsu_id'):
        item_id, own, kind = f'{OWN_PREFIX}{main}', True, 'series'
    elif e.get('tmdb_id'):
        item_id, own = f"tmdb:{e['tmdb_id']}", False
    else:
        item_id, own, kind = f'{OWN_PREFIX}{main}', True, 'series'
    p = {'id': item_id, 'type': kind, **preview_metadata(lookup_title(t['name']), meta, art)}
    p['poster'] = art
    if own and not e.get('kitsu_id') and not e.get('description') and t.get('summary'):
        p['description'] = t['summary']
    return p, own


def own_meta(main: str, versions: list[str], titles: dict, preview: dict) -> tuple[dict, dict]:
    """Full meta for a title we serve ourselves, plus {video_id: [streams]} for its episodes."""
    t = titles[main]
    e = next((titles[o]['enrichment'] for o in versions if (titles[o].get('enrichment') or {}).get('kitsu_id')),
             t.get('enrichment') or {})
    links = []
    for o in versions:
        label = 'Dub' if titles[o].get('dubbed') else 'Sub'
        links.append({'name': f'Watch on OceanVeil ({label})' if len(versions) > 1 else 'Watch on OceanVeil',
                      'category': 'OceanVeil', 'url': f'{oceanveil.SITE}/anime_titles/{o}'})
    ov_streams = [{'name': 'OceanVeil', 'title': link['name'], 'externalUrl': link['url']} for link in links]
    released = (t.get('start_at') or '2000-01-01')[:10] + 'T00:00:00.000Z'
    count = int(e.get('episodes') or 0) or max(int(titles[o].get('episodes') or 0) for o in versions) or 1
    count = min(count, 500)
    videos, streams = [], {}
    for ep in range(1, count + 1):
        if e.get('kitsu_id'):
            vid = f"kitsu:{e['kitsu_id']}:{ep}"
        else:
            vid = f'{OWN_PREFIX}{main}:1:{ep}'
        videos.append({'id': vid, 'title': f'Episode {ep}' if count > 1 else 'Full title',
                       'season': 1, 'episode': ep, 'released': released})
        streams[vid] = ov_streams
    meta = {**preview, 'releaseInfo': preview.get('releaseInfo') or (t.get('start_at') or '')[:4] or None,
            'website': links[0]['url'], 'links': links, 'videos': videos,
            'behaviorHints': {'defaultVideoId': videos[0]['id']} if count == 1 else None}
    return {k: v for k, v in meta.items() if v}, streams


# ---------------------------------------------------------------- publish

def emit_catalog(catid: str, metas: list, genre: str | None = None) -> None:
    """Write the catalog and every page a client might ask for (empty past the end, so lists stop)."""
    base = OUT / 'catalog' / 'series'
    offsets = sorted({s for step in SKIP_STEPS for s in range(0, len(metas) + step, step)})
    for skip in offsets:
        page = {'metas': metas[skip:skip + PAGE]}
        if genre is None:
            if skip == 0:
                dump_json(base / f'{catid}.json', page)
            dump_json(base / catid / f'skip={skip}.json', page)
        else:
            name = f'genre={genre}' + (f'&skip={skip}' if skip else '')
            dump_json(base / catid / f'{name}.json', page)


def publish(titles: dict, rows: list[dict], stats: dict) -> dict:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    (OUT / '.nojekyll').touch()
    base = pages_base_url()

    mains = canonical_oids(titles)
    versions = {}
    for oid, main in mains.items():
        versions.setdefault(main, []).append(oid)
    previews, own, playable_own, stream_files = {}, 0, 0, 0
    for main, group in versions.items():
        group.sort(key=lambda o: (bool(titles[o].get('dubbed')), int(o) if o.isdigit() else 0))
        # The best-matched version speaks for the whole show (after share_between_versions they agree).
        best = max(group, key=lambda o: strength(titles[o].get('enrichment')))
        p, served = preview_for(best, titles[best], main)
        for o in group:
            previews[o] = p
        if served:
            own += 1
            meta, streams = own_meta(main, group, titles, p)
            playable_own += meta['videos'][0]['id'].startswith('kitsu:')
            dump_json(OUT / 'meta' / 'series' / f"{p['id']}.json", {'meta': meta})
            for vid, s in streams.items():
                dump_json(OUT / 'stream' / 'series' / f'{vid}.json', {'streams': s})
                stream_files += 1

    def metas(oids):
        seen, out = set(), []
        for oid in oids:
            p = previews.get(str(oid))
            if p and p['id'] not in seen:
                seen.add(p['id'])
                out.append(p)
        return out

    catalogs, counts = [], {}
    for row in rows:
        items = metas(row['ids'])
        if not items:
            continue
        catalogs.append({'type': 'series', 'id': row['catalog_id'], 'name': PREFIX + row['name'],
                         'extra': [{'name': 'skip', 'isRequired': False}]})
        emit_catalog(row['catalog_id'], items)
        counts[row['catalog_id']] = len(items)

    order = list(titles)  # API order: newest first
    groups = {c: [o for o in order if classify(titles[o]) == c] for c in ('sfw', '16plus', 'nsfw')}
    filters = {
        'oceanveil-mix-all': order,
        'oceanveil-sfw': groups['sfw'],
        'oceanveil-16plus': groups['16plus'],
        'oceanveil-nsfw': groups['nsfw'],
        'oceanveil-mix': interleave(groups['sfw'], groups['nsfw']),
        'oceanveil-nsfw-dubbed': [o for o in groups['nsfw'] if titles[o].get('dubbed')],
    }
    options = []
    for label, key in BROWSE_FILTERS:
        items = metas(filters[key])
        counts[f'{BROWSE_ID}:{label}'] = len(items)
        if label == 'All':
            emit_catalog(BROWSE_ID, items)  # what the board row and "no filter" show
        if items:
            options.append(label)
            emit_catalog(BROWSE_ID, items, genre=label)
    catalogs.append({'type': 'series', 'id': BROWSE_ID, 'name': PREFIX + 'Browse',
                     'genres': options,
                     'extra': [{'name': 'genre', 'isRequired': False, 'options': options},
                               {'name': 'skip', 'isRequired': False}]})

    manifest = {
        'id': 'community.oceanveil.auto.catalog',
        'version': VERSION,
        'name': 'OceanVeil',
        'description': "Mirrors OceanVeil's homepage rows daily, plus SFW / NSFW filters. "
                       'Matched titles use IMDb/TMDB IDs so your stream addons can play them.',
        'logo': f'{base}/logo.png' if (ROOT / 'static' / 'logo.png').exists() else None,
        'resources': ['catalog', {'name': 'meta', 'types': ['series'], 'idPrefixes': [OWN_PREFIX]},
                      # "Open on OceanVeil" for titles no stream addon can play; kitsu: video IDs
                      # also get that link next to whatever your anime stream addons return.
                      {'name': 'stream', 'types': ['series'], 'idPrefixes': [OWN_PREFIX, 'kitsu:']}],
        'types': ['series', 'movie'],
        'idPrefixes': ['tt', 'tmdb:', OWN_PREFIX, 'kitsu:'],
        'catalogs': catalogs,
        'behaviorHints': {'configurable': False},
    }
    manifest = {k: v for k, v in manifest.items() if v is not None}
    dump_json(OUT / 'manifest.json', manifest)
    if (ROOT / 'static' / 'logo.png').exists():
        shutil.copy(ROOT / 'static' / 'logo.png', OUT / 'logo.png')

    ids = list({p['id']: 1 for p in previews.values()})
    rated_sfw = len(groups['sfw'])
    status = {
        'build_version': VERSION,
        'updated_utc': now_utc(),
        'titles': len(titles),
        'shows_after_merging_dub_sub': len(ids),
        'id_types': {'imdb': sum(i.startswith('tt') for i in ids), 'tmdb': sum(i.startswith('tmdb:') for i in ids),
                     'oceanveil_own_meta': own, 'own_meta_with_kitsu_episodes': playable_own,
                     'own_meta_link_only': own - playable_own},
        'stream_files': stream_files,
        'source_region_check': {
            'proxy_used': stats.get('proxied', False),
            'titles_listed': len(titles), 'sfw_titles_listed': rated_sfw,
            'looks_region_limited': rated_sfw < max(20, len(titles) // 5),
            'hint': 'OceanVeil lists licensed (mostly PG/13+) titles only to some regions. If this list is '
                    'much shorter than oceanveil.net shows you, set the OCEANVEIL_PROXY secret.',
        },
        'homepage_config_live': stats.get('home_live'),
        'home_rows': [{'catalog_id': r['catalog_id'], 'name': r['name'], 'items': counts.get(r['catalog_id'], 0)} for r in rows],
        'catalog_counts': counts,
        'classification': {k: len(v) for k, v in groups.items()},
        'enrichment_attempts_this_run': stats.get('attempts'),
        'metadata_apis_configured': stats.get('apis'),
        'link_only_titles': sorted({titles[o]['name'] for o in titles if previews[o]['id'].startswith(OWN_PREFIX)
                                    and not (titles[o].get('enrichment') or {}).get('kitsu_id')})[:80],
        'source_checks': stats.get('events', [])[:40],
        'addon_manifest_url': f'{base}/manifest.json',
    }
    dump_json(OUT / 'status.json', status)
    write_pages(base, catalogs)
    return status


def write_pages(base: str, catalogs: list[dict]) -> None:
    options = ''.join(f"<option value='{c['id']}'>{c['name'].replace(PREFIX, '')}</option>" for c in catalogs)
    (OUT / 'index.html').write_text(
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>OceanVeil Catalog</title><style>body{font:16px/1.6 system-ui;max-width:1000px;margin:5vh auto;padding:16px;"
        "background:#0b1523;color:#eef6ff}a{color:#88cdff}code{overflow-wrap:anywhere}select{padding:10px;border-radius:8px;"
        "background:#24354b;color:#fff;border:1px solid #47607a;max-width:100%}.g{display:grid;grid-template-columns:"
        "repeat(auto-fill,minmax(130px,1fr));gap:14px;margin-top:18px}.g div{background:#16263b;border-radius:10px;padding:8px}"
        ".g img{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:6px;background:#24354b}.g b{display:block;font-size:13px;"
        "line-height:1.3;margin-top:6px}</style><h1>OceanVeil Catalog</h1>"
        f"<p>Install URL: <code>{base}/manifest.json</code> · <a href='status.json'>status</a></p>"
        f"<select id='c'>{options}</select><p id='s'></p><div class='g' id='g'></div><script>"
        "const c=document.getElementById('c'),g=document.getElementById('g'),s=document.getElementById('s');"
        "async function r(){g.textContent='';try{const d=await (await fetch('catalog/series/'+c.value+'.json',{cache:'no-store'})).json();"
        "s.textContent=d.metas.length+' titles';for(const m of d.metas){const e=document.createElement('div'),i=document.createElement('img'),"
        "b=document.createElement('b');i.loading='lazy';i.src=m.poster||'';i.alt='';b.textContent=m.name+' · '+m.id;e.append(i,b);g.append(e)}}"
        "catch(x){s.textContent='Failed: '+x.message}}c.onchange=r;r();</script></html>", encoding='utf8')


# ---------------------------------------------------------------- main

def resolve_rows(ov: OceanVeil, sections: list[dict], titles: dict, saved: dict | None = None) -> list[dict]:
    saved = saved or {}
    rows, promo = [], 0
    for s in oceanveil.active_title_rows(sections):
        key = oceanveil.row_key(s)
        if key is None:
            promo += 1
            key = f'promo-{promo}'
        if s.get('search_type') == 'completed':
            ids = [o for o, t in titles.items() if t.get('completed')]
        else:
            found = ov.resolve_row(s)
            if found is None:
                continue
            for t in found:  # titles reachable only through a row still get recorded
                titles.setdefault(str(t['id']), record(t, saved.get(str(t['id']), {})))
            ids = [str(t['id']) for t in found]
        rows.append({'catalog_id': f'oceanveil-home-{key}', 'name': str(s.get('header') or key).strip(),
                     'search_type': s.get('search_type'), 'ids': ids})
    return rows


def record(a: dict, previous: dict) -> dict:
    today = now_utc()[:10]
    return {**previous,
            'name': a['name'], 'romaji': a.get('romajiName') or '', 'japanese': a.get('japaneseName') or '',
            'summary': a.get('summary') or '',
            'rating': a.get('rating') or '', 'simulcast': bool(a.get('isSimulcast')),
            'completed': bool(a.get('isCompleted')), 'start_at': a.get('startAt') or '',
            'season': a.get('seasonName') or '', 'episodes': int(a.get('episodeCount') or 0),
            'first_seen': previous.get('first_seen', today), 'last_seen': today}


def build() -> dict:
    state = load_state()
    saved = state.get('titles') or {}
    ov = OceanVeil()

    listing = ov.titles('anime_titles')
    if not listing:
        # Fail the run: GitHub Pages keeps serving the last good deployment.
        raise RuntimeError('OceanVeil title API unavailable; refusing to publish an empty or stale catalog.')
    nsfw = {str(t['id']) for t in ov.titles('anime_titles', [('tag_ids[]', TAG_NSFW)]) or []}
    dubbed = {str(t['id']) for t in ov.titles('anime_titles', [('tag_ids[]', TAG_DUBBED)]) or []}

    titles = {}
    for a in listing:
        oid = str(a['id'])
        titles[oid] = record(a, saved.get(oid, {}))
    sections, home_live = ov.home_sections()
    rows = resolve_rows(ov, sections, titles, saved)
    for oid, t in titles.items():
        if oid in saved and 'enrichment' not in t and saved[oid].get('enrichment'):
            t['enrichment'] = saved[oid]['enrichment']
        t['nsfw'] = oid in nsfw
        t['dubbed'] = oid in dubbed or bool(re.match(r'\s*\[\s*dub', t['name'], re.I))

    enricher = Enricher()
    attempts = enrich_titles(titles, enricher, state.get('legacy_enrichment') or {},
                             int(os.getenv('ENRICH_MAX_NEW', '120')))
    dump_json(STATE_FILE, {'version': 2, 'updated_utc': now_utc(), 'titles': titles,
                           'home_rows': [{k: r[k] for k in ('catalog_id', 'name', 'ids')} for r in rows]})
    status = publish(titles, rows, {
        'home_live': home_live, 'attempts': attempts, 'events': ov.events, 'proxied': ov.proxied,
        'apis': {'tmdb': bool(enricher.tmdb_key), 'tvdb': bool(enricher.tvdb_key), 'mdblist': bool(enricher.mdb_key)},
    })
    print(f"Built {VERSION}: {len(titles)} titles, {len(rows)} homepage rows (live config: {home_live}), "
          f"IDs {status['id_types']}, enrichment attempts {attempts}, TMDB key set: {status['metadata_apis_configured']['tmdb']}")
    region = status['source_region_check']
    if region['looks_region_limited']:
        print(f"::warning::OceanVeil returned {region['titles_listed']} titles, only {region['sfw_titles_listed']} rated PG/13+. "
              'The site likely hides licensed titles from this machine\'s region; set OCEANVEIL_PROXY to fix.')
    if not status['metadata_apis_configured']['tmdb']:
        print('::warning::TMDB_API_KEY is not set for this run, so matching relies on Kitsu only.')
    return status


if __name__ == '__main__':
    build()
