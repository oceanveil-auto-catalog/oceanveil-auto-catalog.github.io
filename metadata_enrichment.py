"""Conservative TMDB -> IMDb matching for Stremio/Nuvio interoperability.

Only public metadata is retrieved. We do NOT fetch or provide any video streams.
An IMDb ID enables other installed, compatible providers to answer stream requests.
No title match is invented. External IDs are verified against TMDB external_ids.
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone

import requests

TMDB_API = 'https://api.themoviedb.org/3'
TMDB_IMAGE = 'https://image.tmdb.org/t/p'
IMDb = re.compile(r'^tt\d{7,11}$')
MAX_MATCHES = int(os.getenv('MAX_MATCHES_PER_RUN', '80'))
MAX_SEASONS = int(os.getenv('MAX_SEASONS_PER_TITLE', '12'))
MAX_EPISODES = int(os.getenv('MAX_EPISODES_PER_TITLE', '600'))


def clean_name(value: str) -> str:
    value = re.sub(r'^\s*\[(?:dub|sub)\]\s*', '', str(value), flags=re.I)
    value = re.sub(r'\s*\[(?:dub|sub)\]\s*$', '', value, flags=re.I)
    value = re.sub(r'\s*\((?:english dub|dub|sub)\)\s*$', '', value, flags=re.I)
    value = unicodedata.normalize('NFKC', value).lower()
    value = re.sub(r'[^\w\s]', ' ', value, flags=re.UNICODE)
    return ' '.join(value.split())


def title_matches(source: str, candidate: dict) -> bool:
    """Require exact normalized title, or exceptional near-exact transliteration.

    A fuzzy match alone (even at 99%) is NOT sufficient for changing identity.
    """
    key = clean_name(source)
    if not key or len(key) < 5:
        return False
    aliases = [candidate.get(k, '') for k in ('name', 'original_name', 'title', 'original_title')]
    return any(clean_name(alias) == key for alias in aliases if alias)


def fresh(iso: str, days: int) -> bool:
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - dt).total_seconds() < days * 86400
    except (ValueError, TypeError):
        return False


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def year_of(value: str | None) -> int | None:
    found = re.match(r'^(\d{4})', value or '')
    return int(found[1]) if found else None


class TMDBMatcher:
    def __init__(self, api_key: str | None = None, session=None):
        self.api_key = api_key if api_key is not None else os.environ.get('TMDB_API_KEY', '')
        self.session = session or requests.Session()
        self.calls = 0
        self.failures = 0

    @property
    def enabled(self):
        return bool(self.api_key)

    def api(self, path: str, params: dict | None = None) -> dict | None:
        if not self.enabled:
            return None
        options = {'api_key': self.api_key, 'language': 'en-US', **(params or {})}
        try:
            response = self.session.get(TMDB_API + path, params=options, timeout=14)
            self.calls += 1
            if response.status_code == 429:
                self.failures += 1
                return None
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            self.failures += 1
            return None

    def episode_list(self, tmdb_id: int, seasons: list, imdb_id: str) -> list:
        videos = []
        for season in sorted(seasons, key=lambda row: row.get('season_number', 0)):
            sn = season.get('season_number')
            if not isinstance(sn, int) or sn < 0:
                continue
            if len({v['season'] for v in videos}) >= MAX_SEASONS or len(videos) >= MAX_EPISODES:
                break
            details = self.api(f'/tv/{tmdb_id}/season/{sn}')
            if not details:
                continue
            for episode in details.get('episodes') or []:
                ep = episode.get('episode_number')
                if not isinstance(ep, int) or ep < 1 or len(videos) >= MAX_EPISODES:
                    continue
                row = {'id': f'{imdb_id}:{sn}:{ep}', 'title': episode.get('name') or f'Episode {ep}',
                       'season': sn, 'episode': ep}
                if episode.get('air_date'):
                    row['released'] = episode['air_date'] + 'T00:00:00.000Z'
                if episode.get('still_path'):
                    row['thumbnail'] = TMDB_IMAGE + '/w500' + episode['still_path']
                if episode.get('overview'):
                    row['overview'] = episode['overview']
                videos.append(row)
        return videos

    def match(self, title: str, source_year: int | None = None) -> dict | None:
        """Cross-check exact search matches and IMDb external ids.

        Multiple different IMDb IDs on equally valid matches are ambiguous:
        return None rather than secretly picking a film instead of its TV series.
        """
        if not self.enabled:
            return None
        query = re.sub(r'^\s*\[(?:dub|sub)\]\s*', '', title, flags=re.I)
        query = re.sub(r'\s*\[(?:dub|sub)\]\s*$', '', query, flags=re.I)
        query = re.sub(r'\s*\((?:english dub|dub|sub)\)\s*$', '', query, flags=re.I)
        choices = []
        for media in ('tv', 'movie'):
            search = self.api(f'/search/{media}', {'query': query, 'include_adult': 'true'})
            if not search:
                continue
            for item in (search.get('results') or [])[:12]:
                if not title_matches(title, item):
                    continue
                yr = year_of(item.get('first_air_date') or item.get('release_date'))
                if source_year and yr and abs(int(source_year) - yr) > 1:
                    continue
                ext = self.api(f'/{media}/{item["id"]}/external_ids')
                imdb_id = (ext or {}).get('imdb_id')
                if not imdb_id or not IMDb.fullmatch(imdb_id):
                    continue
                choices.append((media, item, imdb_id))
        identities = {(media, imdb_id) for media, _, imdb_id in choices}
        if len(identities) != 1:
            return None
        media, item, imdb_id = choices[0]
        info = self.api(f'/{media}/{item["id"]}')
        if not info:
            return None
        matched = {
            'imdb_id': imdb_id, 'type': 'series' if media == 'tv' else 'movie',
            'tmdb_id': item['id'], 'tmdb_type': media,
            'name': info.get('name') or info.get('title') or title,
            'description': info.get('overview') or '',
            'poster': TMDB_IMAGE + '/w500' + info['poster_path'] if info.get('poster_path') else '',
            'background': TMDB_IMAGE + '/original' + info['backdrop_path'] if info.get('backdrop_path') else '',
            'year': year_of(info.get('first_air_date') or info.get('release_date')),
            'genres': [g.get('name') for g in info.get('genres', []) if g.get('name')],
            'matched_utc': iso_now(),
        }
        if media == 'tv':
            matched['videos'] = self.episode_list(item['id'], info.get('seasons') or [], imdb_id)
            matched['episodes_complete'] = len(info.get('seasons') or []) <= MAX_SEASONS and len(matched['videos']) < MAX_EPISODES
        else:
            matched['videos'] = []
            matched['episodes_complete'] = True
        return matched


def add_metadata(titles: dict, matcher: TMDBMatcher) -> dict:
    """Cache successful resolutions and throttle nonmatches. Safe on site errors."""
    stats = {'metadata_matched_new': 0, 'metadata_unmatched_checked': 0,
             'metadata_reused': 0, 'metadata_not_attempted': 0}
    attempted = 0
    # Prioritize homepage selection, then other newly seen show IDs.
    for oid, item in titles.items():
        existing = item.get('match') or {}
        if existing.get('imdb_id') and IMDb.fullmatch(existing['imdb_id']) and fresh(existing.get('matched_utc'), 30):
            stats['metadata_reused'] += 1
            continue
        if not matcher.enabled or attempted >= MAX_MATCHES:
            stats['metadata_not_attempted'] += 1
            continue
        if not existing and fresh(item.get('match_checked_utc'), 45):
            continue
        attempted += 1
        matched = matcher.match(item['title'], item.get('year'))
        item['match_checked_utc'] = iso_now()
        if matched:
            item['match'] = matched
            stats['metadata_matched_new'] += 1
        else:
            # Existing verified mapping survives API outages/revalidation failure.
            # A changed title already invalidates the mapping in put_title().
            stats['metadata_unmatched_checked'] += 1
    stats['tmdb_api_calls'] = matcher.calls
    stats['tmdb_api_failures'] = matcher.failures
    return stats
