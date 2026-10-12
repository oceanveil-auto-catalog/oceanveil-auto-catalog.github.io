import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discovery import from_html, from_json, tags_for, merge_record, record_key
import build

class DiscoveryTests(unittest.TestCase):
    def test_linkless_official_images_and_title_tags(self):
        html = '''<main><div class="tile"><img src="/images/one.jpg" alt="[Dub] Sample Pilot"/>
          <span>Young Adult</span><span>SimCast</span><span>13+</span></div>
          <div class="tile"><img src="/images/two.jpg" alt="Horror Example"/>
          <span>18+</span></div></main>'''
        data = from_html(html, 'https://oceanveil.net/anime_titles')
        self.assertEqual(len(data), 2)
        values = list(data.values())
        dub = next(x for x in values if x['dubbed'])
        adult = next(x for x in values if x['nsfw'])
        self.assertTrue(dub['simulcast'])
        self.assertTrue(dub['sfw'])
        self.assertFalse(dub['nsfw'])
        self.assertFalse(adult['sfw'])

    def test_public_json_title_array_discovery(self):
        data = {'data': {'anime_titles': [
            {'anime_title_id': 413, 'title': 'Pilot Show', 'image': '/413.jpg', 'age_rating':'PG', 'is_dub': True},
            {'anime_title_id': 414, 'title': 'Uncut Show', 'poster_url': '/414.jpg', 'classification':'18+', 'simulcast': True},
        ]}}
        got = from_json(data, 'https://oceanveil.net/api/list')
        self.assertEqual(len(got), 2)
        self.assertEqual(got['413']['url'], 'https://oceanveil.net/anime_titles/413') # numeric official title ID
        self.assertTrue(got['413']['dubbed'])
        self.assertTrue(got['413']['sfw'])
        self.assertTrue(got['414']['nsfw'])
        self.assertTrue(got['414']['simulcast'])

    def test_html_hydration_discovery(self):
        raw={'props':{'pageProps':{'titles':[{'id':8,'title':'Hydrated Test Anime','image':'/a.jpg','rating':'13+'}]}}}
        html='<script id="__NEXT_DATA__" type="application/json">'+json.dumps(raw)+'</script>'
        got=from_html(html,'https://oceanveil.net/anime_titles')
        self.assertTrue(got['8']['sfw'])

    def test_same_title_dub_variants_merge(self):
        a = from_json({'data':[{'anime_id':1,'title':'Some Show','poster':'/p.jpg','rating':'PG'}]},'https://oceanveil.net/api/a')
        b = from_json({'data':[{'anime_id':1,'title':'[Dub] Some Show','poster':'/p.jpg','rating':'PG'}]},'https://oceanveil.net/api/a')
        for row in b.values(): merge_record(a,row)
        self.assertEqual(len(a),1)
        self.assertTrue(a['1']['dubbed'])
        self.assertTrue(a['1']['sfw'])

    def test_no_sitewide_adult_keyword_leak(self):
        a=tags_for('[Dub] Testing','SimCast 16+')
        self.assertFalse(a['nsfw'])
        self.assertFalse(a['sfw']) # 16+ is ambiguous, not a verified SFW tag
        self.assertTrue(a['dubbed'])
        self.assertTrue(a['simulcast'])

    def test_bot_state_build_eight_catalogs_and_classifications(self):
        class FakeFeed:
            events=[]
            pages = {'https://manhwaindex.com/platform/oceanveil':'',
                     'https://oceanveil.net/':'''<main><div><a href="/anime_titles/9"><img alt="[Dub] Test Pilot" src="/x.jpg"></a><span>SimCast 18+</span></div>
                         <div><a href="/anime_titles/10"><img alt="Kind Story" src="/x.jpg"></a><span>PG</span></div></main>''',
                     'https://oceanveil.net/anime_titles':'', 'https://oceanveil.net/sitemap.xml':'', 'https://oceanveil.net/sitemap_index.xml':''}
            def get(self,url): return self.pages.get(url)
        class FakeMetadata:
            tmdb_key='test';tvdb_key='';mdb_key='';events=[]
            def enrich(self, title, previous=None):
                name=title.replace('[Dub] ','')
                if name not in ('Test Pilot', 'Kind Story'): return {}
                ident='11' if name=='Test Pilot' else '12'
                return {'id':'tmdb:'+ident,'tmdb_id':ident,'tmdb_type':'tv','poster':'https://image.tmdb.org/t/p/w500/poster'+ident+'.jpg','tmdb_rating':8.9 if ident=='11' else 7.1,'refresh_after':'2099-01-01'}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'static').mkdir();(root/'static'/'placeholder.png').write_bytes(b'x')
            with patch.object(build,'ROOT',root), patch.object(build,'OUT',root/'public'), patch.object(build,'STATE_FILE',root/'state.json'), patch.object(build,'GentleFetcher',FakeFeed), patch.object(build,'Enricher',FakeMetadata), patch.dict('os.environ',{'ENABLE_BROWSER_DISCOVERY':'0'}):
                build.build()
                status=json.loads((root/'public/status.json').read_text())
                self.assertEqual(status['discovery_methods']['official_http'],2)
                self.assertEqual(status['live_titles_extracted_this_run'],2)
                counts=status['catalog_counts']
                self.assertEqual(counts['oceanveil-simulcast'],1)
                self.assertEqual(counts['oceanveil-dubbed'],1)
                self.assertEqual(counts['oceanveil-nsfw'],1)
                self.assertEqual(counts['oceanveil-sfw'],1)
                self.assertEqual(counts['oceanveil-mix'],2)
                self.assertEqual(counts['oceanveil-mix-all'],2)
                self.assertEqual(counts['oceanveil-nsfw-dubbed'],1)
                self.assertEqual(counts['oceanveil-top'],2)

if __name__=='__main__': unittest.main()
