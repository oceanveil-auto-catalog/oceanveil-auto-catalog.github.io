import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build
import oceanveil

# Shaped like the real page: bare keys, !0 booleans, emoji, colons and URLs in strings.
HOME_HTML = '''<html><script>window.__NUXT__={};window.__NUXT__.config={public:{env:"production",
domain:"oceanveil.net",generalTopSettings:{carousel:[{carousel_items:[{set:{id:485}}],start_at:"2026-09-25 00:00"}],
must_see_picks_list:{ids:[1,2]},sections:[
{type:"anime-title-slider",name:"must_see_picks_oa",header:"Must-Watch",search_type:"id",ids:[1,2],is_subscription_onboarding_only:!0},
{type:"continue-watching-episode-slider",name:"cw",header:"Continue Watching",search_type:"watched_episodes",limit:10},
{type:"banner-large",name:"b",carousel_items:[{set:{link:"https://oceanveil.net/anime_titles/542"}}]},
{type:"anime-title-slider",name:"simulsub_oa",header:"SimulCast Lineup : New Every Week \\ud83d\\udcc5",search_type:"simulcast",limit:10},
{type:"anime-title-slider",name:"newest_addition_oa",header:"Newest Addition",search_type:"newest_addition",limit:10},
{type:"anime-title-slider",name:"pick_up_Fall_oa",header:"This Fall’s Must-See",search_type:"id",ids:[3,1],start_at:"2026-08-16 10:00",end_at:"9999-12-31 23:59"},
{type:"anime-title-slider",name:"future_oa",header:"Not Yet",search_type:"id",ids:[2],start_at:"2099-01-01 00:00"},
{type:"anime-title-slider",name:"trending_oa",header:"Ranking",search_type:"trending",limit:10,link:"/ranking"},
{type:"anime-title-slider",name:"dubbed_oa",header:"English Dubbed Titles",search_type:"tag",ids:[62],limit:15},
{type:"anime-title-slider",name:"completed_oa",header:"Completed",search_type:"completed",limit:10},
{type:"anime-title-slider",name:"bl_section_oa",header:"BL Section",search_type:"genre",ids:[4],link:"/search?genre_id=4"}]},
matureTopSettings:{sections:[]},recaptcha:{siteKey:"x"},flag:void 0}};</script></html>'''


def title(i, name, rating='age_16_plus', simul=False, completed=True, romaji=''):
    return {'id': str(i), 'type': 'animeTitle', 'attributes': {
        'id': i, 'name': name, 'romajiName': romaji, 'summary': f'About {name}', 'rating': rating,
        'isSimulcast': simul, 'isCompleted': completed, 'startAt': '2024-01-01T00:00:00.000-07:00', 'isMature': False}}


CATALOG = [title(3, '[Dub] Ocean Tale', simul=True, completed=False), title(2, 'Quiet Garden', 'pg'),
           title(1, 'Night Bloom', 'age_18_plus')]
HIDDEN = title(9, 'Promo Only Show')  # reachable only through a curated row


class Resp:
    def __init__(self, url, status=200, text='', data=None):
        self.url, self.status_code, self.text, self._data = url, status, text, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class FakeSite:
    def __init__(self, home=HOME_HTML, api_down=False):
        self.headers, self.home, self.api_down = {}, home, api_down

    def get(self, url, params=None, timeout=None):
        q = parse_qsl(urlparse(url).query) + (list(params.items()) if isinstance(params, dict) else list(params or []))
        path = urlparse(url).path
        if path == '/':
            return Resp(url, text=self.home)
        if self.api_down:
            return Resp(url, status=503)
        if path.endswith('/newest_addition_anime_titles'):
            return Resp(url, data={'data': CATALOG})
        if path.endswith('/trending_anime_titles'):
            return Resp(url, data={'data': [CATALOG[1], CATALOG[0]]})
        if path.endswith('/anime_titles'):
            keys = dict(q)
            if 'ids[]' in keys:
                wanted = {str(v) for k, v in q if k == 'ids[]'}
                return Resp(url, data={'data': [t for t in CATALOG + [HIDDEN] if t['id'] in wanted]})
            if keys.get('tag_ids[]') == str(oceanveil.TAG_NSFW) or keys.get('tag_ids[]') == oceanveil.TAG_NSFW:
                return Resp(url, data={'data': [CATALOG[2]]})
            if str(keys.get('tag_ids[]')) == str(oceanveil.TAG_DUBBED):
                return Resp(url, data={'data': [CATALOG[0]]})
            if 'genre_ids[]' in keys:
                return Resp(url, data={'data': [CATALOG[1]]})
            if keys.get('is_simulcast') == 'true':
                return Resp(url, data={'data': [CATALOG[0]]})
            return Resp(url, data={'data': CATALOG})
        return Resp(url, status=404)


class FakeEnricher:
    tmdb_key, tvdb_key, mdb_key = 'k', '', ''
    calls = []

    def enrich(self, name, previous=None):
        self.calls.append(name)
        if name == '[Dub] Ocean Tale':
            return {'id': 'tmdb:100', 'tmdb_id': '100', 'tmdb_type': 'tv', 'imdb_id': 'tt0000100',
                    'poster': 'https://image.tmdb.org/t/p/w500/a.jpg', 'refresh_after': '2099-01-01'}
        if name == 'Quiet Garden':
            return {'id': 'tmdb:200', 'tmdb_id': '200', 'tmdb_type': 'movie', 'refresh_after': '2099-01-01'}
        return {'id': 'kitsu:5', 'refresh_after': '2099-01-01'}


class ConfigParsing(unittest.TestCase):
    def test_parses_embedded_home_sections(self):
        sections = oceanveil.parse_home_sections(HOME_HTML)
        self.assertEqual(len(sections), 11)
        self.assertTrue(sections[0]['is_subscription_onboarding_only'])
        self.assertEqual(sections[3]['header'], 'SimulCast Lineup : New Every Week \U0001f4c5')
        self.assertEqual(sections[2]['carousel_items'][0]['set']['link'], 'https://oceanveil.net/anime_titles/542')

    def test_active_rows_skip_banners_user_rows_and_scheduled(self):
        rows = oceanveil.active_title_rows(oceanveil.parse_home_sections(HOME_HTML),
                                           now=datetime(2026, 10, 13, tzinfo=oceanveil.SITE_TZ))
        self.assertEqual([r['name'] for r in rows], ['simulsub_oa', 'newest_addition_oa', 'pick_up_Fall_oa',
                                                     'trending_oa', 'dubbed_oa', 'completed_oa', 'bl_section_oa'])

    def test_stable_row_keys(self):
        self.assertEqual(oceanveil.row_key({'search_type': 'tag', 'ids': [62]}), 'tag-62')
        self.assertEqual(oceanveil.row_key({'search_type': 'trending'}), 'ranking')
        self.assertIsNone(oceanveil.row_key({'search_type': 'id', 'ids': [1]}))

    def test_poster_url(self):
        self.assertTrue(oceanveil.poster_url(536).endswith('/000/000000/000000536/000000536_vertical_with_logo.jpg_small.webp'))


class BuildTests(unittest.TestCase):
    def run_build(self, site, state=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'static').mkdir()
        if state is not None:
            (root / 'state.json').write_text(json.dumps(state))
        env = {'GITHUB_REPOSITORY': 'oceanveil-auto-catalog/oceanveil-auto-catalog.github.io'}
        with patch.object(build, 'ROOT', root), patch.object(build, 'OUT', root / 'public'), \
                patch.object(build, 'STATE_FILE', root / 'state.json'), patch.dict(os.environ, env, clear=True), \
                patch.object(build, 'Enricher', FakeEnricher), \
                patch.object(build, 'OceanVeil', lambda: oceanveil.OceanVeil(session=site, delay=0)):
            status = build.build()
        return root, status

    def load(self, root, rel):
        return json.loads((root / 'public' / rel).read_text())

    def test_home_rows_in_site_order_with_imdb_ids(self):
        root, status = self.run_build(FakeSite())
        manifest = self.load(root, 'manifest.json')
        ids = [c['id'] for c in manifest['catalogs']]
        self.assertEqual(ids[:7], ['oceanveil-home-simulcast', 'oceanveil-home-newest', 'oceanveil-home-promo-1',
                                   'oceanveil-home-ranking', 'oceanveil-home-tag-62', 'oceanveil-home-completed',
                                   'oceanveil-home-genre-4'])
        self.assertEqual(manifest['catalogs'][0]['name'], 'OceanVeil • SimulCast Lineup : New Every Week \U0001f4c5')
        self.assertTrue(status['homepage_config_live'])
        simul = self.load(root, 'catalog/series/oceanveil-home-simulcast.json')['metas']
        self.assertEqual(simul[0]['id'], 'tt0000100')  # IMDb preferred over tmdb:
        ranking = [m['id'] for m in self.load(root, 'catalog/series/oceanveil-home-ranking.json')['metas']]
        self.assertEqual(ranking, ['tmdb:200', 'tt0000100'])  # site order kept
        movie = self.load(root, 'catalog/series/oceanveil-home-ranking.json')['metas'][0]
        self.assertEqual(movie['type'], 'movie')

    def test_curated_row_keeps_order_and_includes_row_only_titles(self):
        root, _ = self.run_build(FakeSite())
        promo = self.load(root, 'catalog/series/oceanveil-home-promo-1.json')['metas']
        self.assertEqual([m['name'] for m in promo][:2], ['Ocean Tale', 'Night Bloom'])

    def test_unmatched_titles_get_own_meta(self):
        root, status = self.run_build(FakeSite())
        meta = self.load(root, 'meta/series/oceanveil_1.json')['meta']
        self.assertEqual(meta['name'], 'Night Bloom')
        self.assertIn('oceanveil.net/anime_titles/1', meta['website'])
        self.assertIn('image.oceanveil.net', meta['poster'])
        manifest = self.load(root, 'manifest.json')
        self.assertIn({'name': 'meta', 'types': ['series'], 'idPrefixes': ['oceanveil_']}, manifest['resources'])

    def test_sfw_nsfw_classification_from_site_tags_and_ratings(self):
        root, status = self.run_build(FakeSite())
        names = lambda c: [m['name'] for m in self.load(root, f'catalog/series/{c}.json')['metas']]
        self.assertEqual(names('oceanveil-nsfw'), ['Night Bloom'])
        self.assertEqual(names('oceanveil-sfw'), ['Quiet Garden'])
        self.assertEqual(names('oceanveil-16plus'), ['Ocean Tale'])
        self.assertEqual(names('oceanveil-mix'), ['Quiet Garden', 'Night Bloom'])

    def test_falls_back_to_default_rows_when_config_missing(self):
        root, status = self.run_build(FakeSite(home='<html>no config</html>'))
        self.assertFalse(status['homepage_config_live'])
        self.assertIn('oceanveil-home-tag-62', status['catalog_counts'])

    def test_api_outage_fails_instead_of_publishing_stale_data(self):
        with self.assertRaises(RuntimeError):
            self.run_build(FakeSite(api_down=True))

    def test_cached_matches_skip_api_and_legacy_state_migrates(self):
        legacy = {'titles': {'x': {'title': '[Dub] Night Bloom', 'enrichment': {
            'id': 'tmdb:300', 'tmdb_id': '300', 'imdb_id': 'tt0000300', 'refresh_after': '2099-01-01'}}}}
        FakeEnricher.calls = []
        root, _ = self.run_build(FakeSite(), state=legacy)
        nsfw = self.load(root, 'catalog/series/oceanveil-nsfw.json')['metas']
        self.assertEqual(nsfw[0]['id'], 'tt0000300')
        self.assertNotIn('Night Bloom', FakeEnricher.calls)
        saved = json.loads((root / 'state.json').read_text())
        self.assertEqual(saved['version'], 2)
        self.assertEqual(saved['titles']['1']['enrichment']['imdb_id'], 'tt0000300')


if __name__ == '__main__':
    unittest.main()
