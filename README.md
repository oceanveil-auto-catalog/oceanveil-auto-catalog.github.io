# OceanVeil Auto Catalog v4 — Nuvio/Stremio IMDb-Compatible Metadata

**This addon produces catalogs and metadata, not copyrighted video streams.** When an OceanVeil title is confidently matched to its IMDb identity using TMDB's public metadata API, Nuvio can request playback from **separately installed, compatible sources** using the standard IMDb and episode IDs. It does not guarantee that a stream exists for every matched title and does not log in to, extract, or bypass OceanVeil video access. Only use authorized playback services.

Version 4 focuses on the failure you reported: one catalog with 100 bad entries (including `episode.1`, missing posters, no episode metadata) that existing stream addons could not recognize. **Don't upload older versions again.** The live OceanVeil site and your live GitHub repository were inaccessible from the development environment. All verification here is **offline using fixtures**; actual results must be checked on the deployed repository.

## What is improved

| Requirement | What v4 does | Limitation |
|---|---|---|
| Playability through other installed addons | Uses verified `tt1234567` IMDb IDs instead of private `oceanveil:NNN` IDs where a trusted match is found | Only works when a compatible, separately installed source actually offers the title |
| Series episodes | Uses TMDB series data to create `tt1234567:season:episode` episode IDs, episode titles, release dates, and thumbnails when available | No invented episode data; season and episode counts come from TMDB, not verified against every OceanVeil edit |
| Posters, backgrounds, descriptions | Prefers verified TMDB metadata and poster CDN URLs | TMDB may not list niche anime; unmatched show entries will still lack rich metadata |
| Sticky Nuvio rows | Keeps manifest addon ID `community.oceanveil.auto.catalog`, old `home-...` section IDs, positional promo slots | Nuvio may cache manifest names; refresh may be required. **Item** IDs can change from `oceanveil:` to `tt`, affecting item-specific history |
| Daily dynamic homepage | Publishes separate independent homepage rows; changes content without changing section ID | Only when public HTML is accessible and parseable by the GitHub Actions runner |
| 100+ items | `skip=100`, `skip=200` pages for each catalog | Nuvio must support pagination; discovery is not guaranteed complete |
| Malformed imported titles | Invalid titles such as `episode.1` and `episode.2` are quarantined, not published | Other imperfect records can survive until their source extraction is improved |
| Correct GitHub Pages asset location | For `OWNER/OWNER.github.io`, URLs use root `https://OWNER.github.io` | Keep the normal `owner.github.io/repo` scheme for *project* repositories |
| Movies versus series | Publishes movie matches in corresponding `-movies` catalog rows; original row IDs remain series IDs | More than one catalog appears; existing Nuvio arrangement of series rows remains separate |

**Crucial distinction:** A verified external IMDb ID means the Nuvio client can send normal `stream/series/tt...:season:episode` or `stream/movie/tt...` requests to your installed stream addons. This catalog itself deliberately does **not** declare a `stream` resource; it's impossible to honestly guarantee video sources without knowing which authorized playback provider you use.

## Upgrade your exact repository

Repository: **https://github.com/oceanveil-auto-catalog/oceanveil-auto-catalog.github.io**

Expected GitHub Pages root: **https://oceanveil-auto-catalog.github.io/**

Expected addon installation URL: **https://oceanveil-auto-catalog.github.io/manifest.json**

Expected diagnostics URL: **https://oceanveil-auto-catalog.github.io/status.json**

1. Download and unzip v4. Open the *inner* `oceanveil-auto-catalog/` directory.
2. In GitHub, upload its contents **into the root of the same repository**, preserving `.github/workflows/update.yml`. Replace `build.py`, `metadata_enrichment.py`, `tests/`, `README.md`, `.github/workflows/update.yml`. Leave `state.json` intact. Do NOT rename the repository or its addon ID.
3. Get an **API v3 key (not a bearer token)** from [The Movie Database](https://www.themoviedb.org/settings/api) (free noncommercial access is subject to their terms).
4. Go to the GitHub repository → **Settings → Secrets and variables → Actions → New repository secret**. Name it exactly `TMDB_API_KEY`; put the **TMDB API v3 key** in its secret value. **Never commit this key to `state.json`, `build.py`, or any public file.**
5. GitHub repository → **Settings → Pages → Build and deployment → Source: GitHub Actions**.
6. Repository → **Actions → Update and publish OceanVeil catalog → Run workflow**. The workflow runs offline tests, fetches public catalog pages, matches up to **80 titles per day** with TMDB, caches verified IMDb mappings in `state.json`, then deploys JSON files to GitHub Pages. Existing title mappings are kept between daily updates. If many records exist, mapping may take multiple days; repeated manual workflow runs can work through the backlog too.
7. Open **https://oceanveil-auto-catalog.github.io/status.json** and inspect `imdb_matched_count`, `unmatched_count`, `known_title_count`, `metadata.tmdb_api_calls`, `metadata.tmdb_api_failures`, and `homepage_rows_fresh`. If `imdb_matched_count` remains 0 after running with a valid secret, look at logs and the source title quality. If `homepage_rows_fresh` is `false`, promotional rows are stale and the homepage scraper needs work against accessible real HTML.
8. Open **https://oceanveil-auto-catalog.github.io/manifest.json**. Confirm `version` is `4.0.0` and that the `catalogs` array contains many IDs such as `home-simulcast-lineup`, `home-ranking`, `home-promo-slot-01`, and `oceanveil-linked`.
9. In Nuvio, **refresh the existing installed OceanVeil addon** so it downloads the new manifest. Do not uninstall unless refreshing is unavailable and the new catalogs never appear, since uninstalling can reset custom row placement. Ensure another **authorized stream addon**, with support for standard IMDb IDs, is installed separately. Our catalog cannot provide a stream by itself.
10. Choose **OceanVeil — IMDb-matched (compatible sources)** in Nuvio. Open one verified match. Confirm poster, description, episodes (for a series), and whether your separate source addon returns a legitimate, working playback option. Repeat on at least one movie and one series if present.

**Do not use an OceanVeil promotional catalog row as proof that playback is supported.** Promotional rows intentionally display both IMDb-matched and unmatched OceanVeil titles; some entries may only provide an official website link.

## What happens with unmatched titles?

The addon refuses to assign random or merely similar IMDb IDs; doing that risks playing the **wrong anime** or showing incorrect episode lists. Unmatched titles keep their stable `oceanveil:NNN` metadata IDs with their OceanVeil listing link and any legitimate poster detected on the public site. They remain in the homepage mirror and All Titles, but most generic stream addons cannot play them. `OceanVeil — IMDb-matched (compatible sources)` lists only successfully linked items.

A small percentage of promotional titles may represent alternate cuts, dubbed variants or independent short releases not listed in TMDB/IMDb at all. They cannot honestly be made automatically playable by an IMDb mapper. Improving these requires confirmed provider mappings or official accessible playback integrations; **no invented stream sources**.

## Verify exact playback metadata routes

For a matched series `tt12345678`, test:

```
https://oceanveil-auto-catalog.github.io/meta/series/tt12345678.json
```

It should have `meta.id: "tt12345678"`, poster/description, and `meta.videos` like `{ "id": "tt12345678:1:1", "season": 1, "episode": 1 }` if episodes exist in TMDB. Other compatible addons will receive `tt12345678:1:1` when the client searches for a stream; this catalog **does not** contain video URLs.

For an unmatched title `oceanveil:123`, `/meta/series/oceanveil:123.json` is available but its `videos` list is empty unless metadata can be verified by another source. That is intentional rather than presenting fake episode data.

The matching logic requires exact normalized title matches (ignoring clearly marked `[Dub]`/`[Sub]` prefix/suffix) and rejects ambiguous media IDs and year conflicts. `state.json` stores the verified IMDb match plus cached TMDB metadata. Existing mappings are refreshed roughly monthly; failed lookups are retried after 45 days to limit API use.

## Troubleshooting and honest limitations

- `version` still 1/2/3 → GitHub Pages is serving the old deployment. Check Actions deployment and Pages settings; Nuvio manifest caching won't change the live `manifest.json` contents.
- One catalog in Nuvio even though manifest has many → Nuvio likely has a stale installed manifest; refresh that addon. If live manifest has only one catalog, deployment did not update.
- `imdb_matched_count=0` → TMDB secret missing/invalid; or most titles do not exist in IMDb; or source titles are garbage. Check `metadata.tmdb_api_calls` and `metadata.tmdb_api_failures`.
- Posters remain blank → unmatched title uses an inaccessible official poster or generic placeholder. Verify `poster` URL; matched entries prefer TMDB CDN images.
- Homepage promotional section doesn't change → `homepage_rows_fresh` is false; the bot couldn't parse the real homepage and is preserving its last good snapshot.
- Wrong title match or wrong season → report it; conservative matching still cannot disambiguate all localized cuts. Metadata matching is independent of availability on OceanVeil.
- IMDb match opens but no stream → separate source addon did not supply an available authorized stream; adding arbitrary stream URLs or changing catalog IDs won't solve that.
- Nuvio watch history may be reset for titles that switch from custom IDs to IMDb IDs. **Stable catalog IDs preserve row identity, not necessarily item watch progress.** The addon has no way to migrate Nuvio internal history.
- This runs on **static GitHub Pages**; it cannot do OAuth, dynamic paid-site login, or resolve a paid video player. The scheduled build happens in GitHub Actions, not inside Nuvio.

## Local offline testing

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
TMDB_API_KEY=your_actual_tmdb_api_v3_key python build.py
python -m http.server --directory public 8000
```

**Never post logs containing API keys.** Automated tests use fake TMDB responses and site HTML fixtures. They do not establish the real catalog's completeness or real playback success. No live test was performed against your GitHub Pages host or OceanVeil.

Data sources: OceanVeil public pages and TMDB API, subject to their respective access rules. Metadata provided using TMDB; this addon is not endorsed or certified by TMDB. For usage rights, credits, and image terms consult the TMDB API terms. Stremio protocol: https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/protocol.md .
