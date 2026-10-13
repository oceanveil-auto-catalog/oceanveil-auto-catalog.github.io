"""Read OceanVeil's public catalog API and its homepage row layout.

Everything here is public, logged-out data:
- /api/v1/anime_titles (+ ids[], tag_ids[], genre_ids[], is_simulcast filters)
- /api/v1/newest_addition_anime_titles, /api/v1/trending_anime_titles
- The homepage row layout, which the site embeds in its HTML as
  window.__NUXT__.config.public.generalTopSettings.sections
No sign-in, cookies, or video URLs are used.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

SITE = 'https://oceanveil.net'
API = SITE + '/api/v1'
IMAGE = 'https://image.oceanveil.net/public/anime_titles'
UA = 'OceanVeilCatalogBot/2.0 (personal Stremio catalog; public pages only)'
SITE_TZ = ZoneInfo('America/Los_Angeles')  # the site's own configured timezone

TAG_DUBBED = 62
TAG_NSFW = 56

# Row types the site renders as title rows. Episode sliders, banners and
# per-user rows (watchlist, continue watching, recommendations) are skipped.
TITLE_ROW_TYPES = {'anime-title-slider', 'anime-title-info-slider'}
SUPPORTED_SEARCH = {'id', 'simulcast', 'newest_addition', 'trending', 'completed', 'tag', 'genre'}

# Used only if the homepage config can't be read on a given run.
DEFAULT_SECTIONS = [
    {'type': 'anime-title-slider', 'name': 'simulsub_oa', 'header': 'SimulCast Lineup : New Every Week', 'search_type': 'simulcast'},
    {'type': 'anime-title-slider', 'name': 'newest_addition_oa', 'header': 'Newest Addition', 'search_type': 'newest_addition'},
    {'type': 'anime-title-slider', 'name': 'trending_oa', 'header': 'Ranking', 'search_type': 'trending'},
    {'type': 'anime-title-slider', 'name': 'dubbed_oa', 'header': 'English Dubbed Titles', 'search_type': 'tag', 'ids': [TAG_DUBBED]},
    {'type': 'anime-title-slider', 'name': 'completed_oa', 'header': 'Completed', 'search_type': 'completed'},
    {'type': 'anime-title-slider', 'name': 'romance_section_oa', 'header': 'Romance Section', 'search_type': 'genre', 'ids': [2]},
    {'type': 'anime-title-slider', 'name': 'mature_section_oa', 'header': 'Young Adult Section', 'search_type': 'genre', 'ids': [5]},
    {'type': 'anime-title-slider', 'name': 'bl_section_oa', 'header': 'BL Section', 'search_type': 'genre', 'ids': [4]},
]


def poster_url(title_id) -> str:
    n = f'{int(title_id):09d}'
    return f'{IMAGE}/{n[:3]}/{n[:6]}/{n}/{n}_vertical_with_logo.jpg_small.webp'


# ---------------------------------------------------------------- config parsing

def _matching_brace(text: str, start: int) -> int:
    """Index just past the bracket that closes text[start], skipping strings."""
    pairs = {'{': '}', '[': ']'}
    stack, i, quote = [], start, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == '\\':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in '"\'':
            quote = c
        elif c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    raise ValueError('unbalanced brackets')


def js_literal_to_json(src: str) -> str:
    """Convert a JS object literal (bare keys, !0/!1, void 0) to JSON."""
    out, i, n = [], 0, len(src)
    last = ''  # last significant non-space character emitted
    while i < n:
        c = src[i]
        if c in '"\'':
            j, buf = i + 1, []
            while j < n and src[j] != c:
                if src[j] == '\\':
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                buf.append('\\"' if src[j] == '"' else src[j])
                j += 1
            out.append('"' + ''.join(buf).replace("\\'", "'") + '"')
            last, i = '"', j + 1
        elif c == '!' and i + 1 < n and src[i + 1] in '01':
            out.append('true' if src[i + 1] == '0' else 'false')
            last, i = 'e', i + 2
        elif c.isalpha() or c in '_$':
            j = i
            while j < n and (src[j].isalnum() or src[j] in '_$'):
                j += 1
            word = src[i:j]
            k = j
            while k < n and src[k].isspace():
                k += 1
            if last in '{,' and k < n and src[k] == ':':
                out.append(json.dumps(word))
            elif word == 'void':
                m = re.match(r'\s*0', src[j:])
                j += m.end() if m else 0
                out.append('null')
            elif word == 'undefined':
                out.append('null')
            else:
                out.append(word)
            last, i = word[-1], j
        else:
            out.append(c)
            if not c.isspace():
                last = c
            i += 1
    return ''.join(out)


def parse_home_sections(html: str, key: str = 'generalTopSettings') -> list[dict]:
    """Return the homepage's ordered row definitions embedded in the page HTML."""
    m = re.search(re.escape(key) + r'\s*:\s*\{', html or '')
    if not m:
        raise ValueError(f'{key} not found in page')
    start = m.end() - 1
    obj = json.loads(js_literal_to_json(html[start:_matching_brace(html, start)]))
    sections = obj.get('sections')
    if not isinstance(sections, list):
        raise ValueError('sections missing')
    return sections


def _parse_when(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=SITE_TZ)
    except ValueError:
        return None


def active_title_rows(sections: list[dict], now: datetime | None = None) -> list[dict]:
    """Rows a logged-out visitor would see today, in homepage order."""
    now = now or datetime.now(SITE_TZ)
    rows = []
    for s in sections:
        if not isinstance(s, dict) or s.get('type') not in TITLE_ROW_TYPES:
            continue
        if s.get('search_type') not in SUPPORTED_SEARCH or s.get('is_subscription_onboarding_only'):
            continue
        start, end = _parse_when(s.get('start_at')), _parse_when(s.get('end_at'))
        if (start and now < start) or (end and now > end):
            continue
        rows.append(s)
    return rows


def row_key(section: dict) -> str | None:
    """Stable identity for fixed rows; None for curated promo rows (slotted by position)."""
    st = section.get('search_type')
    if st == 'id':
        return None
    if st in ('tag', 'genre'):
        ids = section.get('ids') or []
        return f'{st}-{ids[0]}' if ids else None
    return {'newest_addition': 'newest', 'trending': 'ranking'}.get(st, st)


# ---------------------------------------------------------------- API client

class OceanVeil:
    def __init__(self, session=None, delay=0.6):
        self.session = session or requests.Session()
        self.session.headers.update({'User-Agent': os.environ.get('OCEANVEIL_USER_AGENT') or UA,
                                     'Accept': 'application/json, text/html;q=0.8'})
        # OceanVeil only lists licensed titles to some regions. If the build machine's
        # region sees a cut-down list, route OceanVeil requests (only) through a proxy.
        proxy = os.environ.get('OCEANVEIL_PROXY', '').strip()
        if proxy:
            self.session.proxies = {'http': proxy, 'https': proxy}
        self.proxied = bool(proxy)
        self.delay = delay
        self._last = 0.0
        self.events: list[dict] = []

    def _get(self, url, params=None):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            r = self.session.get(url, params=params, timeout=25)
            self.events.append({'url': r.url, 'status': r.status_code})
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            self.events.append({'url': url, 'error': str(e)[:140]})
            return None

    def titles(self, endpoint='anime_titles', params=None) -> list[dict] | None:
        r = self._get(f'{API}/{endpoint}', params)
        if r is None:
            return None
        try:
            data = r.json().get('data')
        except ValueError:
            return None
        if not isinstance(data, list):
            return None
        out = []
        for item in data:
            attrs = dict((item or {}).get('attributes') or {})
            if attrs.get('id') is not None and attrs.get('name'):
                rel = ((item or {}).get('relationships') or {}).get('animeEpisodes') or {}
                if isinstance(rel.get('data'), list):
                    attrs['episodeCount'] = len(rel['data'])
                out.append(attrs)
        return out

    def home_sections(self) -> tuple[list[dict], bool]:
        r = self._get(SITE + '/')
        if r is not None:
            # The page is UTF-8 but its Content-Type has no charset, so requests would
            # decode it as Latin-1 and garble row names ("This Fallâs", "Week ð").
            r.encoding = 'utf-8'
            try:
                return parse_home_sections(r.text), True
            except (ValueError, json.JSONDecodeError) as e:
                self.events.append({'url': SITE, 'error': f'home config parse: {e}'[:140]})
        return DEFAULT_SECTIONS, False

    def resolve_row(self, section: dict) -> list[dict] | None:
        st, ids = section.get('search_type'), section.get('ids') or []
        if st == 'id':
            if not ids:
                return []
            found = self.titles('anime_titles', [('ids[]', i) for i in ids])
            if found is None:
                return None
            order = {int(i): n for n, i in enumerate(ids)}
            return sorted(found, key=lambda t: order.get(int(t['id']), 1e9))
        if st == 'simulcast':
            return self.titles('anime_titles', {'is_simulcast': 'true'})
        if st == 'newest_addition':
            return self.titles('newest_addition_anime_titles', {'limit': 40})
        if st == 'trending':
            return self.titles('trending_anime_titles', {'limit': 50})
        if st == 'tag' and ids:
            return self.titles('anime_titles', [('tag_ids[]', ids[0])])
        if st == 'genre' and ids:
            return self.titles('anime_titles', [('genre_ids[]', ids[0])])
        return []  # 'completed' is filtered from the full list by the caller
