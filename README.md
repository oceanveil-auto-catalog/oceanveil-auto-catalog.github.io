# OceanVeil Auto Catalog — V2

**Unofficial metadata-only addon for Stremio and Nuvio.** This repo publishes a Stremio-compatible static catalog on GitHub Pages. It checks public title sources every day and preserves previously found records. It does **not** supply streams, log into OceanVeil, bypass subscriptions, or access paid content.

## Why V2 exists

The first version failed on its first GitHub Actions run because `https://manhwaindex.com/platform/oceanveil` returned `403 Forbidden`, the official pages yielded no extractable title links, and there was no saved catalog. That prevented the *build* and therefore the *deploy* step.

V2 fixes the **empty first-run deployment** issue:

- `seed.json` includes **34 titles** appearing in public OceanVeil-related lists as of October 12, 2026. This is a starter snapshot, **not the whole library**. It gives the addon something to publish even if public discovery is temporarily unavailable.
- The daily job tries the original permitted HTML/sitemap sources. If ordinary HTML doesn't contain title links, it also tries **Chromium rendering** of OceanVeil's public landing/catalog pages (no login and respecting a readable `robots.txt`). The browser does not circumvent access restrictions.
- `status.json` tells you **how many titles actually came from live discovery**. A green deployment means the catalog published, *not* necessarily that it detected new releases.
- Stored titles survive unavailable sources on subsequent runs.

**Important limitation:** We have not been able to verify the browser-discovery fallback against live OceanVeil from this environment, and the third-party index blocked the GitHub runner in your first run. Thus, *fully automatic addition of every new OceanVeil title is still unproven*. V2 resolves the observed build/deploy failure, not access restrictions imposed by source sites. It will append new titles if it can actually extract new public links.

## Replace your existing GitHub repository

Your screenshot indicates:

- GitHub repository: `https://github.com/oceanveil-auto-catalog/oceanveil-auto-catalog`
- Expected Pages site: `https://oceanveil-auto-catalog.github.io/oceanveil-auto-catalog/`
- Expected Stremio/Nuvio install URL: **`https://oceanveil-auto-catalog.github.io/oceanveil-auto-catalog/manifest.json`**

1. **Extract** the V2 ZIP. Go inside its `oceanveil-auto-catalog` folder.
2. In your GitHub repository, replace `build.py`, `requirements.txt`, `README.md`, `.github/workflows/update.yml`, and `tests/test_build.py` with the new versions, and **add `seed.json`**. Keep `static/placeholder.png`. You can use GitHub Desktop or upload the whole extracted project contents from your computer. **Do not upload the ZIP file itself.**
3. Commit all changes to **`main`**. Be sure `.github/workflows/update.yml` is present in the repo as a *nested path*, not renamed to `update.yml` in the root.
4. In **Settings → Pages**, keep **Source: GitHub Actions** (the screenshot shows this already correctly set).
5. Go to **Actions → Update and publish OceanVeil catalog → Run workflow**. The script installs Chromium, attempts discovery, and publishes the initial catalog even if the remote sources are blocked.
6. Wait for **build** and **deploy** to be green. First run may be slower because the browser is installed.
7. Open `https://oceanveil-auto-catalog.github.io/oceanveil-auto-catalog/status.json`. Check `known_title_count` and especially `live_titles_extracted_this_run` (if **0**, fresh-title discovery still isn't functioning, even if deployment succeeded).
8. Open `https://oceanveil-auto-catalog.github.io/oceanveil-auto-catalog/manifest.json`. Install this URL using Stremio's addon-by-URL field or Nuvio's Stremio-addon installation entry.

If Pages returns 404 immediately after your first green deployment, wait briefly for propagation, then reload. Verify the workflow's **deploy** job finished successfully.

## Automatic updating

The GitHub Actions job checks daily at **03:15 UTC** and also runs on pushes to `main`. A public title is added when the scraper can discover an OceanVeil `/anime_titles/123` link, and the source provides a usable show name. The state is written to `state.json` and committed to preserve discoveries.

Do not confuse **scheduled checks** with **guaranteed discovery**. If OceanVeil does not expose enough public title data and the third-party index stays blocked, this still behaves as a cached snapshot. There is no verified public comprehensive API for automatic enumeration in this project.

## What URLs should work after successful deployment?

- `/` — small catalog landing page
- `/manifest.json` — installation manifest
- `/status.json` — number of entries, live discovery status and per-source checks
- `/catalog/series/oceanveil-anime.json` — first 100 catalog items
- `/catalog/series/oceanveil-anime/skip=100.json` — subsequent items if available
- `/meta/series/oceanveil:seed-....json` — per-title metadata

The catalog uses custom `oceanveil:` IDs. **Other stream addons might not recognize those IDs**, and this project has no video/stream route. The initially seeded titles use a generic placeholder image until a permitted live source supplies artwork; do not expect polished posters for all seeded entries yet.

## Repo structure

```text
oceanveil-auto-catalog/
├── .github/
│   └── workflows/
│       └── update.yml       # Daily update, browser install and GitHub Pages deploy
├── build.py                 # Public sources, optional browser extraction and addon generation
├── seed.json                # Starter snapshot (34 sourced titles)
├── requirements.txt        # Python dependencies
├── static/
│   └── placeholder.png      # Fallback poster
├── tests/
│   └── test_build.py        # Offline tests, including simulated 403/blocked sources
└── README.md
```

The workflow generates and commits `state.json` when titles are found and creates `public/` as a temporary deploy artifact. You do **not** have to upload either directory manually.

## Run tests locally

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
ENABLE_BROWSER_DISCOVERY=0 python build.py
python -m http.server --directory public 8000
```

On Windows PowerShell use `$env:ENABLE_BROWSER_DISCOVERY="0"` before running `python build.py` instead of the inline environment variable above. To test Chromium mode locally, run `python -m playwright install chromium`, set `ENABLE_BROWSER_DISCOVERY=1`, then run `python build.py`.

## Data sources and limitations

- `https://oceanveil.net/` — OceanVeil's public listings and rendered public pages.
- `https://manhwaindex.com/platform/oceanveil` — third-party availability index; first GitHub Actions run reported **HTTP 403** from this source.
- Public sitemaps if reachable and if their robots policy permits access.

We don't circumvent access controls or make false completeness claims. Starter title names were sourced from the publicly indexed OceanVeil list and the official site's recently-streamed listing. Some titles may be retired, may be unavailable in your region, or may not map to a direct official detail URL. All third-party source pages retain their own rights to artwork and metadata.
