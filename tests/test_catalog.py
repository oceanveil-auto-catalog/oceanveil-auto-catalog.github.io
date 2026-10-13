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
import enrichment
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
        self.content = b'{}'

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
            return Resp(url, data={'data': CATALOG[1:2] + CATALOG[0:1]})
        if path.endswith('/anime_titles'):
            keys = dict(q)
            if 'ids[]' in keys:
                wanted = {str(v) for k, v in q if k == 'ids[]'}
                return Resp(url, data={'data': [t for t in CATALOG + [HIDDEN] if t['id'] in wanted]})
            if keys.get('tag_ids[]') == str(oceanveil.TAG_NSFW) or keys.get('tag_ids[]') == oceanveil.TAG_NSFW:
                return Resp(url, data={'data': CATALOG[2:3]})
            if str(keys.get('tag_ids[]')) == str(oceanveil.TAG_DUBBED):
                return Resp(url, data={'data': CATALOG[0:1]})
            if 'genre_ids[]' in keys:
                return Resp(url, data={'data': CATALOG[1:2]})
            if keys.get('is_simulcast') == 'true':
                return Resp(url, data={'data': CATALOG[0:1]})
            return Resp(url, data={'data': CATALOG})
        return Resp(url, status=404)


class FakeEnricher:
    tmdb_key, tvdb_key, mdb_key = 'k', '', ''
    calls = []

    def enrich(self, name, previous=None, alt_names=()):
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
    def run_build(self, site, state=None, enricher=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'static').mkdir()
        if state is not None:
            (root / 'state.json').write_text(json.dumps(state))
        env = {'GITHUB_REPOSITORY': 'oceanveil-auto-catalog/oceanveil-auto-catalog.github.io'}
        with patch.object(build, 'ROOT', root), patch.object(build, 'OUT', root / 'public'), \
                patch.object(build, 'STATE_FILE', root / 'state.json'), patch.dict(os.environ, env, clear=True), \
                patch.object(build, 'Enricher', enricher or FakeEnricher), \
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
        names = lambda g: [m['name'] for m in self.load(root, f'catalog/series/oceanveil-browse/genre={g}.json')['metas']]
        self.assertEqual(names('NSFW'), ['Night Bloom'])
        self.assertEqual(names('SFW'), ['Quiet Garden'])
        self.assertEqual(names('16-Plus'), ['Ocean Tale'])
        self.assertEqual(names('Mix-SFW-NSFW'), ['Quiet Garden', 'Night Bloom'])

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
        nsfw = self.load(root, 'catalog/series/oceanveil-browse/genre=NSFW.json')['metas']
        self.assertEqual(nsfw[0]['id'], 'tt0000300')
        self.assertNotIn('Night Bloom', FakeEnricher.calls)
        saved = json.loads((root / 'state.json').read_text())
        self.assertEqual(saved['version'], 2)
        self.assertEqual(saved['titles']['1']['enrichment']['imdb_id'], 'tt0000300')


class VersionSiblingsAndPlayback(unittest.TestCase):
    """Dub/sub copies collapse to one entry; unmatched titles still get something to play."""

    def run_with(self, catalog, enrich):
        global CATALOG
        old = CATALOG
        CATALOG = catalog
        self.addCleanup(lambda: globals().__setitem__('CATALOG', old))

        class E(FakeEnricher):
            def enrich(self, name, previous=None, alt_names=()):
                return enrich(name)
        return BuildTests.run_build(self, FakeSite(), enricher=E)

    load = BuildTests.load

    def test_lookup_title_strips_version_tags(self):
        self.assertEqual(enrichment.lookup_title('[Dub]The Lonely Snow Widow'), 'The Lonely Snow Widow')
        self.assertEqual(enrichment.lookup_title('Angel Beats!(Sub)'), 'Angel Beats!')
        self.assertEqual(enrichment.lookup_title('[Dub] Black Jack (The Complete OVA Series Dubbed)'), 'Black Jack')
        self.assertEqual(enrichment.series_key('[Dub] Overflow'), enrichment.series_key('Overflow'))
        self.assertNotEqual(enrichment.series_key('Show Time! 2'), enrichment.series_key('Show Time!'))

    def test_unmatched_dub_and_sub_become_one_entry_with_open_link_streams(self):
        cat = [title(7, '[Dub] Overflow'), title(4, 'Overflow')]
        root, status = self.run_with(cat, lambda n: {})
        metas = self.load(root, 'catalog/series/oceanveil-home-newest.json')['metas']
        self.assertEqual([m['id'] for m in metas], ['oceanveil_4'])
        meta = self.load(root, 'meta/series/oceanveil_4.json')['meta']
        self.assertEqual(len(meta['links']), 2)
        self.assertEqual(meta['videos'][0]['id'], 'oceanveil_4:1:1')
        streams = self.load(root, 'stream/series/oceanveil_4:1:1.json')['streams']
        self.assertEqual({s['externalUrl'] for s in streams},
                         {'https://oceanveil.net/anime_titles/4', 'https://oceanveil.net/anime_titles/7'})
        self.assertIn('stream', [r['name'] for r in self.load(root, 'manifest.json')['resources'] if isinstance(r, dict)])

    def test_match_on_one_version_is_shared_with_the_other(self):
        cat = [title(7, '[Dub] Overflow'), title(4, 'Overflow')]
        root, _ = self.run_with(cat, lambda n: {'id': 'tt1', 'imdb_id': 'tt0001234', 'refresh_after': '2099-01-01'}
                                if n.startswith('[Dub]') else {})
        metas = self.load(root, 'catalog/series/oceanveil-home-newest.json')['metas']
        self.assertEqual([m['id'] for m in metas], ['tt0001234'])

    def test_kitsu_only_titles_get_kitsu_episode_ids(self):
        cat = [title(5, 'Hidden Gem')]
        root, status = self.run_with(cat, lambda n: {'id': 'kitsu:42', 'kitsu_id': '42', 'episodes': 3,
                                                      'refresh_after': '2099-01-01'})
        meta = self.load(root, 'meta/series/oceanveil_5.json')['meta']
        self.assertEqual([v['id'] for v in meta['videos']], ['kitsu:42:1', 'kitsu:42:2', 'kitsu:42:3'])
        self.assertTrue((root / 'public' / 'stream' / 'series' / 'kitsu:42:2.json').exists())
        self.assertEqual(status['id_types']['own_meta_with_kitsu_episodes'], 1)

    def test_browse_catalog_and_extra_page_offsets(self):
        root, status = BuildTests.run_build(self, FakeSite())
        cats = {c['id']: c for c in self.load(root, 'manifest.json')['catalogs']}
        self.assertNotIn('oceanveil-sfw', cats)
        self.assertIn('SFW', cats['oceanveil-browse']['extra'][0]['options'])
        self.assertEqual(len(self.load(root, 'catalog/series/oceanveil-browse.json')['metas']), 3)
        self.assertEqual(self.load(root, 'catalog/series/oceanveil-browse/genre=SFW&skip=20.json')['metas'], [])
        self.assertEqual(self.load(root, 'catalog/series/oceanveil-home-newest/skip=25.json')['metas'], [])


class AniListMatching(unittest.TestCase):
    """AniList finds titles Kitsu hides; Fribb turns the AniList ID into Kitsu/IMDb IDs."""

    class Http:
        def __init__(self):
            self.posts = []

        def get(self, url, params=None, headers=None, timeout=None):
            if 'anime-lists' in url:
                return Resp(url, data=[{'anilist_id': 900, 'mal_id': 901, 'kitsu_id': 902}])
            return Resp(url, data={'data': []})  # Kitsu: nothing (adult titles are hidden)

        def post(self, url, json=None, timeout=None):
            self.posts.append(json['variables']['s'])
            media = [{'id': 900, 'idMal': 901, 'episodes': 2, 'title': {'romaji': 'Obaafuro', 'english': 'Overflow'},
                      'synonyms': [], 'coverImage': {'large': 'https://s4.anilist.co/x.jpg'}, 'description': '<i>Hi</i>'},
                     {'id': 5, 'title': {'romaji': 'Something Else'}, 'synonyms': []}]
            return Resp(url, data={'data': {'Page': {'media': media}}})

    def test_anilist_match_gives_kitsu_id_and_episodes(self):
        r = Resp('x', data=None)
        r.content = b'x'
        http = self.Http()
        with patch.dict(os.environ, {}, clear=True), patch.object(enrichment.time, 'sleep', lambda s: None):
            e = enrichment.Enricher(http).enrich('[Dub] Overflow', {}, ['Obafuro'])
        self.assertEqual(e['kitsu_id'], '902')
        self.assertEqual(e['episodes'], 2)
        self.assertEqual(e['id'], 'kitsu:902')
        self.assertEqual(e['anilist_description'], 'Hi')
        self.assertEqual(http.posts, ['Overflow'])  # exact hit, romaji search skipped


class OfflineDatabaseMatching(unittest.TestCase):
    DB = {'data': [
        {'title': 'Yuru Yuri', 'type': 'TV', 'episodes': 12, 'animeSeason': {'year': 2011}, 'synonyms': [],
         'sources': ['https://kitsu.app/anime/6205', 'https://anilist.co/anime/10495']},
        {'title': 'Yuru Yuri♪♪', 'type': 'TV', 'episodes': 12, 'animeSeason': {'year': 2012},
         'synonyms': ['YuruYuri 2'], 'sources': ['https://kitsu.app/anime/6724']},
        {'title': 'Ranpo Kitan: Game of Laplace', 'type': 'TV', 'episodes': 11, 'synonyms': ['ranpokitan', '乱歩奇譚 Game of Laplace'],
         'sources': ['https://kitsu.app/anime/10039']},
        {'title': 'Overflow', 'type': 'ONA', 'episodes': 8, 'synonyms': [], 'sources': ['https://myanimelist.net/anime/40746']},
    ]}

    def enricher(self):
        db = self.DB

        class S:
            def get(self, *a, **k):
                return Resp('x', data=db)
        return enrichment.Enricher(S())

    def test_seasons_romaji_and_mal_only(self):
        e = self.enricher()
        self.assertEqual(e.offline_match('Yuru Yuri Season 1', 'Yuru yuri ikki')['kitsu_id'], '6205')
        self.assertEqual(e.offline_match('Yuru Yuri Season 2', 'Yuru yuri niki')['kitsu_id'], '6724')
        self.assertEqual(e.offline_match('Rampo Kitan Club', 'Ranpo Kitan')['kitsu_id'], '10039')
        over = e.offline_match('[Dub] Overflow')
        self.assertEqual(over['mal_id'], '40746')
        self.assertNotIn('kitsu_id', over)  # no ID Torrentio understands exists for this one
        self.assertIsNone(e.offline_match('Totally Unknown Show'))

    def test_search_variants(self):
        self.assertIn('Yuru Yuri 2', enrichment.search_variants('Yuru Yuri Season 2'))
        self.assertIn('Negima!? OVA', enrichment.search_variants('Negima!?(OVA)'))


if __name__ == '__main__':
    unittest.main()
