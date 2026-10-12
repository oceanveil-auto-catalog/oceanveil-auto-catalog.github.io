import json
import tempfile
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build as generator


class OfflineFeed:
    pages = {
        generator.SECONDARY: '<html><a href="/anime/angel-beats">Angel Beats!</a></html>',
        'https://manhwaindex.com/anime/angel-beats': '''<h1>Angel Beats!</h1><img alt="Cover of Angel Beats!" src="https://cdn.example.net/angel.jpg" />
            <a href="https://oceanveil.net/anime_titles/434">Watch on OceanVeil</a>''',
        'https://oceanveil.net/': '<a href="/anime_titles/500"><img alt="Added from OceanVeil" src="/poster500.jpg"></a>',
        'https://oceanveil.net/anime_titles': '',
        'https://oceanveil.net/sitemap.xml': '<urlset><url><loc>https://oceanveil.net/anime_titles/501</loc></url></urlset>',
        'https://oceanveil.net/sitemap_index.xml': '',
        'https://oceanveil.net/anime_titles/501': '<meta property="og:title" content="New from sitemap" /><meta property="og:image" content="https://img.example/poster.jpg" />',
    }

    def get(self, url):
        return self.pages.get(url)


class Tests(unittest.TestCase):
    def test_catalog_build_and_remembers_titles(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            (root / 'static').mkdir()
            (root / 'static' / 'placeholder.png').write_bytes(b'posterfixture')
            with patch.object(generator, 'ROOT', root), patch.object(generator, 'STATE_FILE', root / 'state.json'), patch.object(generator, 'OUT', root / 'public'), patch.object(generator, 'GentleFetcher', OfflineFeed):
                generator.build()
                manifest = json.loads((root / 'public' / 'manifest.json').read_text())
                data = json.loads((root / 'public' / 'catalog' / 'series' / 'oceanveil-anime.json').read_text())
                self.assertEqual(manifest['catalogs'][0]['name'], 'OceanVeil Anime')
                self.assertEqual(len(data['metas']), 3)
                self.assertTrue((root / 'public' / 'meta' / 'series' / 'oceanveil:434.json').exists())
                # Next run with nothing available still builds from saved state.
                with patch.object(generator, 'GentleFetcher', type('Empty', (), {'get': lambda self, url: None})):
                    generator.build()
                    data = json.loads((root / 'public' / 'catalog' / 'series' / 'oceanveil-anime.json').read_text())
                    self.assertEqual(len(data['metas']), 3)


    def test_initial_publish_with_all_sources_blocked(self):
        class BlockedFeed:
            events = [{"url": "https://manhwaindex.com/platform/oceanveil", "result": "error: HTTP 403"}]
            def get(self, url):
                return None

        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            (root / "static").mkdir()
            (root / "static" / "placeholder.png").write_bytes(b"placeholder")
            shutil.copy(Path(generator.__file__).with_name("seed.json"), root / "seed.json")
            env = {"GITHUB_REPOSITORY": "oceanveil-auto-catalog/oceanveil-auto-catalog", "ENABLE_BROWSER_DISCOVERY": "0"}
            with patch.dict(os.environ, env), patch.object(generator, "ROOT", root), patch.object(generator, "STATE_FILE", root / "state.json"), patch.object(generator, "OUT", root / "public"), patch.object(generator, "GentleFetcher", BlockedFeed):
                generator.build()
                status = json.loads((root / "public" / "status.json").read_text())
                listing = json.loads((root / "public" / "catalog" / "series" / "oceanveil-anime.json").read_text())["metas"]
                self.assertEqual(status["starter_snapshot_count"], 34)
                self.assertEqual(status["live_titles_extracted_this_run"], 0)
                self.assertEqual(status["known_title_count"], 34)
                self.assertEqual(len(listing), 34)
                self.assertTrue(all(item["id"].startswith("oceanveil:") for item in listing))
                self.assertTrue(all(item["poster"].startswith("https://oceanveil-auto-catalog.github.io/oceanveil-auto-catalog/") for item in listing))
                self.assertTrue((root / "public" / "manifest.json").is_file())
                self.assertTrue((root / "public" / "index.html").is_file())
                self.assertTrue((root / "public" / "catalog" / "series" / "oceanveil-anime" / "skip=0.json").is_file())
                self.assertTrue((root / "state.json").is_file())


if __name__ == '__main__':
    unittest.main()
