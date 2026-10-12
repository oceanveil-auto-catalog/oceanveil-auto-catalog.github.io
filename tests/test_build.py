import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build as generator
import enrichment


class OfflineFeed:
    events = []
    pages = {
        generator.SECONDARY: '<html><a href="/anime/angel-beats">Angel Beats!</a></html>',
        'https://manhwaindex.com/anime/angel-beats': '<h1>Angel Beats!</h1><a href="https://oceanveil.net/anime_titles/434">Watch</a>',
        'https://oceanveil.net/': '<a href="/anime_titles/500"><img alt="Added from OceanVeil" src="/poster500.jpg"></a>',
        'https://oceanveil.net/anime_titles': '',
        'https://oceanveil.net/sitemap.xml': '<urlset><url><loc>https://oceanveil.net/anime_titles/501</loc></url></urlset>',
        'https://oceanveil.net/sitemap_index.xml': '',
        'https://oceanveil.net/anime_titles/501': '<meta property="og:title" content="New from sitemap" />',
    }

    def get(self, url):
        return self.pages.get(url)


class FakeEnricher:
    tmdb_key = 'fakekey'
    tvdb_key = 'fakekey'
    mdb_key = 'fakekey'
    events = []

    def enrich(self, title, previous=None):
        if title == 'Angel Beats!':
            return {'id': 'tmdb:1234', 'tmdb_id': '1234', 'tmdb_type': 'tv', 'tvdb_id': '5678',
                    'ratings': {'imdb': 8.9}, 'poster': 'https://image.tmdb.org/t/p/w500/poster.jpg',
                    'description': 'Anime description', 'refresh_after': '2099-01-01'}
        if title == 'New from sitemap':
            return {'id': 'tmdb:9999', 'tmdb_id': '9999', 'tmdb_type': 'movie',
                    'refresh_after': '2099-01-01'}
        return {'id': 'kitsu:888', 'refresh_after': '2099-01-01'} if title == 'Added from OceanVeil' else {}


class Tests(unittest.TestCase):
    def test_user_site_vs_project_site_url(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "oceanveil-auto-catalog/oceanveil-auto-catalog.github.io"}, clear=True):
            self.assertEqual(generator.pages_base_url(), 'https://oceanveil-auto-catalog.github.io')
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "oceanveil-auto-catalog/oceanveil-auto-catalog"}, clear=True):
            self.assertEqual(generator.pages_base_url(), 'https://oceanveil-auto-catalog.github.io/oceanveil-auto-catalog')

    def test_nuvio_compatible_ids_and_metadata(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            (root / 'static').mkdir()
            (root / 'static' / 'placeholder.png').write_bytes(b'poster')
            env = {'GITHUB_REPOSITORY': 'oceanveil-auto-catalog/oceanveil-auto-catalog.github.io', 'ENABLE_BROWSER_DISCOVERY': '0'}
            with patch.dict(os.environ, env), patch.object(generator, 'ROOT', root), patch.object(generator, 'STATE_FILE', root/'state.json'), patch.object(generator, 'OUT', root/'public'), patch.object(generator, 'GentleFetcher', OfflineFeed), patch.object(generator, 'Enricher', FakeEnricher):
                generator.build()
                pub = root / 'public'
                manifest = json.loads((pub / 'manifest.json').read_text())
                self.assertEqual(manifest['catalogs'][0]['name'], 'OceanVeil • Simulcast')
                self.assertEqual(len([c for c in manifest['catalogs'] if c['type']=='series']), 8)
                self.assertTrue((pub/'catalog/series/oceanveil-mix-all.json').exists())
                self.assertEqual(manifest['catalogs'][0]['extra'][0]['name'], 'skip')
                series = json.loads((pub/'catalog/series/oceanveil-anime.json').read_text())['metas']
                movies = json.loads((pub/'catalog/movie/oceanveil-movies.json').read_text())['metas']
                self.assertEqual(len(series), 1)
                self.assertEqual(len(movies), 0)
                self.assertIn('tmdb:1234', [m['id'] for m in series])
                self.assertNotIn('tmdb:9999', [m['id'] for m in movies])  # no trusted poster, not listed
                entry = next(m for m in series if m['id'] == 'tmdb:1234')
                self.assertEqual(entry['imdbRating'], '8.9')
                self.assertEqual(entry['poster'], 'https://image.tmdb.org/t/p/w500/poster.jpg')
                self.assertEqual(json.loads((pub/'status.json').read_text())['identifier_counts']['tmdb'], 1)
                self.assertEqual(json.loads((pub/'status.json').read_text())['enrichment_counts']['mdb_ratings'], 1)
                # Rerun with source inaccessible, no loss of saved standard IDs.
                with patch.object(generator, 'GentleFetcher', type('Empty', (), {'events': [], 'get': lambda self, url: None})):
                    generator.build()
                    items = json.loads((pub/'catalog/series/oceanveil-anime.json').read_text())['metas']
                    self.assertIn('tmdb:1234', [m['id'] for m in items])

    def test_first_run_offline_and_direct_stream_route(self):
        class BlockedFeed:
            events = []
            def get(self, url): return None
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            (root/'static').mkdir()
            (root/'static/placeholder.png').write_bytes(b'p')
            shutil.copy(Path(generator.__file__).with_name('seed.json'), root/'seed.json')
            (root/'streams.json').write_text(json.dumps({'tmdb:1234:1:1': [{'url': 'https://media.example/ep.m3u8', 'title': 'Episode 1'}]}))
            env = {'GITHUB_REPOSITORY':'oceanveil-auto-catalog/oceanveil-auto-catalog.github.io', 'ENABLE_BROWSER_DISCOVERY':'0'}
            with patch.dict(os.environ, env), patch.object(generator, 'ROOT', root), patch.object(generator, 'STATE_FILE', root/'state.json'), patch.object(generator, 'OUT', root/'public'), patch.object(generator, 'GentleFetcher', BlockedFeed), patch.object(generator, 'Enricher', FakeEnricher):
                generator.build()
                pub = root/'public'
                manifest = json.loads((pub/'manifest.json').read_text())
                self.assertTrue(any(isinstance(r, dict) and r.get('name') == 'stream' for r in manifest['resources']))
                stream = json.loads((pub/'stream/series/tmdb:1234:1:1.json').read_text())
                self.assertEqual(stream['streams'][0]['url'], 'https://media.example/ep.m3u8')
                status = json.loads((pub/'status.json').read_text())
                self.assertEqual(status['known_title_count'], 1)  # one mock-matched title; no placeholders
                self.assertEqual(status['live_titles_extracted_this_run'], 0)
                self.assertEqual(status['raw_known_title_count'], 34)
                self.assertEqual(status['excluded_title_count'], 33)
                self.assertTrue((pub/'source-debug.json').exists())
                self.assertTrue(all(x['id'].startswith(('tmdb:', 'tt')) for x in json.loads((pub/'catalog/series/oceanveil-anime.json').read_text())['metas']))
                self.assertTrue((pub/'catalog/series/oceanveil-anime/skip=0.json').exists())
                self.assertEqual(status['addon_manifest_url'], 'https://oceanveil-auto-catalog.github.io/manifest.json')
                self.assertEqual(manifest['version'], '1.6.0')

    def test_catalog_id_and_artwork_gate(self):
        self.assertTrue(generator.playable_id('tmdb:123'))
        self.assertTrue(generator.playable_id('tt1234567'))
        self.assertFalse(generator.playable_id('oceanveil:seed-123'))
        self.assertFalse(generator.playable_id('kitsu:123'))
        self.assertTrue(generator.image_is_trusted('https://image.tmdb.org/t/p/w500/abc.jpg'))
        self.assertFalse(generator.image_is_trusted('https://oceanveil.net/img/broken.jpg'))
        self.assertFalse(generator.image_is_trusted('https://cdn.example.org/unknown.jpg'))
        self.assertEqual(enrichment.lookup_title('[Dub] Angel Beats!'), 'Angel Beats!')
        self.assertEqual(enrichment.lookup_title('(Sub) Angel Beats!'), 'Angel Beats!')
        self.assertFalse(generator.title_ok('Newly Streaming'))

    def test_strict_title_matching(self):
        self.assertEqual(enrichment.match_score('Angel Beats!', 'Angel Beats!'), 1)
        self.assertLess(enrichment.match_score('Mononoke', 'Princess Mononoke'), .89)

    def test_tmdb_tvdb_mdblist_enrichment_with_mock_provider_data(self):
        class APIResponse:
            def __init__(self, data):
                self.status_code = 200
                self._data = data
                self.content = b'{}'
            def raise_for_status(self): pass
            def json(self): return self._data
        class FakeSession:
            def __init__(self): self.calls=[]
            def get(self, url, params=None, headers=None, timeout=None):
                self.calls.append(url)
                if url == enrichment.FRIBB:
                    return APIResponse([{'kitsu_id': 1, 'themoviedb_id': {'tv': 1234}, 'thetvdb_id': 5678}])
                if url == enrichment.KITSU:
                    return APIResponse({'data': [{'id': '1', 'attributes': {'canonicalTitle': 'Angel Beats!', 'titles': {'en': 'Angel Beats!'}}}]})
                if url.endswith('/tv/1234'):
                    return APIResponse({'id':1234, 'overview':'Test synopsis', 'poster_path':'/poster.jpg', 'vote_average':8.4, 'genres':[{'name':'Animation'}], 'external_ids':{'imdb_id':'tt1111111', 'tvdb_id':5678}})
                if '/series/5678/extended' in url:
                    return APIResponse({'data': {'id': 5678, 'overview': 'TVDB overview'}})
                if '/imdb/show/tt1111111/' in url:
                    return APIResponse({'ratings': [{'source':'imdb','value':8.9}, {'source':'myanimelist','value':8.2}]})
                return APIResponse({})
            def post(self, url, json=None, timeout=None):
                self.calls.append(url)
                return APIResponse({'data': {'token': 'testtoken'}})
        with patch.dict(os.environ, {'TMDB_API_KEY':'test', 'TVDB_API_KEY':'test', 'MDBLIST_API_KEY':'test'}, clear=True):
            sess = FakeSession()
            enricher = enrichment.Enricher(sess)
            result = enricher.enrich('Angel Beats!')
            self.assertEqual(result['id'], 'tmdb:1234')
            self.assertEqual(result['tvdb_id'], '5678')
            self.assertEqual(result['ratings']['imdb'], 8.9)
            self.assertIn('Test synopsis', result['description'])
            self.assertEqual(result['poster'], 'https://image.tmdb.org/t/p/w500/poster.jpg')
            self.assertTrue(any('mdblist.com' in x for x in sess.calls))
            calls = len(sess.calls)
            self.assertEqual(enricher.enrich('Angel Beats!', result)['id'], 'tmdb:1234')
            self.assertEqual(len(sess.calls), calls)


if __name__ == '__main__':
    unittest.main()
