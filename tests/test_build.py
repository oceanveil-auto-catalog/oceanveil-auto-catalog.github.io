import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build as mod


HOME1 = '''<html><body>
<h1>Niche Anime</h1><h2>SimulCast Lineup | New Every Week</h2>
<div class="anime"><a href="/anime_titles/101"><img alt="[Dub] Seaside Story" src="https://cdn.test/101.jpg"></a><span>SimCast New 16+</span></div>
<div class="anime"><a href="/anime_titles/102"><img alt="Sea Hearts" src="https://cdn.test/102.jpg"></a><span>SimCast New PG</span></div>
<h2>Newest Addition</h2><div><a href="/anime_titles/103"><img alt="New Princess"></a>18+ Mature</div>
<h2>Ranking</h2><a href="/anime_titles/102"><img alt="Sea Hearts"></a><a href="/anime_titles/103"><img alt="New Princess"></a>
<h2>English Dubbed Titles</h2><a href="/anime_titles/101"><img alt="[Dub] Seaside Story"></a>
<h2>Going Viral</h2><a href="/anime_titles/103"><img alt="New Princess"></a>
<h2>BL Section</h2><a href="/anime_titles/101"><img alt="[Dub] Seaside Story"></a>
<h2>News</h2><a href="/anime_titles/104"><img alt="A News Link Not A Row"></a>
</body></html>'''
HOME2 = '''<h2>SimulCast Lineup | New Every Week</h2>
<a href="/anime_titles/102"><img alt="Sea Hearts" /></a>
<h2>Ranking</h2><a href="/anime_titles/101"><img alt="[Dub] Seaside Story" /></a>
<h2>Special Pick Up</h2><a href="/anime_titles/105"><img alt="Special Spotlight" /></a>'''


class Feed:
    pages = {
        f'{mod.OFFICIAL}/': HOME1,
        f'{mod.OFFICIAL}/anime_titles': '''<a href="/anime_titles/104"><img alt="Browse Anime" /></a>
          <a href="/anime_titles?page=2">Next</a>''',
        f'{mod.OFFICIAL}/anime_titles?page=2': '<a href="/anime_titles/105"><img alt="Special Spotlight" /></a>',
        f'{mod.OFFICIAL}/sitemap.xml': '''<sitemapindex><sitemap><loc>https://oceanveil.net/sitemap-anime.xml</loc></sitemap></sitemapindex>''',
        f'{mod.OFFICIAL}/sitemap-anime.xml': '''<urlset><url><loc>https://oceanveil.net/anime_titles/106</loc></url></urlset>''',
        f'{mod.OFFICIAL}/sitemap_index.xml': '',
        f'{mod.OFFICIAL}/anime_titles/106': '''<meta property="og:title" content="Official Sitemap Title | OceanVeil" />''',
        mod.SECONDARY: '',
    }

    def get(self, url):
        return self.pages.get(url)


class Tests(unittest.TestCase):
    def test_section_parser_preserves_homepage_order(self):
        titles, secs = mod.parse_homepage(HOME1, mod.OFFICIAL)
        self.assertEqual(list(secs), ['simulcast-lineup', 'newest-additions', 'ranking',
                                      'english-dubbed-titles', 'going-viral', 'bl-section'])
        self.assertEqual(secs['ranking']['ids'], ['102', '103'])
        self.assertEqual(secs['simulcast-lineup']['ids'], ['101', '102'])
        self.assertTrue(titles['101']['dubbed'])
        self.assertEqual(titles['102']['rating'], 'PG')
        self.assertEqual(titles['103']['rating'], '18+')
        self.assertNotIn('104', secs['bl-section']['ids'])

    def test_16_is_unclassified(self):
        self.assertEqual(mod.adult_level({'rating': '16+'}), 'unknown')
        self.assertEqual(mod.adult_level({'rating': '18+'}), 'adult')
        self.assertEqual(mod.adult_level({'rating': 'PG'}), 'sfw')
        self.assertEqual(mod.adult_level({'rating': '', 'genre': 'BL'}), 'unknown')
        self.assertEqual(mod.interleave(['a', 'b'], ['c', 'd']), ['a', 'c', 'b', 'd'])

    def test_build_daily_changes_keep_old_titles_not_old_featured_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'static').mkdir()
            (root / 'static' / 'placeholder.png').write_bytes(b'fake')
            with patch.object(mod, 'ROOT', root), patch.object(mod, 'STATE_FILE', root/'state.json'), \
                 patch.object(mod, 'OUT', root/'public'), patch.object(mod, 'GentleFetcher', Feed):
                mod.build()
                status = json.loads((root/'public/status.json').read_text())
                self.assertEqual(status['known_title_count'], 6)
                self.assertEqual(status['homepage_row_counts']['Ranking'], 2)
                self.assertEqual(json.loads((root/'public/catalog/series/home-ranking.json').read_text())['metas'][0]['id'], 'oceanveil:102')
                manifest = json.loads((root/'public/manifest.json').read_text())
                self.assertIn('home-special-pick-up', [x['id'] for x in manifest['catalogs']])
                self.assertIn('mix-all', [x['id'] for x in manifest['catalogs']])
                self.assertEqual(len(json.loads((root/'public/catalog/series/oceanveil-anime.json').read_text())['metas']), 6)
                # Day 2: homepage promotions shuffle and one row disappears.
                newpages = dict(Feed.pages)
                newpages[f'{mod.OFFICIAL}/'] = HOME2
                with patch.object(mod, 'GentleFetcher', type('Changed', (), {'get': lambda self, url: newpages.get(url)})):
                    mod.build()
                status2 = json.loads((root/'public/status.json').read_text())
                self.assertTrue(status2['homepage_rows_fresh'])
                self.assertNotIn('Newest Additions', status2['homepage_row_counts'])
                ranked = json.loads((root/'public/catalog/series/home-ranking.json').read_text())['metas']
                self.assertEqual([x['id'] for x in ranked], ['oceanveil:101'])
                self.assertEqual(len(json.loads((root/'public/catalog/series/oceanveil-anime.json').read_text())['metas']), 6)
                # If source becomes inaccessible, old latest-known homepage remains but reports stale.
                with patch.object(mod, 'GentleFetcher', type('Down', (), {'get': lambda self, url: None})):
                    mod.build()
                status3 = json.loads((root/'public/status.json').read_text())
                self.assertFalse(status3['homepage_rows_fresh'])
                self.assertEqual(status3['homepage_row_counts']['Ranking'], 1)

    def test_promotional_rows_reuse_ids_across_new_headings_and_new_titles(self):
        # Rename a promo without creating or reordering any Nuvio catalog IDs.
        day1 = '''<h2>Ranking</h2><a href="/anime_titles/1"><img alt="First Ranked" /></a>
        <h2>Halloween Specials</h2><a href="/anime_titles/2"><img alt="Pumpkin Show" /></a>
        <h2>BL Section</h2><a href="/anime_titles/3"><img alt="BL Show" /></a>
        <h2>Weekend Spotlight</h2><a href="/anime_titles/4"><img alt="Weekend Show" /></a>'''
        day2 = '''<h2>BL Section</h2><a href="/anime_titles/3"><img alt="BL Show" /></a>
        <h2>Romance Spotlight</h2><a href="/anime_titles/5"><img alt="New Romance" /></a>
        <h2>Ranking</h2><a href="/anime_titles/6"><img alt="New Number One" /></a>
        <h2>New Week Picks</h2><a href="/anime_titles/7"><img alt="New Pick" /></a>'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'static').mkdir()
            (root/'static'/'placeholder.png').write_bytes(b'x')
            titles = {str(i): {'title': f'Show {i}', 'url': f'{mod.OFFICIAL}/anime_titles/{i}', 'first_seen': '2026-01-01'} for i in range(1, 8)}
            stats = {'new': 0, 'home_ok': True, 'home_fresh': True, 'discovered_urls': 0, 'details_attempted': 0}
            with patch.object(mod, 'OUT', root/'public'), patch.object(mod, 'ROOT', root):
                _, seen = mod.parse_homepage(day1, mod.OFFICIAL)
                home1 = mod.reconcile_homepage({}, seen)
                mod.publish(titles, home1, stats)
                manifest1 = json.loads((root/'public/manifest.json').read_text())
                before = [(x['id'], x['name']) for x in manifest1['catalogs']]
                ids_before = [x[0] for x in before]
                self.assertIn(('home-promo-slot-01', 'Halloween Specials'), before)
                self.assertIn(('home-promo-slot-02', 'Weekend Spotlight'), before)
                self.assertEqual([x['id'] for x in json.loads((root/'public/catalog/series/home-promo-slot-01.json').read_text())['metas']], ['oceanveil:2'])
                _, seen2 = mod.parse_homepage(day2, mod.OFFICIAL)
                home2 = mod.reconcile_homepage(home1, seen2)
                mod.publish(titles, home2, stats)
                manifest2 = json.loads((root/'public/manifest.json').read_text())
                after = [(x['id'], x['name']) for x in manifest2['catalogs']]
                self.assertEqual([x[0] for x in after], ids_before)
                self.assertIn(('home-promo-slot-01', 'Romance Spotlight'), after)
                self.assertIn(('home-promo-slot-02', 'New Week Picks'), after)
                self.assertNotIn('home-romance-spotlight', [x[0] for x in after])
                self.assertEqual([x['id'] for x in json.loads((root/'public/catalog/series/home-promo-slot-01.json').read_text())['metas']], ['oceanveil:5'])
                self.assertEqual([x['id'] for x in json.loads((root/'public/catalog/series/home-promo-slot-02.json').read_text())['metas']], ['oceanveil:7'])
                self.assertEqual([x['id'] for x in json.loads((root/'public/catalog/series/home-ranking.json').read_text())['metas']], ['oceanveil:6'])
                # Old featured show remains discoverable in All Titles, not promo.
                self.assertIn('oceanveil:2', [x['id'] for x in json.loads((root/'public/catalog/series/oceanveil-anime.json').read_text())['metas']])
                status = json.loads((root/'public/status.json').read_text())
                self.assertEqual(status['sticky_home_rows'][1]['catalog_id'], 'home-promo-slot-01')
                self.assertEqual(status['sticky_home_rows'][1]['display_name'], 'Romance Spotlight')

    def test_full_daily_build_persists_slots_to_state_file(self):
        first_html = '<h2>Tonight\'s Spotlight</h2><a href="/anime_titles/891"><img alt="First Show" /></a>'
        next_html = '<h2>Hot This Week</h2><a href="/anime_titles/892"><img alt="Second Show" /></a>'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'static').mkdir()
            (root/'static'/'placeholder.png').write_bytes(b'x')
            with patch.object(mod, 'ROOT', root), patch.object(mod, 'STATE_FILE', root/'state.json'), patch.object(mod, 'OUT', root/'public'):
                for html in (first_html, next_html):
                    pages = {f'{mod.OFFICIAL}/': html, f'{mod.OFFICIAL}/anime_titles': '',
                             f'{mod.OFFICIAL}/sitemap.xml': '', f'{mod.OFFICIAL}/sitemap_index.xml': '', mod.SECONDARY: ''}
                    class Fake:
                        def get(self, url):
                            return pages.get(url)
                    with patch.object(mod, 'GentleFetcher', Fake):
                        mod.build()
                    saved = json.loads((root/'state.json').read_text())
                    self.assertEqual(saved['homepage']['promo_slots'], ['promo-slot-01'])
                    self.assertEqual(saved['homepage']['row_order'], ['promo-slot-01'])
                    manifest = json.loads((root/'public/manifest.json').read_text())
                    self.assertEqual(manifest['catalogs'][0]['id'], 'home-promo-slot-01')
                    self.assertEqual(manifest['catalogs'][0]['name'], 'Tonight\'s Spotlight' if html == first_html else 'Hot This Week')
                items = json.loads((root/'public/catalog/series/home-promo-slot-01.json').read_text())['metas']
                self.assertEqual([m['id'] for m in items], ['oceanveil:892'])
                all_items = json.loads((root/'public/catalog/series/oceanveil-anime.json').read_text())['metas']
                self.assertEqual({m['id'] for m in all_items}, {'oceanveil:891', 'oceanveil:892'})

    def test_missing_promotional_row_is_kept_but_emptied_and_reused(self):
        _, day1 = mod.parse_homepage('''<h2>Ranking</h2><a href="/anime_titles/1"><img alt="Ranked" /></a>
            <h2>Featured Collection</h2><a href="/anime_titles/2"><img alt="Featured" /></a>''', mod.OFFICIAL)
        first = mod.reconcile_homepage({}, day1)
        _, day2 = mod.parse_homepage('<h2>Ranking</h2><a href="/anime_titles/3"><img alt="Updated" /></a>', mod.OFFICIAL)
        second = mod.reconcile_homepage(first, day2)
        self.assertEqual(second['sections']['promo-slot-01']['ids'], [])
        self.assertEqual(second['sections']['promo-slot-01']['name'], 'Featured Collection')
        self.assertEqual(second['row_order'], first['row_order'])
        _, day3 = mod.parse_homepage('<h2>Brand New Feature</h2><a href="/anime_titles/4"><img alt="Something New" /></a>', mod.OFFICIAL)
        third = mod.reconcile_homepage(second, day3)
        self.assertEqual(third['sections']['promo-slot-01'], {'name': 'Brand New Feature', 'ids': ['4']})
        self.assertEqual(third['row_order'], first['row_order'])

    def test_v2_heading_slug_catalog_id_is_preserved_when_upgrading(self):
        legacy = {'sections': {'ranking': {'name': 'Ranking', 'ids': ['1']},
                               'summer-specials': {'name': 'Summer Specials', 'ids': ['2']}}}
        _, new = mod.parse_homepage('''<h2>Ranking</h2><a href="/anime_titles/3"><img alt="Ranked" /></a>
            <h2>October Surprise</h2><a href="/anime_titles/4"><img alt="Halloween" /></a>''', mod.OFFICIAL)
        updated = mod.reconcile_homepage(legacy, new)
        self.assertEqual(updated['promo_slots'], ['summer-specials'])
        self.assertEqual(updated['row_order'], ['ranking', 'summer-specials'])
        self.assertEqual(updated['sections']['summer-specials'], {'name': 'October Surprise', 'ids': ['4']})
        self.assertNotIn('october-surprise', updated['sections'])

    def test_pagination_over_100_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'static').mkdir()
            (root/'static'/'placeholder.png').write_bytes(b'x')
            titles = {str(n): {'title': f'Show {n}', 'url': f'{mod.OFFICIAL}/anime_titles/{n}',
                               'first_seen': '2026-01-01', 'rating': 'PG'} for n in range(1, 251)}
            with patch.object(mod, 'OUT', root/'public'), patch.object(mod, 'ROOT', root):
                mod.publish(titles, {'sections': {}}, {'new': 250, 'home_ok': False,
                    'home_fresh': False, 'discovered_urls': 250, 'details_attempted': 250})
                for offset, expected in ((0, 100), (100, 100), (200, 50), (300, 0)):
                    path = root/'public/catalog/series/oceanveil-anime'/f'skip={offset}.json'
                    self.assertEqual(len(json.loads(path.read_text())['metas']), expected)
                self.assertEqual(len(json.loads((root/'public/catalog/series/mix-all.json').read_text())['metas']), 100)


if __name__ == '__main__':
    unittest.main()
