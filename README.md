# OceanVeil catalog for Stremio / Nuvio / Xperience

Install URL: `https://oceanveil-auto-catalog.github.io/manifest.json`

Twice a day, GitHub Actions rebuilds a static addon that mirrors OceanVeil's homepage:

- **Homepage rows, in the site's order and with the site's names.** The row layout is read from
  the config oceanveil.net embeds in its own HTML, so curated rows ("This Fall's Must-See Titles",
  "Going Viral!", …) change automatically when the site changes them. Fixed rows (SimulCast,
  Newest Addition, Ranking, English Dubbed, Completed, genre sections) keep stable catalog IDs;
  curated rows use positional slots (`oceanveil-home-promo-1`, `-2`, …).
- **Filter rows:** All Titles, SFW (PG/13+), 16+, NSFW/18+, Mix (SFW + NSFW), NSFW Dubbed.
  Classification uses the site's own age rating and its `NSFW` / `Dubbed` tags.

Titles are matched to IMDb (preferred) or TMDB IDs so your installed metadata and stream addons
handle details and playback. Unmatched titles are served with their own `oceanveil_<id>` detail
page (poster, synopsis, link to OceanVeil), so they still appear.

Data comes only from OceanVeil's public, logged-out API (`/api/v1/anime_titles`,
`newest_addition_anime_titles`, `trending_anime_titles`). No sign-in or video URLs are used.
Titles that are only visible to signed-in or subscribed users can't be listed.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Needed | Purpose |
|---|---|---|
| `TMDB_API_KEY` | strongly recommended | ID matching, posters, synopses |
| `TVDB_API_KEY`, `MDBLIST_API_KEY` | optional | extra ID matching and ratings |

Matches are cached in `state.json`, so each title costs API calls once a week at most.

## Files

- `oceanveil.py`: public API client and homepage config parser
- `build.py`: matching and publishing (`public/` is generated, not committed)
- `enrichment.py`: Kitsu / Fribb / TMDB / TVDB / MDBList ID matching
- `public/status.json` (live site): what the last run found, unmatched titles, row counts

Run locally: `pip install -r requirements.txt && python -m unittest discover -s tests && python build.py`
