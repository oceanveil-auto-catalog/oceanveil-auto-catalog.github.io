#!/usr/bin/env python3
"""Build daily static Stremio/Nuvio catalogs from *public* OceanVeil metadata.

Preserves homepage sections in source order. Doesn't scrape videos or infer that
an OceanVeil page is playable through Stremio. All sources may be incomplete.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import unicodedata
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from metadata_enrichment import TMDBMatcher, add_metadata, IMDb

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / 'state.json'
OUT = ROOT / 'public'
OFFICIAL = 'https://oceanveil.net'
SECONDARY = 'https://manhwaindex.com/platform/oceanveil'
UA = 'OceanVeilCatalogBot/3.0 (public metadata only)'
TIMEOUT = 18
DELAY = 0.7
MAX_SITEMAP_URLS = 8000
MAX_XML_FILES = 60
MAX_DETAILS = int(os.environ.get('MAX_DETAILS', '180'))
MAX_BROWSE_PAGES = 24
MAX_SECONDARY_PAGES = 65
TITLE_PATH = re.compile(r'^/anime_titles/(\d+)/?$')

# Stable ID/name mappings let installed addons keep their rows after updates.
HOME_SECTIONS = OrderedDict([
    ('simulcast-lineup', 'Simulcast Lineup'),
    ('newest-additions', 'Newest Additions'),
    ('newly-streaming', 'Newly Streaming'),
    ('others-are-watching', 'Others Are Watching'),
    ('special-pick-up', 'Special Pick Up'),
    ('ranking', 'Ranking'),
    ('english-dubbed-titles', 'English Dubbed Titles'),
    ('our-recommendations', 'Our Recommendations'),
    ('going-viral', 'Going Viral'),
    ('sweet-female-breaths', 'Sweet Female Breaths'),
    ('completed', 'Completed'),
    ('suitable-ladies-titles', 'Suitable Ladies Titles'),
    ('romance-section', 'Romance Section'),
    ('young-adult-section', 'Young Adult Section'),
    ('bl-section', 'BL Section'),
    ('suiseisha-collection', 'Suiseisha Collection'),
])
FILTERS = OrderedDict([
    ('oceanveil-anime', 'OceanVeil — All Titles'),
    ('simulcast', 'OceanVeil — Simulcast'),
    ('dubbed', 'OceanVeil — Dubbed'),
    ('sfw', 'OceanVeil — SFW (PG/13+)'),
    ('nsfw', 'OceanVeil — Adult (18+/Mature)'),
    ('mix', 'OceanVeil — SFW + Adult Mix'),
    ('top', 'OceanVeil — Top & Trending'),
    ('mix-all', 'OceanVeil — Mix of Everything'),
    ('unclassified', 'OceanVeil — 16+/Unclassified'),
])
GENERIC_TITLES = {'welcome', 'oceanveil', 'image', 'more', 'anime', 'all titles',
                  'read more', 'watch now', 'watch', 'niche anime', 'details'}
HOME_IGNORES = {'news', 'faq', 'why choose oceanveil', 'why chose oceanveil',
                'about us', 'home', 'plans', 'free trial', 'subscription'}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def pages_base_url() -> str:
    if os.environ.get('PAGES_BASE_URL'):
        return os.environ['PAGES_BASE_URL'].rstrip('/')
    owner, repo = os.environ.get('GITHUB_REPOSITORY', 'example/oceanveil-auto-catalog').split('/', 1)
    if repo.lower() == f'{owner.lower()}.github.io':
        return f'https://{owner}.github.io'
    return f'https://{owner}.github.io/{repo}'


class GentleFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': UA, 'Accept': 'text/html,application/xml;q=0.9,*/*;q=0.7'})
        self.robots = {}
        self.last_request = {}

    def allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        if host not in self.robots:
            try:
                resp = self.session.get(f'https://{host}/robots.txt', timeout=TIMEOUT)
                if resp.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(resp.text.splitlines())
                    self.robots[host] = rp
                else:
                    self.robots[host] = None
            except requests.RequestException:
                self.robots[host] = None
        rp = self.robots[host]
        return rp is None or rp.can_fetch(UA, url)

    def get(self, url: str) -> str | None:
        if urlparse(url).scheme != 'https' or not self.allowed(url):
            print('Not fetching (scheme or robots.txt):', url)
            return None
        host = urlparse(url).netloc
        wait = DELAY - (time.monotonic() - self.last_request.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.last_request[host] = time.monotonic()
        try:
            resp = self.session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            if len(resp.content) > 6_000_000:
                return None
            return resp.text
        except requests.RequestException as exc:
            print('Fetch failed:', url, str(exc)[:160])
            return None


def norm(text: str | None) -> str:
    return ' '.join((text or '').split())


def clean_title(text: str | None) -> str:
    value = norm(text)
    value = re.sub(r'^Cover of\s+', '', value, flags=re.I)
    value = re.sub(r'\s*\|\s*OceanVeil.*$', '', value, flags=re.I)
    if (len(value) < 3 or len(value) > 220 or value.lower() in GENERIC_TITLES
            or re.fullmatch(r'episode[\s.\-_:]*(?:\d+|\d+\s*[-.]\s*\d+)', value, re.I)
            or re.fullmatch(r'ep[\s.\-_:]*\d+', value, re.I)):
        return ''
    if any(s in value.lower() for s in ['oceanveil watch anime', 'dive for anime', 'leave with secrets']):
        return ''
    return value


def slugify(name: str) -> str:
    slug = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'-{2,}', '-', re.sub(r'[^a-z0-9]+', '-', slug)).strip('-')[:60]


def media_url(raw: str | None, origin: str) -> str:
    result = urljoin(origin, norm(raw)) if raw else ''
    return result if urlparse(result).scheme == 'https' and urlparse(result).netloc else ''


def ocean_id(url: str) -> str:
    parsed = urlparse(url)
    match = TITLE_PATH.fullmatch(parsed.path)
    if parsed.netloc.lower() in ('oceanveil.net', 'www.oceanveil.net') and match:
        return match.group(1)
    return ''


def is_browse_url(url: str) -> bool:
    p = urlparse(url)
    if p.netloc not in ('oceanveil.net', 'www.oceanveil.net') or p.path.rstrip('/') != '/anime_titles':
        return False
    params = parse_qs(p.query)
    return not params or (set(params) <= {'page'} and params['page'][0].isdigit())


def label_for_heading(label: str) -> str | None:
    normalized = slugify(re.sub(r'\|.*$', '', norm(label)))
    if not normalized or norm(label).lower() in HOME_IGNORES:
        return None
    aliases = {
        'simulcast-lineup-new-every-week': 'simulcast-lineup',
        'simulcast-lineup': 'simulcast-lineup',
        'newest-addition': 'newest-additions',
        'newest-additions': 'newest-additions',
        'english-dub-titles': 'english-dubbed-titles',
        'newly-streaming': 'newly-streaming',
        'recommendations': 'our-recommendations',
        'suitable-ladies': 'suitable-ladies-titles',
        'romance': 'romance-section',
        'young-adult': 'young-adult-section',
        'bl': 'bl-section',
    }
    if normalized in aliases:
        return aliases[normalized]
    if normalized in HOME_SECTIONS:
        return normalized
    if normalized.startswith('simulcast-lineup'):
        return 'simulcast-lineup'
    # Dynamically accommodate newly introduced promotional/genre rows.
    if 3 <= len(label) <= 75 and len(normalized) >= 4 and not any(k in normalized for k in ('subscribe', 'news', 'why-choose', 'streaming-service')):
        return normalized
    return None


def card_record(link, origin: str) -> tuple[str, dict] | None:
    url = urljoin(origin, link.get('href', ''))
    oid = ocean_id(url)
    if not oid:
        return None
    img = link.find('img')
    pic = media_url(img.get('data-src') or img.get('data-original') or img.get('src') or img.get('srcset', '').split(',')[0].split(' ')[0], origin) if img else ''
    title = (clean_title(img.get('alt') if img else '')
             or clean_title(link.get('aria-label'))
             or clean_title(link.get('title'))
             or clean_title(link.get_text(' ', strip=True)))
    parent = link.parent
    # Source often uses one <a> on poster, with the title in an adjacent <h3>.
    # Never take text from a wrapper containing several different anime links.
    for _ in range(3):
        if not parent or title:
            break
        distinct = {ocean_id(urljoin(origin, a.get('href', ''))) for a in parent.select('a[href]')}
        distinct.discard('')
        if len(distinct) > 1:
            break
        for h in parent.select('h2,h3,h4,h5,[class*=title]'):
            title = clean_title(h.get_text(' ', strip=True))
            if title:
                break
        parent = parent.parent
    if not title:
        return None
    container = link.parent
    for _ in range(3):
        if not container:
            break
        other = {ocean_id(urljoin(origin, a.get('href',''))) for a in container.select('a[href]')}
        other.discard('')
        if len(other) > 1:
            container = link  # sibling titles share this parent: never read their labels
            break
        if any(word in container.get_text(' ', strip=True).lower() for word in ('simcast', '18+', '16+', 'pg', 'new', 'dub', 'mature')):
            break
        container = container.parent
    info = norm(container.get_text(' ', strip=True))[:400] if container else ''
    # These fields come from the page, not from a guessed TMDB classification.
    flags = {
        'dubbed': bool(re.search(r'\[\s*dub\s*\]|\bdubbed\b', title, re.I) or re.search(r'\bdub\b', info, re.I)),
        'simulcast': bool(re.search(r'\bsim\s*cast\b|\bsimulcast\b', info, re.I)),
        'rating': detect_rating(info),
        'genre': detect_genre(info),
    }
    return oid, {'title': title, 'poster': pic, 'url': f'{OFFICIAL}/anime_titles/{oid}', **flags}


def detect_rating(text: str) -> str:
    for pattern, value in [(r'\b18\s*\+', '18+'), (r'\b16\s*\+', '16+'),
                           (r'\b13\s*\+', '13+'), (r'\bPG\b', 'PG')]:
        if re.search(pattern, text, flags=re.I):
            return value
    return ''


def detect_genre(text: str) -> str:
    for genre in ('Mature OVA', 'Mature', 'Young Adult', 'Romance', 'BL'):
        if re.search(r'\b' + re.escape(genre) + r'\b', text, flags=re.I):
            return genre
    return ''


def merge_record(before: dict, incoming: dict) -> dict:
    merged = dict(before)
    for key, value in incoming.items():
        if key in ('simulcast', 'dubbed'):
            merged[key] = bool(merged.get(key) or value)
        elif value or key not in merged:
            merged[key] = value
    return merged


def parse_homepage(html: str, origin: str) -> tuple[dict, OrderedDict]:
    """Track heading → anime cards in DOM order, never sort away promotions.

    Supports headings in h1–h4 and common CSS section-title nodes; ignores
    heading-like text inside title cards unless it matches an expected row.
    """
    soup = BeautifulSoup(html, 'html.parser')
    found: dict[str, dict] = {}
    sections: OrderedDict[str, dict] = OrderedDict()
    current: str | None = None
    for tag in soup.find_all(True):
        if tag.name in ('script', 'style', 'footer', 'nav'):
            continue
        if tag.name in ('h1', 'h2', 'h3', 'h4') or (tag.name in ('div', 'p', 'span') and any('section-title' in x or 'section-heading' in x for x in (tag.get('class') or []))):
            # Do not treat an h3 anime title as a new row.
            candidate = norm(tag.get_text(' ', strip=True))
            new_id = label_for_heading(candidate)
            if new_id and (tag.name in ('h1', 'h2') or new_id in HOME_SECTIONS or 'section' in ' '.join(tag.get('class') or [])):
                if candidate.lower() not in HOME_IGNORES:
                    current = new_id
                    sections.setdefault(new_id, {'name': candidate[:75], 'ids': []})
            elif tag.name in ('h1', 'h2') and current and candidate.lower() in HOME_IGNORES:
                current = None
        if tag.name != 'a' or not tag.get('href'):
            continue
        card = card_record(tag, origin)
        if not card:
            continue
        oid, record = card
        found[oid] = merge_record(found.get(oid, {}), record)
        if current:
            ids = sections[current]['ids']
            if oid not in ids:
                ids.append(oid)
    sections = OrderedDict((k, v) for k, v in sections.items() if v['ids'])
    return found, sections



def reconcile_homepage(previous: dict, discovered: OrderedDict) -> dict:
    """Keep catalog *IDs and positions* while changing their labels and contents.

    Fixed sections retain their historical semantic IDs (such as `ranking`).
    Unknown/renamable promotional rows are assigned stable slots by their
    order *among promotional rows*, never by the current heading text.

    Backwards compatibility: v2 used the heading slug as the ID. Reuse each
    v2 promotional catalog ID as its slot instead of breaking Nuvio sorting.
    A missing row stays registered with zero items, not its outdated titles.
    """
    old_sections = OrderedDict(previous.get('sections') or {})
    old_order = list(previous.get('row_order') or old_sections)
    slots = list(previous.get('promo_slots') or [])
    for key in old_sections:
        if key not in HOME_SECTIONS and key not in slots:
            slots.append(key)  # migrate v2 slug IDs without changing identity

    fresh = OrderedDict()
    next_promo = 0
    for source_id, data in discovered.items():
        if source_id in HOME_SECTIONS:
            stable_id = source_id
        else:
            if next_promo < len(slots):
                stable_id = slots[next_promo]
            else:
                number = 1
                while f'promo-slot-{number:02d}' in slots or f'promo-slot-{number:02d}' in HOME_SECTIONS:
                    number += 1
                stable_id = f'promo-slot-{number:02d}'
                slots.append(stable_id)
            next_promo += 1
        fresh[stable_id] = {'name': data['name'], 'ids': list(dict.fromkeys(data['ids']))}

    # A catalog's location in a Nuvio user's customized homepage should not
    # jump when the source website rearranges promotions or changes headings.
    order = list(dict.fromkeys([*old_order, *old_sections, *fresh]))
    sections = OrderedDict()
    for stable_id in order:
        if stable_id in fresh:
            sections[stable_id] = fresh[stable_id]
        else:
            old = old_sections.get(stable_id, {})
            # Keep the row/ID but clear a vanished promotion, otherwise
            # yesterday's featured shows would still look current.
            sections[stable_id] = {
                'name': old.get('name') or HOME_SECTIONS.get(stable_id, stable_id),
                'ids': [],
            }
    return {
        'sections': sections,
        'row_order': order,
        'promo_slots': slots,
        'last_success_utc': now_utc(),
    }



def parse_cards(html: str, origin: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    found = {}
    for a in soup.select('a[href]'):
        card = card_record(a, origin)
        if card:
            oid, record = card
            found[oid] = merge_record(found.get(oid, {}), record)
    return found


def browse_index(fetcher) -> tuple[dict, set]:
    # Follow *observed* pagination links only; do not guess page numbers.
    queue = deque([f'{OFFICIAL}/anime_titles'])
    seen = set()
    found = {}
    details = set()
    while queue and len(seen) < MAX_BROWSE_PAGES:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        html = fetcher.get(url)
        if not html:
            continue
        found.update(parse_cards(html, url))
        soup = BeautifulSoup(html, 'html.parser')
        for a in soup.select('a[href]'):
            href = urldefrag(urljoin(url, a['href']))[0]
            if ocean_id(href):
                details.add(href)
            elif is_browse_url(href) and href not in seen and href not in queue:
                queue.append(href)
        # Capture title links embedded in public HTML bootstrap JSON.
        for number in re.findall(r'/anime_titles/(\d+)(?:\b|[/?#])', html):
            details.add(f'{OFFICIAL}/anime_titles/{number}')
    print(f'Browse pages scanned={len(seen)} cards={len(found)} observed_links={len(details)}')
    return found, details


def scan_sitemaps(fetcher) -> set[str]:
    queue = deque([f'{OFFICIAL}/sitemap.xml', f'{OFFICIAL}/sitemap_index.xml'])
    visited = set()
    found = set()
    while queue and len(visited) < MAX_XML_FILES and len(found) < MAX_SITEMAP_URLS:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        xml = fetcher.get(url)
        if not xml:
            continue
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue
        for loc in root.findall('.//{*}loc'):
            value = norm(loc.text)
            if ocean_id(value):
                found.add(f'{OFFICIAL}/anime_titles/{ocean_id(value)}')
            elif urlparse(value).netloc in ('oceanveil.net', 'www.oceanveil.net') and urlparse(value).path.endswith('.xml') and value not in visited:
                queue.append(value)
            if len(found) >= MAX_SITEMAP_URLS:
                break
    print(f'Sitemap files scanned={len(visited)} detail_links={len(found)}')
    return found


def details_from_official(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, 'html.parser')
    title = ''
    for css in ('meta[property="og:title"]', 'meta[name="twitter:title"]', 'main h1', 'h1'):
        node = soup.select_one(css)
        if node:
            title = clean_title(node.get('content') if node.name == 'meta' else node.get_text(' ', strip=True))
            if title:
                break
    if not title or title.lower().startswith(('welcome ', 'oceanveil:')):
        # Some OceanVeil title routes send the homepage shell, not title metadata.
        return None
    img = soup.select_one('meta[property="og:image"]') or soup.select_one('meta[name="twitter:image"]')
    body = soup.select_one('main') or soup
    summary = norm(body.get_text(' ', strip=True))[:600]
    return {'title': title, 'poster': media_url(img.get('content'), url) if img else '',
            'url': f'{OFFICIAL}/anime_titles/{ocean_id(url)}',
            'dubbed': bool(re.search(r'\[\s*dub\s*\]', title, re.I)),
            'rating': detect_rating(summary), 'genre': detect_genre(summary), 'simulcast': False}


def secondary_index(fetcher) -> dict:
    """Supplementary only: never assume this index contains the whole catalog."""
    html = fetcher.get(SECONDARY)
    if not html:
        return {}
    page = BeautifulSoup(html, 'html.parser')
    links = []
    for a in page.select('a[href]'):
        u = urljoin(SECONDARY, a['href'])
        if urlparse(u).netloc == 'manhwaindex.com' and re.fullmatch(r'/anime/[^/]+/?', urlparse(u).path) and u not in links:
            links.append(u)
    found = {}
    for u in links[:MAX_SECONDARY_PAGES]:
        body = fetcher.get(u)
        if not body:
            continue
        s = BeautifulSoup(body, 'html.parser')
        h1 = s.select_one('h1')
        title = clean_title(h1.get_text(' ', strip=True) if h1 else '')
        if not title:
            continue
        pic = s.select_one('meta[property="og:image"]')
        for a in s.select('a[href]'):
            oid = ocean_id(urljoin(u, a.get('href', '')))
            if oid:
                found[oid] = {'title': title, 'poster': media_url(pic.get('content'), u) if pic else '',
                              'url': f'{OFFICIAL}/anime_titles/{oid}', 'dubbed': '[dub]' in title.lower(),
                              'rating': '', 'genre': '', 'simulcast': False}
                break
    print('Secondary index linked titles=', len(found))
    return found


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {'titles': {}, 'homepage': {}, 'homepage_updated_utc': None}
    data = json.loads(STATE_FILE.read_text(encoding='utf8'))
    return {'titles': data.get('titles', {}), 'homepage': data.get('homepage', {}),
            'homepage_updated_utc': data.get('homepage_updated_utc')}


def dump_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf8')


def put_title(titles: dict, oid: str, data: dict, today: str) -> None:
    previous = titles.get(oid, {})
    if not data.get('title'):
        return
    fields = {'title': data['title'], 'poster': data.get('poster') or previous.get('poster', ''),
              'url': f'{OFFICIAL}/anime_titles/{oid}',
              'first_seen': previous.get('first_seen', today), 'last_seen': today}
    for key in ('dubbed', 'simulcast'):
        fields[key] = bool(data.get(key) or previous.get(key) or (key == 'dubbed' and re.search(r'\[\s*dub\s*\]', data['title'], re.I)))
    for key in ('rating', 'genre'):
        fields[key] = data.get(key) or previous.get(key, '')
    if previous.get('title') == data['title']:
        for retained in ('match', 'match_checked_utc', 'year'):
            if retained in previous and retained not in fields:
                fields[retained] = previous[retained]
    if data.get('year'):
        fields['year'] = data['year']
    titles[oid] = fields


def adult_level(item: dict) -> str:
    if item.get('rating') == '18+' or item.get('genre', '').lower() in ('mature', 'mature ova'):
        return 'adult'
    if item.get('rating') in ('PG', '13+'):
        return 'sfw'
    return 'unknown'  # 16+ alone is NOT proof of SFW or explicit adult content.


def interleave(left: list, right: list) -> list:
    result, seen = [], set()
    for n in range(max(len(left), len(right))):
        for group in (left, right):
            if n < len(group) and group[n] not in seen:
                result.append(group[n]); seen.add(group[n])
    return result


def catalogs(titles: dict, homepage: dict) -> OrderedDict:
    by_recent = sorted(titles, key=lambda oid: (titles[oid].get('first_seen',''), int(oid)), reverse=True)
    # Full-library and mix-all deliberately contain ALL discovered entries.
    result = OrderedDict([(cid, list()) for cid in FILTERS])
    result['oceanveil-anime'] = by_recent
    result['mix-all'] = by_recent.copy()
    home_rows = homepage.get('sections', {})
    for oid in by_recent:
        item = titles[oid]
        if item.get('simulcast') or any(oid in home_rows.get(k, {}).get('ids', []) for k in ('simulcast-lineup',)):
            result['simulcast'].append(oid)
        if item.get('dubbed') or any(oid in home_rows.get(k, {}).get('ids', []) for k in ('english-dubbed-titles',)):
            result['dubbed'].append(oid)
        level = adult_level(item)
        if level == 'adult':
            result['nsfw'].append(oid)
        elif level == 'sfw':
            result['sfw'].append(oid)
        else:
            result['unclassified'].append(oid)
    result['mix'] = interleave(result['sfw'], result['nsfw'])
    for k in ('ranking', 'others-are-watching', 'going-viral'):
        for oid in home_rows.get(k, {}).get('ids', []):
            if oid in titles and oid not in result['top']:
                result['top'].append(oid)
    for section_id, section in home_rows.items():
        if section_id in HOME_SECTIONS or section.get('ids'):
            result[f'home-{section_id}'] = [oid for oid in section.get('ids', []) if oid in titles]
    # Important: don't forge rankings when homepage provides none.
    return result


def emit_pages(cid: str, metas: list, kind: str = 'series') -> None:
    base = OUT / 'catalog' / kind
    for offset in range(0, len(metas) + 100, 100):
        page = {'metas': metas[offset:offset + 100]}
        if offset == 0:
            dump_json(base / f'{cid}.json', page)
        dump_json(base / cid / f'skip={offset}.json', page)


def proper_poster(item: dict, base: str) -> str:
    # TMDB CDN images are more reliable for client rendering than site login images.
    return (item.get('match') or {}).get('poster') or item.get('poster') or f'{base}/placeholder.png'


def good_titles(titles: dict) -> tuple[dict, list]:
    """Quarantine malformed imported entries rather than re-publishing junk."""
    valid, rejected = {}, []
    for oid, item in titles.items():
        title = clean_title(item.get('title'))
        if not str(oid).isdigit() or not title:
            rejected.append({'oceanveil_id': oid, 'title': item.get('title', '')})
            continue
        valid[oid] = item
    return valid, rejected


def publish(titles: dict, homepage: dict, stats: dict) -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copy(ROOT / 'static' / 'placeholder.png', OUT / 'placeholder.png')
    (OUT / '.nojekyll').touch()
    base = pages_base_url()
    feeds = catalogs(titles, homepage)
    home = homepage.get('sections', {})
    ordered = OrderedDict()
    # Stable order and IDs: not derived from daily rotating promo names.
    for slug in dict.fromkeys([*homepage.get('row_order', []), *home]):
        section = home.get(slug, {})
        ordered[f'home-{slug}'] = section.get('name') or HOME_SECTIONS.get(slug, slug)
    for slug, name in HOME_SECTIONS.items():
        ordered.setdefault(f'home-{slug}', name)
    ordered.update(FILTERS)
    ordered.setdefault('oceanveil-linked', 'OceanVeil — IMDb-matched (compatible sources)')
    feeds['oceanveil-linked'] = [oid for oid, item in titles.items() if (item.get('match') or {}).get('imdb_id')]

    previews = {}
    # Every record retains its own OceanVeil ID in the persistent state, but
    # verified matches use IMDb meta IDs when published to clients.
    for oid, item in titles.items():
        match = item.get('match') or {}
        imdb_id = match.get('imdb_id', '')
        linked = bool(IMDb.fullmatch(imdb_id))
        meta_id = imdb_id if linked else f'oceanveil:{oid}'
        kind = match.get('type', 'series') if linked else 'series'
        poster = proper_poster(item, base)
        name = match.get('name') if linked else item['title']
        preview = {'id': meta_id, 'type': kind, 'name': name or item['title'],
                   'poster': poster, 'posterShape': 'poster'}
        previews[oid] = preview
        summary = []
        if match.get('description'):
            summary.append(match['description'])
        else:
            summary.append('Listed on OceanVeil. No verified IMDb metadata match was found. Playback through other addons is not assured.')
        if item.get('rating'):
            summary.append(f'Website age label: {item["rating"]}.')
        if item.get('genre'):
            summary.append(f'Website category: {item["genre"]}.')
        meta = {**preview,
                'description': ' '.join(summary),
                'website': item['url'],
                'links': [{'name': 'OceanVeil title page', 'category': 'watch', 'url': item['url']}],
                'videos': match.get('videos', []) if linked else []}
        if match.get('year'):
            meta['releaseInfo'] = str(match['year'])
        if match.get('background'):
            meta['background'] = match['background']
        if match.get('genres'):
            meta['genres'] = match['genres']
        if linked:
            # Two different site rows (Dub/Sub) may resolve to one shared IMDb ID.
            # One file per IMDb identity; keep a complete episode list if available.
            target = OUT / 'meta' / kind / f'{meta_id}.json'
            if target.exists():
                old = json.loads(target.read_text('utf8')).get('meta', {})
                if len(old.get('videos', [])) > len(meta['videos']):
                    continue
        dump_json(OUT / 'meta' / kind / f'{meta_id}.json', {'meta': meta})

    catalog_defs = []
    catalog_counts = {}
    movie_defs = []
    # Preserve legacy series catalog IDs. Put movies in a SECOND row rather
    # than incorrectly labeling a movie as a series (breaks stream lookup).
    for cid, name in ordered.items():
        seen = set()
        by_type = {'series': [], 'movie': []}
        for oid in feeds.get(cid, []):
            if oid not in previews:
                continue
            preview = previews[oid]
            key = (preview['type'], preview['id'])
            if key in seen:
                continue
            seen.add(key)
            by_type[preview['type']].append(preview)
        catalog_defs.append({'type': 'series', 'id': cid, 'name': name,
                            'extra': [{'name': 'skip', 'isRequired': False}]})
        emit_pages(cid, by_type['series'], 'series')
        catalog_counts[cid] = len(by_type['series'])
        if by_type['movie']:
            movie_id = cid + '-movies'
            movie_defs.append({'type': 'movie', 'id': movie_id, 'name': name + ' · Films',
                              'extra': [{'name': 'skip', 'isRequired': False}]})
            emit_pages(movie_id, by_type['movie'], 'movie')
            catalog_counts[movie_id] = len(by_type['movie'])
    catalog_defs += movie_defs
    dump_json(OUT / 'manifest.json', {
        'id': 'community.oceanveil.auto.catalog', 'version': '4.0.0',
        'name': 'OceanVeil Auto Catalog',
        'description': 'Daily OceanVeil discovery with verified IMDb matching and detailed metadata. Streams require separately installed, compatible sources.',
        'resources': [
            'catalog',
            {'name': 'meta', 'types': ['series', 'movie'], 'idPrefixes': ['tt', 'oceanveil:']},
        ],
        'types': ['series', 'movie'], 'catalogs': catalog_defs,
        'behaviorHints': {'configurable': False}
    })
    dump_json(OUT / 'status.json', {
        'last_build_utc': now_utc(),
        'known_title_count': len(titles),
        'imdb_matched_count': sum(1 for t in titles.values() if (t.get('match') or {}).get('imdb_id')),
        'unmatched_count': sum(1 for t in titles.values() if not (t.get('match') or {}).get('imdb_id')),
        'new_titles_this_run': stats.get('new', 0),
        'official_homepage_fetched': stats.get('home_ok', False),
        'homepage_rows_fresh': stats.get('home_fresh', False),
        'homepage_last_success_utc': homepage.get('last_success_utc'),
        'homepage_row_counts': {v['name']: len(v['ids']) for v in home.values()},
        'sticky_home_rows': [{'catalog_id': f'home-{key}', 'display_name': value['name'],
                              'items': len(value['ids']), 'kind': 'fixed' if key in HOME_SECTIONS else 'promo-slot'}
                             for key, value in home.items()],
        'catalog_counts': catalog_counts,
        'discovered_direct_links': stats.get('discovered_urls', 0),
        'detail_pages_attempted': stats.get('details_attempted', 0),
        'metadata': stats.get('metadata', {}),
        'pages_base_url': base,
        'notice': 'Matching an IMDb ID does NOT guarantee playable streams. Stream providers are separate; no source bypass or video extraction.'
    })


def build() -> None:
    fetcher = GentleFetcher()
    state = load_state()
    old_ids = set(state['titles'])
    titles, rejected = good_titles(state['titles'])
    if rejected:
        print('Quarantined previously imported invalid titles:', len(rejected))
    today = now_utc()[:10]
    homepage = state.get('homepage') or {'sections': {}}
    homepage.setdefault('sections', {})
    homepage.setdefault('last_success_utc', state.get('homepage_updated_utc'))
    home_html = fetcher.get(f'{OFFICIAL}/')
    home_fresh = False
    fresh = {}
    if home_html:
        home_titles, sections = parse_homepage(home_html, f'{OFFICIAL}/')
        fresh.update(home_titles)
        if sections:
            homepage = reconcile_homepage(homepage, sections)
            home_fresh = True
        else:
            print('WARNING: homepage loaded but no identifiable card sections; retaining cached sections.')
    else:
        print('WARNING: official homepage unreachable; retaining cached promotional rows.')
    browse_titles, observed = browse_index(fetcher)
    fresh.update({oid: entry for oid, entry in browse_titles.items() if oid not in fresh})
    sitemap_urls = scan_sitemaps(fetcher)
    secondary = secondary_index(fetcher)
    fresh.update({oid: entry for oid, entry in secondary.items() if oid not in fresh})
    # Try detail pages for observed links without a title, including sitemap-only IDs.
    # This budget is per build. A large sitemap takes several days to fill safely.
    urls = sorted(sitemap_urls | observed, key=lambda s: int(ocean_id(s)))
    attempted = 0
    for url in urls:
        oid = ocean_id(url)
        if not oid or oid in titles or oid in fresh:
            continue
        if attempted >= MAX_DETAILS:
            break
        attempted += 1
        html = fetcher.get(url)
        if html:
            record = details_from_official(html, url)
            if record:
                fresh[oid] = record
    if not titles and not fresh:
        raise RuntimeError('No metadata sources accessible and no previous state. Refusing empty first deployment.')
    for oid, record in fresh.items():
        put_title(titles, oid, record, today)
    # A row can contain a link without usable title: keep order but filter
    # unknown IDs during publish so no fabricated shows enter the feed.
    metadata_stats = add_metadata(titles, TMDBMatcher())
    state = {'titles': titles, 'homepage': homepage, 'quarantine': rejected}
    dump_json(STATE_FILE, state)
    stats = {'new': len(set(titles) - old_ids), 'home_ok': bool(home_html),
             'home_fresh': home_fresh, 'discovered_urls': len(sitemap_urls | observed),
             'details_attempted': attempted, 'metadata': metadata_stats}
    publish(titles, homepage, stats)
    print(f'Build finished: {len(titles)} titles, +{stats["new"]} this run, '
          f'{len(homepage["sections"])} homepage rows, fresh={home_fresh}')


if __name__ == '__main__':
    build()
