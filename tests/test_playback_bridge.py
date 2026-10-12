import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build
from metadata_enrichment import TMDBMatcher, add_metadata, clean_name


class Response:
    def __init__(self, payload, code=200):
        self._payload = payload
        self.status_code = code
    def json(self): return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f'HTTP {self.status_code}')


class TMDBFixture:
    def get(self, url, params=None, timeout=None):
        path = url.replace('https://api.themoviedb.org/3', '')
        if path == '/search/tv':
            return Response({'results': [{'id': 101, 'name': 'Starlight Planet', 'original_name': 'Starlight Planet', 'first_air_date': '2025-03-10'}]})
        if path == '/search/movie':
            return Response({'results': []})
        if path == '/tv/101/external_ids':
            return Response({'imdb_id': 'tt12345678'})
        if path == '/tv/101':
            return Response({'id':101, 'name':'Starlight Planet', 'overview': 'A verified series description.',
                             'poster_path':'/poster.png', 'backdrop_path':'/bg.png',
                             'first_air_date':'2025-03-10', 'genres':[{'name':'Animation'}],
                             'seasons':[{'season_number':1}]})
        if path == '/tv/101/season/1':
            return Response({'episodes':[{'episode_number':1,'name':'First Light','air_date':'2025-03-10','still_path':'/still.jpg'},
                                         {'episode_number':2,'name':'New Stars','air_date':'2025-03-17'}]})
        raise AssertionError(path)


class WrongTMDBFixture(TMDBFixture):
    def get(self, url, params=None, timeout=None):
        path = url.replace('https://api.themoviedb.org/3', '')
        if path == '/search/tv':
            return Response({'results':[{'id':101,'name':'Starlight Plant','original_name':'Starlight Plant'}]})
        if path == '/search/movie':
            return Response({'results':[]})
        raise AssertionError('Should never fetch externals for a similar but different name: ' + path)


class TestPlaybackCompatibility(unittest.TestCase):
    def test_clean_name_dub_and_episode_junk(self):
        self.assertEqual(clean_name('[Dub] Starlight Planet'), 'starlight planet')
        self.assertEqual(build.clean_title('episode.2'), '')
        self.assertEqual(build.clean_title('episode.1'), '')
        self.assertEqual(build.clean_title('EP-21'), '')
        self.assertEqual(build.clean_title('Starlight Planet'), 'Starlight Planet')

    def test_root_pages_url_for_user_site_repo(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'oceanveil-auto-catalog/oceanveil-auto-catalog.github.io'}, clear=True):
            self.assertEqual(build.pages_base_url(), 'https://oceanveil-auto-catalog.github.io')
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'alice/custom-catalog'}, clear=True):
            self.assertEqual(build.pages_base_url(), 'https://alice.github.io/custom-catalog')

    def test_resolved_imdb_episode_ids_metadata(self):
        matcher = TMDBMatcher('TESTKEY', TMDBFixture())
        result = matcher.match('[Dub] Starlight Planet')
        self.assertEqual(result['imdb_id'], 'tt12345678')
        self.assertEqual(result['type'], 'series')
        self.assertEqual(result['poster'], 'https://image.tmdb.org/t/p/w500/poster.png')
        self.assertEqual([v['id'] for v in result['videos']], ['tt12345678:1:1', 'tt12345678:1:2'])
        self.assertEqual(result['videos'][0]['title'], 'First Light')
        self.assertTrue(result['episodes_complete'])
        self.assertIsNone(TMDBMatcher('TESTKEY', WrongTMDBFixture()).match('Starlight Planet'))

    def test_publish_produces_playback_compatible_and_legacy_entries(self):
        match = TMDBMatcher('TESTKEY', TMDBFixture()).match('Starlight Planet')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'static').mkdir()
            (root/'static'/'placeholder.png').write_bytes(b'png')
            titles = {
                '1': {'title': 'Starlight Planet', 'url': build.OFFICIAL+'/anime_titles/1', 'first_seen': '2026-01-01', 'match': match},
                '2': {'title': '[Dub] Starlight Planet', 'url': build.OFFICIAL+'/anime_titles/2', 'first_seen': '2026-01-01', 'match': match},
                '3': {'title': 'Unmapped Niche Show', 'url': build.OFFICIAL+'/anime_titles/3', 'first_seen': '2026-01-01'},
            }
            homepage = {'sections': {'promo-slot-01': {'name': 'Autumn Specials', 'ids': ['1', '2', '3']}},
                        'row_order':['promo-slot-01']}
            with patch.object(build, 'ROOT', root), patch.object(build, 'OUT', root/'public'):
                build.publish(titles, homepage, {'new':0})
                rootout = root/'public'
                man = json.loads((rootout/'manifest.json').read_text())
                self.assertEqual(man['id'], 'community.oceanveil.auto.catalog')
                self.assertEqual(man['catalogs'][0]['id'], 'home-promo-slot-01')
                self.assertIn('meta', [r['name'] if isinstance(r, dict) else r for r in man['resources']])
                self.assertNotIn('stream', [r['name'] if isinstance(r, dict) else r for r in man['resources']])
                preview = json.loads((rootout/'catalog/series/home-promo-slot-01.json').read_text())['metas']
                self.assertEqual([m['id'] for m in preview], ['tt12345678', 'oceanveil:3'])
                self.assertEqual(preview[0]['poster'], 'https://image.tmdb.org/t/p/w500/poster.png')
                meta = json.loads((rootout/'meta/series/tt12345678.json').read_text())['meta']
                self.assertEqual([v['id'] for v in meta['videos']], ['tt12345678:1:1','tt12345678:1:2'])
                other = json.loads((rootout/'meta/series/oceanveil:3.json').read_text())['meta']
                self.assertEqual(other['videos'], [])
                status = json.loads((rootout/'status.json').read_text())
                self.assertEqual(status['imdb_matched_count'], 2)
                self.assertEqual(status['pages_base_url'].startswith('https://'), True)

    def test_old_state_replaces_corrupt_show_but_keeps_valid_ids(self):
        clean, errors = build.good_titles({'1': {'title':'episode.1'},'2':{'title':'Ocean Tale'}})
        self.assertEqual(list(clean), ['2'])
        self.assertEqual(errors[0]['title'], 'episode.1')
        previous = {'title': 'Ocean Tale', 'poster': '', 'match': {'imdb_id': 'tt12345678', 'matched_utc':'2026-10-01'}}
        titles = {'2': previous}
        build.put_title(titles, '2', {'title': 'Ocean Tale'}, '2026-10-12')
        self.assertEqual(titles['2']['match']['imdb_id'], 'tt12345678')
        build.put_title(titles, '2', {'title': 'Actually Different Show'}, '2026-10-13')
        self.assertNotIn('match', titles['2'])


if __name__ == '__main__':
    unittest.main()

class TestRefreshSafety(unittest.TestCase):
    def test_transient_tmdb_outage_keeps_verified_mapping(self):
        from metadata_enrichment import add_metadata
        old = {'imdb_id': 'tt12345678', 'type': 'series', 'matched_utc': '2025-01-01'}
        titles = {'90': {'title': 'Ocean Tale', 'match': old.copy()}}
        class NoResult:
            enabled = True
            calls = 0
            failures = 1
            def match(self, title, source_year=None): return None
        add_metadata(titles, NoResult())
        self.assertEqual(titles['90']['match']['imdb_id'], 'tt12345678')

    def test_movies_use_movie_resource_and_own_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'static').mkdir()
            (root/'static'/'placeholder.png').write_bytes(b'x')
            titles = {'9': {'title':'Feature Show','url':build.OFFICIAL+'/anime_titles/9','first_seen':'2026-01-01',
                            'match': {'imdb_id':'tt88888888','type':'movie','name':'Feature Show',
                                      'description':'Movie description', 'videos': []}}}
            with patch.object(build, 'ROOT', root), patch.object(build, 'OUT', root/'public'):
                build.publish(titles, {'sections':{}}, {})
                movie = json.loads((root/'public/catalog/movie/oceanveil-anime-movies.json').read_text())['metas']
                self.assertEqual(movie[0]['id'], 'tt88888888')
                self.assertEqual(movie[0]['type'], 'movie')
                self.assertTrue((root/'public/meta/movie/tt88888888.json').exists())
                manifest = json.loads((root/'public/manifest.json').read_text())
                self.assertEqual(next(c for c in manifest['catalogs'] if c['id']=='oceanveil-anime')['type'], 'series')
                self.assertEqual(next(c for c in manifest['catalogs'] if c['id']=='oceanveil-anime-movies')['type'], 'movie')
