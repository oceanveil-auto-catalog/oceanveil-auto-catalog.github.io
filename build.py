#!/usr/bin/env python3
"""Publish an honest OceanVeil availability catalog.

Only verified canonical media IDs and working metadata artwork reach the home catalog.
An offline fallback snapshot is explicitly labeled; live discovery is audited separately.
No playback or authenticated OceanVeil URLs are fabricated.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import hashlib
import unicodedata
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from enrichment import Enricher, preview_metadata, lookup_title
from discovery import from_html, render_official, merge_record, record_key, tags_for

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
OUT = ROOT / "public"
OFFICIAL = "https://oceanveil.net"
SECONDARY = "https://manhwaindex.com/platform/oceanveil"
UA = "OceanVeilCatalogBot/1.0 (personal metadata catalog; public pages only)"
PAGE_TIMEOUT = 20
RATE_DELAY = 1.0  # Limit requests to each source
MAX_SITEMAP_LINKS = 110  # Keep first run modest
URL_PATTERN = re.compile(r"^/anime_titles/(\d+)(?:/)?$")


def pages_base_url() -> str:
    if os.environ.get("PAGES_BASE_URL"):
        return os.environ["PAGES_BASE_URL"].rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "example/oceanveil-auto-catalog")
    owner, name = repo.split("/", 1)
    # GitHub Pages user/organization repositories deploy at the domain root.
    # Project repositories deploy under /<repository>/.
    # owner may contain mixed case, but the public domain is case-insensitive.
    host = f"{owner.lower()}.github.io"
    if name.lower() == host:
        return f"https://{host}"
    return f"https://{host}/{name}"


class GentleFetcher:
    def __init__(self):
        self.events = []
        self.probes = []
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.7"})
        self.robots: dict[str, RobotFileParser | None] = {}
        self.last_request: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self.robots:
            try:
                r = self.session.get(host + "/robots.txt", timeout=PAGE_TIMEOUT)
                if r.status_code == 200:
                    robots = RobotFileParser()
                    robots.parse(r.text.splitlines())
                    self.robots[host] = robots
                else:
                    self.robots[host] = None  # no readable policy
            except requests.RequestException:
                self.robots[host] = None
        rp = self.robots[host]
        return rp is None or rp.can_fetch(UA, url)

    def capture_probe(self, html: str, url: str, mode: str) -> None:
        """Public DOM diagnostics, without cookies, request headers, or page HTML."""
        if len(self.probes) >= 9:
            return
        soup = BeautifulSoup(html, "html.parser")
        links = [urljoin(url, a.get("href", "")) for a in soup.select("a[href]")]
        candidates = [a for a in links if ocean_id(a)]
        img_alts = [i.get("alt", "")[:90] for i in soup.select("img[alt]") if i.get("alt")]
        headers = [" ".join(x.get_text(" ", strip=True).split())[:100] for x in soup.select("h1,h2,h3")]
        self.probes.append({"url": url, "mode": mode, "html_characters": len(html),
                            "title_links": len(candidates), "total_links": len(links),
                            "example_title_links": candidates[:3], "headings": headers[:12],
                            "image_alt_examples": img_alts[:15],
                            "document_title": (soup.title.get_text(" ", strip=True) if soup.title else "")[:120]})

    def get(self, url: str) -> str | None:
        if not self.allowed(url):
            print("Blocked by robots.txt:", url)
            self.events.append({"url": url, "result": "robots_denied"})
            return None
        p = urlparse(url)
        host = p.netloc
        wait = RATE_DELAY - (time.monotonic() - self.last_request.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.last_request[host] = time.monotonic()
        try:
            r = self.session.get(url, timeout=PAGE_TIMEOUT)
            r.raise_for_status()
            self.events.append({"url": url, "result": "ok"})
            if p.netloc.endswith("oceanveil.net") and len(self.probes) < 6:
                self.capture_probe(r.text, url, "http")
            if len(r.content) > 5_000_000:
                print("Skipping overly large page", url)
                return None
            return r.text
        except requests.RequestException as e:
            print("Source unavailable:", url, str(e)[:140])
            self.events.append({"url": url, "result": f"error: {str(e)[:100]}"})
            return None


def absolute_media(raw: str | None, origin: str) -> str:
    if not raw:
        return ""
    u = urljoin(origin, raw.strip())
    return u if urlparse(u).scheme in {"https", "http"} else ""


def title_ok(value: str | None) -> bool:
    if not value:
        return False
    value = " ".join(value.split())
    if not (2 < len(value) < 250):
        return False
    generic = {"welcome", "image", "all titles", "watch now", "watch", "anime", "more", "details", "read more", "newly streaming", "special pick up", "why choose oceanveil?", "start with a free account!", "faq", "oceanveil", "niche anime", "unedited titles"}
    return value.lower() not in generic and "oceanveil watch anime" not in value.lower() and "dive for anime" not in value.lower()


def clean_title(value: str | None) -> str:
    t = " ".join((value or "").split())
    t = re.sub(r"^Cover of\s+", "", t, flags=re.I)
    t = re.sub(r"\s*\|\s*OceanVeil.*$", "", t, flags=re.I)
    return t if title_ok(t) else ""


def ocean_id(url: str) -> str:
    p = urlparse(url)
    if p.netloc not in {"oceanveil.net", "www.oceanveil.net"}:
        return ""
    m = URL_PATTERN.fullmatch(p.path)
    return m.group(1) if m else ""


def details_from_official(html: str, url: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    name = ""
    for css in ['meta[property="og:title"]', 'meta[name="twitter:title"]']:
        node = soup.select_one(css)
        name = clean_title(node.get("content") if node else "")
        if name:
            break
    if not name:
        for css in ["article h1", "article h2", "main h1", "main h2", "main h3", "h1"]:
            node = soup.select_one(css)
            name = clean_title(node.get_text(" ", strip=True) if node else "")
            if name and not re.search(r"^(niche anime|unedited titles|newly streaming|special pick up|why choose oceanveil)", name, flags=re.I):
                break
            name = ""
    poster = ""
    for css in ['meta[property="og:image"]', 'meta[name="twitter:image"]']:
        node = soup.select_one(css)
        poster = absolute_media(node.get("content") if node else "", url)
        if poster:
            break
    return name, poster


def official_cards(html: str, page_url: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    for a in soup.select('a[href]'):
        href = urljoin(page_url, a.get("href", ""))
        oid = ocean_id(href)
        if not oid:
            continue
        img = a.find("img")
        title = clean_title(img.get("alt") if img else None) or clean_title(a.get_text(" ", strip=True))
        # Some sites wrap the title next to the linked poster.
        if not title:
            parent = a.parent
            if parent:
                for h in parent.select("h2,h3,h4"):
                    title = clean_title(h.get_text(" ", strip=True))
                    if title:
                        break
        if title:
            poster = ""
            if img:
                poster = absolute_media(img.get("data-src") or img.get("src"), page_url)
            result[oid] = {"title": title, "poster": poster, "url": f"{OFFICIAL}/anime_titles/{oid}"}
    return result


def scan_sitemaps(fetcher: GentleFetcher) -> list[str]:
    candidates = [OFFICIAL + "/sitemap.xml", OFFICIAL + "/sitemap_index.xml"]
    seen = set()
    urls = []
    while candidates and len(seen) < 16:
        url = candidates.pop(0)
        if url in seen:
            continue
        seen.add(url)
        xml = fetcher.get(url)
        if not xml:
            continue
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            # On some runs an HTTP-200 sitemap is actually the site's SPA shell.
            # Diagnose it rather than silently considering it a valid sitemap.
            fetcher.events.append({'url':url,'result':'not_xml_sitemap'})
            continue
        for loc in root.findall(".//{*}loc"):
            if not loc.text:
                continue
            value = loc.text.strip()
            if ocean_id(value):
                urls.append(value)
            elif value.endswith(".xml") and value.startswith(OFFICIAL) and len(seen) + len(candidates) < 16:
                candidates.append(value)
    return list(dict.fromkeys(urls))[:MAX_SITEMAP_LINKS]


def secondary_index(fetcher: GentleFetcher) -> dict[str, dict]:
    """The third-party index publicly shows only a subset (e.g. 24 of 104)."""
    html = fetcher.get(SECONDARY)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    paths = []
    for a in soup.select("a[href]"):
        p = urlparse(urljoin(SECONDARY, a.get("href", "")))
        if p.netloc == "manhwaindex.com" and re.fullmatch(r"/anime/[^/]+", p.path):
            paths.append(f"https://manhwaindex.com{p.path}")
    paths = list(dict.fromkeys(paths))[:30]
    result = {}
    for path in paths:
        body = fetcher.get(path)
        if not body:
            continue
        page = BeautifulSoup(body, "html.parser")
        h1 = page.find("h1")
        title = clean_title(h1.get_text(" ", strip=True)) if h1 else ""
        if not title:
            continue
        poster = ""
        for img in page.find_all("img"):
            if "cover" in (img.get("alt") or "").lower():
                poster = absolute_media(img.get("src") or img.get("data-src"), path)
                if poster:
                    break
        if not poster:
            og = page.select_one('meta[property="og:image"]')
            poster = absolute_media(og.get("content") if og else "", path)
        for a in page.select('a[href]'):
            link = urljoin(path, a.get("href", ""))
            oid = ocean_id(link)
            if oid:
                result[oid] = {"title": title, "poster": poster, "url": f"{OFFICIAL}/anime_titles/{oid}"}
                break
    print("Secondary index provided", len(result), "linked titles")
    return result


def rendered_official_cards(fetcher: GentleFetcher) -> dict[str, dict]:
    """Optional JS-rendered public listings (no accounts, cookies or stream requests).

    This is best-effort: a browser may still be blocked on CI. Only use it if
    the official site robots policy allows the relevant URLs.
    """
    if os.getenv("ENABLE_BROWSER_DISCOVERY") != "1":
        return {}
    if not fetcher.allowed(OFFICIAL + "/") or not fetcher.allowed(OFFICIAL + "/anime_titles"):
        print("Skipping rendered discovery: robots policy does not permit official page")
        return {}
    try:
        from playwright.sync_api import sync_playwright
        records = {}
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 1365, "height": 950})
            page = context.new_page()
            for url in [OFFICIAL + "/", OFFICIAL + "/anime_titles"]:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(2200)
                    # Catch cards that load after scrolling; bounded and polite.
                    for _ in range(4):
                        page.mouse.wheel(0, 900)
                        page.wait_for_timeout(400)
                    fetcher.capture_probe(page.content(), url, "chromium") if hasattr(fetcher, "capture_probe") else None
                    found = official_cards(page.content(), url)
                    print("Browser-rendered cards", url, len(found))
                    records.update(found)
                    fetcher.events.append({"url": url, "result": f"browser: {len(found)} titles"})
                except Exception as exc:
                    print("Browser page unavailable", url, repr(exc)[:180])
                    fetcher.events.append({"url": url, "result": f"browser_error: {str(exc)[:100]}"})
            browser.close()
        return records
    except Exception as exc:
        print("Browser fallback not available:", repr(exc)[:180])
        fetcher.events.append({"url": OFFICIAL, "result": f"browser_start_error: {str(exc)[:100]}"})
        return {}


def slug_key(title: str) -> str:
    """Stable ID for a seed title whose real OceanVeil page ID is not known."""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60]
    suffix = hashlib.sha256(title.casefold().encode("utf8")).hexdigest()[:8]
    return f"seed-{slug}-{suffix}"


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower())


def image_is_trusted(url: str) -> bool:
    """Prefer authoritative image CDNs; never use OceanVeil's unreliable artwork URLs."""
    try:
        p = urlparse(str(url))
        host = (p.hostname or "").lower()
        return p.scheme == "https" and host in {"image.tmdb.org", "media.kitsu.io"} and bool(p.path.strip("/"))
    except (ValueError, TypeError):
        return False


def playable_id(item_id: str) -> bool:
    """Canonical IDs accepted by common Stremio stream/metadata addons."""
    return bool(re.fullmatch(r"tmdb:\d+|tt\d+", item_id))


def read_seed() -> dict[str, dict]:
    path = ROOT / "seed.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf8"))
    result = {}
    for item in data["titles"]:
        title = clean_title(item["title"])
        if not title:
            continue
        source_url = SECONDARY if item.get("source") == "index" else OFFICIAL + "/"
        result[slug_key(title)] = {
            "title": title, "poster": "", "url": source_url,
            "source": "starter-snapshot", "first_seen": "2026-10-12",
            "last_seen": "2026-10-12",
            **tags_for(title),
        }
    return result


def read_old_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    data = json.loads(STATE_FILE.read_text(encoding="utf8"))
    return {str(k): v for k, v in data.get("titles", {}).items()}


def dump_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf8")


def build() -> None:
    fetcher = GentleFetcher()
    saved = read_old_state()
    seed = read_seed()
    titles = dict(seed)
    titles.update(saved)
    fresh = secondary_index(fetcher)
    candidate_methods = {'third_party': len(fresh), 'official_http': 0, 'official_browser': 0, 'official_sitemap': 0}
    # Parse public DOM/title images and structured hydration data, not only links.
    official_seen = {}
    for url in [OFFICIAL + '/', OFFICIAL + '/anime_titles']:
        html = fetcher.get(url)
        if html:
            for record in from_html(html, url).values():
                merge_record(official_seen, record)
            # Preserve useful anchors recognized by the old parser.
            for key, row in official_cards(html, url).items():
                rec = {'key': key, **row, **tags_for(row.get('title'))}
                merge_record(official_seen, rec)
    candidate_methods['official_http'] = len(official_seen)
    # Always use Chromium for independent public JSON/network observation.
    # Waiting for HTTP extraction to be empty was a V5 bug: HTTP may return
    # an incomplete homepage while richer listing data loads in browser.
    if os.getenv('ENABLE_BROWSER_DISCOVERY') == '1':
        try:
            browser_seen = render_official(fetcher)
        except Exception as e:
            browser_seen = {}
            fetcher.events.append({'url':OFFICIAL,'result':f'browser_discovery_failed: {str(e)[:120]}'})
        candidate_methods['official_browser'] = len(browser_seen)
        for record in browser_seen.values():
            merge_record(official_seen,record)
    for key, row in official_seen.items():
        previous = fresh.get(key)
        if previous:
            fresh[key] = {**previous, **row, 'dubbed': bool(previous.get('dubbed') or row.get('dubbed')),
                          'simulcast':bool(previous.get('simulcast') or row.get('simulcast')),
                          'nsfw':bool(previous.get('nsfw') or row.get('nsfw')),
                          'sfw':bool(previous.get('sfw') or row.get('sfw'))}
        else:
            fresh[key] = row
    print('Official source records found:', len(official_seen), 'http:', candidate_methods['official_http'], 'browser:', candidate_methods['official_browser'])
    for url in scan_sitemaps(fetcher):
        oid = ocean_id(url)
        if not oid or oid in fresh or oid in titles:
            continue
        html = fetcher.get(url)
        if not html:
            continue
        # Detail page og:title may be generic; avoid bogus "OceanVeil" entries.
        title, poster = details_from_official(html, url)
        embedded = from_html(html, url)
        matching = next((r for r in embedded.values() if r.get('key') == oid), None)
        if matching:
            fresh[oid] = matching
            candidate_methods['official_sitemap'] += 1
        elif title:
            fresh[oid] = {'title': title, 'poster': poster, 'url': url, **tags_for(title)}
            candidate_methods['official_sitemap'] += 1

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    # [Dub] and non-dub variants are the same underlying show; retain flags.
    canonical_key = lambda name: title_key(re.sub(r'^\s*\[Dub\]\s*', '', name or '', flags=re.I))
    by_name = {canonical_key(v['title']): k for k, v in titles.items()}
    fresh_usable = {}
    for oid, record in fresh.items():
        if not title_ok(record.get('title')):
            continue
        fresh_usable[oid] = record
        duplicate_key = by_name.get(canonical_key(record['title']))
        previous = titles.get(oid) or (titles.get(duplicate_key, {}) if duplicate_key else {})
        if duplicate_key and duplicate_key != oid and duplicate_key.startswith('seed-'):
            del titles[duplicate_key]
        attrs = {}
        for flag in ('dubbed','simulcast','nsfw','sfw'):
            attrs[flag] = bool(record.get(flag) or previous.get(flag))
        if attrs['nsfw']:
            attrs['sfw'] = False
        titles[oid] = {
            'title': record['title'],
            'poster': record.get('poster') or previous.get('poster', ''),
            'url': record.get('url') or previous.get('url') or OFFICIAL + '/anime_titles',
            'source': 'live-discovery',
            'discovery_origin': record.get('origin') or 'public-sitemap',
            'age_rating': record.get('age_rating') or previous.get('age_rating') or '',
            'first_seen': previous.get('first_seen', today),
            'last_seen': today,
            **attrs,
            **({'enrichment':previous['enrichment']} if previous.get('enrichment') else {}),
        }
        by_name[canonical_key(record['title'])] = oid
    if not titles:
        raise RuntimeError("No seed or saved records, and no live titles. Refusing to publish empty data.")
    # Enrich without requiring API keys. Never replace canonical matches with
    # an unverified fuzzy result, and preserve old enrichment on outages.
    enrichment = Enricher()
    prior_count = 0
    max_new = int(os.getenv("ENRICH_MAX_NEW", "45"))
    attempts = 0
    for key, record in titles.items():
        previous = dict(record.get("enrichment") or {})
        if previous.get("id") and previous.get("refresh_after", "") >= today:
            prior_count += 1
            continue
        if attempts >= max_new:
            continue
        attempts += 1
        updated = enrichment.enrich(record['title'], previous)
        if updated:
            record['enrichment'] = updated
    dump_json(STATE_FILE, {"titles": titles})

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copy(ROOT / "static" / "placeholder.png", OUT / "placeholder.png")
    (OUT / ".nojekyll").touch()
    base_url = pages_base_url()
    # Optional user-supplied direct streams. Keys are canonical video IDs, e.g.
    # tmdb:123:1:1; values are lists of {url, title} or {externalUrl, title}.
    # These are not discovered automatically and may require authorized access.
    stream_source = ROOT / "streams.json"
    try:
        streams_config = json.loads(stream_source.read_text(encoding='utf8')) if stream_source.exists() else {}
    except (OSError, ValueError) as e:
        print("Invalid streams.json:", e)
        streams_config = {}
    safe_streams = {}
    for video_id, entries in streams_config.items() if isinstance(streams_config, dict) else []:
        if not isinstance(video_id, str) or not isinstance(entries, list):
            continue
        valid = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            u = e.get('url') or e.get('externalUrl')
            if not isinstance(u, str) or not u.startswith('https://'):
                continue
            item = {'title': str(e.get('title') or 'Watch')[:120]}
            if e.get('url'):
                item['url'] = u
            else:
                item['externalUrl'] = u
            valid.append(item)
        if valid:
            safe_streams[video_id] = valid
    resources = ['catalog']  # Other installed addons provide metadata and video streams for canonical IDs.
    if safe_streams:
        # Only advertised if direct/official sources have been configured.
        # Ordinary matched TMDB/IMDb entries are still playable via other addons.
        prefixes = sorted({vid.removeprefix('movie:').split(':', 2)[0] + ':' + vid.removeprefix('movie:').split(':', 2)[1]
                           if vid.removeprefix('movie:').startswith('tmdb:') and len(vid.removeprefix('movie:').split(':')) > 1
                           else vid.removeprefix('movie:').split(':')[0]
                           for vid in safe_streams})
        resources.append({"name": "stream", "types": ["series", "movie"], "idPrefixes": prefixes})

    CATALOGS = [
        ('oceanveil-simulcast', 'OceanVeil • Simulcast'),
        ('oceanveil-dubbed', 'OceanVeil • Dubbed'),
        ('oceanveil-nsfw', 'OceanVeil • NSFW'),
        ('oceanveil-sfw', 'OceanVeil • SFW'),
        ('oceanveil-mix', 'OceanVeil • Mix (SFW + NSFW)'),
        ('oceanveil-top', 'OceanVeil • Top'),
        ('oceanveil-mix-all', 'OceanVeil • Mix of All'),
        ('oceanveil-nsfw-dubbed', 'OceanVeil • NSFW Dubbed'),
    ]
    manifest = {
        'id': 'community.oceanveil.auto.catalog',
        'version': '1.6.0',
        'name': 'OceanVeil Auto Catalog',
        'description': 'Eight OceanVeil catalogs using public source labels, verified media identifiers and TMDB/MDBList artwork. Compatible installed stream addons can resolve matched titles.',
        'resources': resources,
        'types': ['series','movie'],
        'catalogs': [{'type':'series','id':catid,'name':name,'extra':[{'name':'skip','isRequired':False}]} for catid,name in CATALOGS] +
                    [{'type':'movie','id':'oceanveil-movies','name':'OceanVeil • Movies','extra':[{'name':'skip','isRequired':False}]}],
        'behaviorHints': {'configurable':False},
    }
    dump_json(OUT / 'manifest.json', manifest)
    grouped = {'series': [], 'movie': []}
    aggregated = {}
    match_counts = {'tmdb':0, 'imdb':0, 'kitsu':0, 'oceanveil':0}
    enrich_counts = {'tmdb_metadata':0,'tvdb_metadata':0,'mdb_ratings':0}
    exclusions = []
    snapshot_published = 0
    official_published = 0
    for oid, data in sorted(titles.items(), key=lambda kv: (kv[1].get('last_seen',''), kv[1].get('first_seen',''), kv[1]['title']), reverse=True):
        e = data.get('enrichment') or {}
        item_id = e.get('id') or ''
        art = e.get('poster') or e.get('kitsu_poster') or ''
        art = art if image_is_trusted(art) else ''
        reasons = []
        if not playable_id(item_id): reasons.append('unmatched_tmdb_imdb_id')
        if not art: reasons.append('missing_trusted_poster')
        if reasons:
            exclusions.append({'title':data['title'],'source':data.get('source','unknown'),'reason':', '.join(reasons)})
            continue
        if item_id in aggregated:
            prior = aggregated[item_id]
            for flag in ('dubbed','simulcast','nsfw','sfw'):
                prior['flags'][flag] = bool(prior['flags'][flag] or data.get(flag))
            prior['flags']['sfw'] = bool(prior['flags']['sfw'] and not prior['flags']['nsfw'])
            if data.get('source') == 'live-discovery':
                prior['verified_live'] = True
            continue
        typ = 'movie' if e.get('tmdb_type') == 'movie' and item_id.startswith('tmdb:') else 'series'
        match_counts['tmdb' if item_id.startswith('tmdb:') else 'imdb'] += 1
        enrich_counts['tmdb_metadata'] += int(bool(e.get('description') and e.get('tmdb_id')))
        enrich_counts['tvdb_metadata'] += int(bool(e.get('tvdb_id')))
        enrich_counts['mdb_ratings'] += int(bool(e.get('ratings')))
        preview = {'id':item_id, 'type':typ, **preview_metadata(lookup_title(data['title']),e,art)}
        preview['poster'] = art
        rating_raw = ((e.get('ratings') or {}).get('myanimelist') or (e.get('ratings') or {}).get('imdb') or e.get('tmdb_rating') or 0)
        try: score = float(rating_raw)
        except (ValueError, TypeError): score = 0
        aggregated[item_id] = {'meta':preview,'type':typ,'rating':score,
            'flags':{flag:bool(data.get(flag)) for flag in ('dubbed','simulcast','nsfw','sfw')},
            'verified_live':data.get('source')=='live-discovery'}
    records = list(aggregated.values())
    for rec in records:
        if rec['verified_live']: official_published += 1
        else: snapshot_published += 1
        grouped[rec['type']].append(rec['meta'])
    series = [r for r in records if r['type']=='series']
    def date_order(r):
        return r['meta'].get('releaseInfo',''), r['meta']['name'].casefold()
    # Prefer most recently discovered/produced, but never invent age/dub/simulcast labels.
    ordinary = sorted(series,key=date_order,reverse=True)
    sfw = [r for r in ordinary if r['flags']['sfw'] and not r['flags']['nsfw']]
    nsfw = [r for r in ordinary if r['flags']['nsfw']]
    mixed = []
    for idx in range(max(len(sfw),len(nsfw))):
        if idx < len(sfw): mixed.append(sfw[idx])
        if idx < len(nsfw): mixed.append(nsfw[idx])
    selected = {
        'oceanveil-simulcast': [r for r in ordinary if r['flags']['simulcast']],
        'oceanveil-dubbed': [r for r in ordinary if r['flags']['dubbed']],
        'oceanveil-nsfw':nsfw,
        'oceanveil-sfw':sfw,
        'oceanveil-mix':mixed,
        'oceanveil-top':sorted(series,key=lambda r:(r['rating'],r['meta']['name'].casefold()), reverse=True),
        'oceanveil-mix-all':ordinary,
        'oceanveil-nsfw-dubbed':[r for r in nsfw if r['flags']['dubbed']],
    }
    catalog_counts = {}
    for catid,_name in CATALOGS:
        metas = [r['meta'] for r in selected[catid]]
        catalog_counts[catid] = len(metas)
        for skip in range(0,len(metas)+101,100):
            payload = {'metas':metas[skip:skip+100]}
            if skip == 0:
                dump_json(OUT/'catalog'/'series'/f'{catid}.json', payload)
            dump_json(OUT/'catalog'/'series'/catid/f'skip={skip}.json',payload)
    # Legacy endpoint allows users to test their previously installed catalog link.
    for typ, catid in [('series','oceanveil-anime'),('movie','oceanveil-movies')]:
        metas = grouped[typ]
        for skip in range(0,len(metas)+101,100):
            payload = {'metas':metas[skip:skip+100]}
            if skip == 0: dump_json(OUT/'catalog'/typ/f'{catid}.json',payload)
            dump_json(OUT/'catalog'/typ/catid/f'skip={skip}.json',payload)
    for video_id, streams in safe_streams.items():
        kind = 'movie' if video_id.startswith('movie:') else 'series'
        normalized_id = video_id.removeprefix('movie:')
        dump_json(OUT/'stream'/kind/f'{normalized_id}.json',{'streams':streams})
    listing = grouped['series'] + grouped['movie']
    if not listing:
        print("::warning::The published catalog is EMPTY after identity/artwork validation. Check TMDB_API_KEY, enrichment and status.json exclusions.")
    live_count = sum(r.get("source") == "live-discovery" for r in titles.values())
    newly_found = sum(oid not in saved for oid in fresh_usable)
    status = {
        "known_title_count": len(listing),
        "raw_known_title_count": len(titles),
        "ready_for_nuvio": bool(listing),
        "excluded_title_count": len(exclusions),
        "excluded_titles": exclusions[:200],
        "published_historical_snapshot_count": snapshot_published,
        "published_live_source_count": official_published,
        "content_validation": "Only TMDB/IMDb identifiers + trusted TMDB/Kitsu posters. Snapshot is historical, not live-verified.",
        "source_debug": "source-debug.json",
        "starter_snapshot_count": len(seed),
        "stored_live_title_count": live_count,
        "live_titles_extracted_this_run": len(fresh_usable),
        "new_live_title_ids_this_run": newly_found,
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_checks": getattr(fetcher, "events", [])[:150],
        "discovery_methods": candidate_methods,
        "discovered_title_examples": [v['title'] for v in list(fresh_usable.values())[:20]],
        "catalog_counts": catalog_counts,
        "unclassified_series_count": sum(not (r['flags']['sfw'] or r['flags']['nsfw']) for r in series),
        "addon_manifest_url": f"{base_url}/manifest.json",
        "catalog_url": f"{base_url}/catalog/series/oceanveil-mix-all.json",
        "build_version": manifest["version"],
        "identifier_counts": match_counts,
        "enrichment_counts": enrich_counts,
        "stream_endpoints": len(safe_streams),
        "metadata_apis_configured": {"tmdb": bool(enrichment.tmdb_key), "tvdb": bool(enrichment.tvdb_key), "mdblist": bool(enrichment.mdb_key)},
        "id_match_sources": enrichment.events,
        "unmatched_titles": [x["title"] for x in exclusions if "unmatched" in x["reason"]][:50],
        "notes": ("Live sources provided titles this run." if fresh_usable else
                  "No live OceanVeil titles extracted. Listed candidates, if any, come from prior source snapshots and are not verified current availability."),
    }
    dump_json(OUT / "status.json", status)
    dump_json(OUT / "source-debug.json", {"public_dom_probes": getattr(fetcher, "probes", []),
                                          "source_checks": getattr(fetcher, "events", [])[:30],
                                          "browser_public_json_endpoints": getattr(fetcher,'browser_endpoint_hits',{}),
                                          "candidate_counts": candidate_methods,
                                          "note": "Only public page diagnostics; no cookies/auth headers or video traffic."})
    # The homepage includes direct test links, so a user can isolate publishing
    # faults before troubleshooting the Nuvio application or profile settings.
    page = ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width,initial-scale=1'><title>OceanVeil Auto Catalog</title>"
            "<style>body{font:16px/1.6 system-ui;max-width:740px;margin:8vh auto;padding:20px;"
            "background:#101823;color:white}a{color:#9dc9ff}code{overflow-wrap:anywhere}"
            "li{padding:4px 0}</style>"
            f"<h1>OceanVeil Auto Catalog</h1><p>{len(listing)} metadata-matched titles in eight anime lists, plus movies.</p>"
            "<ul><li><a href='manifest.json'>Addon manifest</a></li>"
            "<li><a href='catalog/series/oceanveil-mix-all.json'>Mix of All JSON</a></li>"
            "<li><a href='catalog-preview.html'>Visual catalog preview</a></li>"
            "<li><a href='status.json'>Update status</a></li><li><a href='source-debug.json'>Source diagnostics</a></li></ul>"
            f"<p>Install URL: <code>{base_url}/manifest.json</code></p>"
            "<p>If this site's preview works but the row is missing in Nuvio, reinstall "
            "the addon into the right Nuvio profile and enable its home catalog.</p>"
            "<p>Uses only matched TMDB/IMDb IDs for compatibility with your installed "
            "metadata and stream addons. OceanVeil subscriber playback is not included. "
            "Live discovery may remain unavailable.</p></html>")
    (OUT / "index.html").write_text(page, encoding="utf8")
    preview = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OceanVeil – Catalog Preview</title><style>
*{box-sizing:border-box}body{font-family:system-ui,sans-serif;background:#0b1523;color:#eef6ff;margin:0;padding:28px}
header{max-width:1000px;margin:auto}h1{margin:0 0 8px}a{color:#88cdff}p{color:#aec0d1}
main{max-width:1000px;margin:20px auto}select{background:#24354b;border:1px solid #47607a;border-radius:9px;padding:13px;color:#fff;min-width:250px;max-width:100%;font:inherit;margin:10px 0 24px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:16px}
article{background:#16263b;border-radius:12px;padding:10px}img{width:100%;aspect-ratio:2/3;object-fit:cover;background:#24354b;border-radius:7px}
b{display:block;margin-top:8px;font-size:14px;line-height:1.3}
#status{padding:12px;background:#172c46;border-radius:10px;margin:12px 0 24px}
</style></head><body><header><h1>OceanVeil — Catalog Preview</h1>
<p>Preview the actual published catalog JSON. A successful preview doesn't prove Nuvio installation or playback.</p>
<p><a href="manifest.json">Addon manifest</a> · <a href="status.json">Discovery status</a> · <a href="source-debug.json">Source diagnostics</a></p></header>
<main><label for="cat">Choose a catalog</label><div><select id="cat">
<option value="oceanveil-simulcast">Simulcast</option><option value="oceanveil-dubbed">Dubbed</option>
<option value="oceanveil-nsfw">NSFW</option><option value="oceanveil-sfw">SFW</option>
<option value="oceanveil-mix">Mix (SFW + NSFW)</option><option value="oceanveil-top">Top</option>
<option value="oceanveil-mix-all" selected>Mix of All</option><option value="oceanveil-nsfw-dubbed">NSFW Dubbed</option>
<option value="oceanveil-movies">Movies (separate Stremio media type)</option></select></div>
<div id="status" role="status">Loading…</div><div id="cards" class="grid"></div></main>
<script>
const choice=document.getElementById('cat');
async function render(){
 const cards=document.getElementById('cards'),status=document.getElementById('status');
 cards.textContent='';status.textContent='Loading…';
 const cat=choice.value,type=cat==='oceanveil-movies'?'movie':'series';
 try{
  const response=await fetch('./catalog/'+type+'/'+cat+'.json',{cache:'no-store'});
  if(!response.ok)throw Error('HTTP '+response.status);
  const body=await response.json();
  if(!Array.isArray(body.metas))throw Error('missing metas array');
  status.textContent=body.metas.length+' titles loaded. Empty sections mean matching or source classification has not found eligible entries yet.';
  for(const entry of body.metas){
   const card=document.createElement('article'),img=document.createElement('img'),name=document.createElement('b');
   img.loading='lazy';img.src=entry.poster||'';img.alt='Poster for '+(entry.name||'');
   name.textContent=entry.name||'(untitled)';card.append(img,name);cards.append(card);
  }
 }catch(e){status.textContent='Catalog failed: '+e.message}
}
choice.addEventListener('change',render);render();
</script></body></html>"""
    (OUT / "catalog-preview.html").write_text(preview, encoding="utf8")
    if not fresh_usable:
        print("::warning::No live titles extracted. Publication uses historical candidates only; this does not confirm current OceanVeil availability.")
    print(f"Generated {len(listing)} titles; seeded {len(seed)}, saved live {live_count}, fetched this run {len(fresh_usable)}. Base: {base_url}")


if __name__ == "__main__":
    build()
