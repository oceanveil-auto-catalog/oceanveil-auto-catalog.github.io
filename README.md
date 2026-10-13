# OceanVeil catalog for Stremio / Nuvio / Xperience

Install URL: `https://oceanveil-auto-catalog.github.io/manifest.json`

Twice a day, GitHub Actions rebuilds a static addon that mirrors OceanVeil's homepage:

- **Homepage rows, in the site's order and with the site's names.** The row layout is read from
  the config oceanveil.net embeds in its own HTML, so curated rows ("This Fall's Must-See Titles",
  "Going Viral!", …) change automatically when the site changes them. Fixed rows (SimulCast,
  Newest Addition, Ranking, English Dubbed, Completed, genre sections) keep stable catalog IDs;
  curated rows use positional slots (`oceanveil-home-promo-1`, `-2`, …).
- **One Browse catalog** instead of six near-identical filter rows: the board shows All Titles, and
  Discover offers SFW, 16-Plus, NSFW, Mix (SFW + NSFW) and NSFW Dubbed as a filter. Classification
  uses the site's own age rating and its `NSFW` / `Dubbed` tags.
- **Dub and sub copies are one entry.** `[Dub] X` and `X` share one match and one poster; the
  detail page links both versions.

Titles are matched through Kitsu, the anime-offline-database (all English, romaji and Japanese
synonyms, so "Yuru Yuri Season 2" or "Ranpo Kitan" still match), AniList, the Fribb crosswalk and TMDB. How each title is served, best first:

1. **IMDb `tt` ID**: your Cinemeta/TMDB and stream addons handle it as usual.
2. **Kitsu ID only**: our own detail page whose episodes are `kitsu:<id>:<ep>`, so anime stream
   addons that understand Kitsu IDs (Torrentio and others) can return streams.
3. **TMDB ID only**: `tmdb:<id>` (works in Nuvio and with TMDB-aware addons).
4. **No IMDb or Kitsu ID exists** (many short adult ONAs are only on MyAnimeList): our own page
   with an **Open on OceanVeil** stream. Torrentio only accepts IMDb and Kitsu IDs, so it can't
   be asked for these.

## Fewer titles than the site shows?

OceanVeil only lists licensed shows (mostly PG/13+ titles like Angel Beats!, Oreimo, Terror in
Resonance) to some regions. GitHub's runners may get a cut-down list of mostly 16+ originals, and
then every row repeats the same few titles. `status.json` → `source_region_check` shows what the
run saw, and the Actions log prints a warning when the list looks limited. To fix it, add an
`OCEANVEIL_PROXY` secret (an HTTP(S) proxy in a region where the full catalog shows). It is used for
OceanVeil requests only.

Data comes only from OceanVeil's public, logged-out API (`/api/v1/anime_titles`,
`newest_addition_anime_titles`, `trending_anime_titles`). No sign-in or video URLs are used.
Titles that are only visible to signed-in or subscribed users can't be listed.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Needed | Purpose |
|---|---|---|
| `TMDB_API_KEY` | strongly recommended | ID matching, posters, synopses |
| `TVDB_API_KEY`, `MDBLIST_API_KEY` | optional | extra ID matching and ratings |
| `OCEANVEIL_PROXY` | if titles are missing | fetch OceanVeil's list from a region that sees all of it |

Matches are cached in `state.json`, so each title costs API calls once a week at most.

## Files

- `oceanveil.py`: public API client and homepage config parser
- `build.py`: matching and publishing (`public/` is generated, not committed)
- `enrichment.py`: Kitsu / offline DB / AniList / Fribb / TMDB / TVDB / MDBList ID matching
- `public/status.json` (live site): what the last run found, region check, link-only titles, row counts

Run locally: `pip install -r requirements.txt && python -m unittest discover -s tests && python build.py`
