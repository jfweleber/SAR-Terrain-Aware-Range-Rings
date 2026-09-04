# WiSAR Terrain-Aware Range Rings

Flask backend + single-page Leaflet front end at **sar.weleber.net**, gunicorn
on 127.0.0.1:8000. **Real search-and-rescue people use this.** Treat production
accordingly: it is authoritative, and git mirrors it rather than the reverse.

Shared conventions and deploy rules live in `D:\Projects\CLAUDE.md`.

## What it computes

From an IPP (initial planning point) it builds a terrain friction surface and
runs an anisotropic Dijkstra cost-distance over it, then contours the result:

- **TARR mode** — thresholds cost-distance at Koester Lost Person Behavior
  p25/p50/p75 find distances into three nested polygons.
- **Travel Time mode** — divides cost-distance by a user speed into isochrones.

A Jacobs (2015) terrain-attractor heatmap is drawn over either. Outputs are PNG
overlays, GeoTIFFs, KML/GeoJSON, and a push to CalTopo.

## The three things most likely to bite

**Everything is synchronous.** `/api/analyze` blocks for *minutes* — a
pure-Python `heapq` Dijkstra over up to 1000×1000 cells plus roughly six
external HTTP fetches with 20–120 s timeouts each. The front end fakes progress
with an 8-second message rotator; it is not real progress. The long gunicorn and
nginx timeouts that make this work are configured **only on the server**, so any
timeout change there can silently break the app.

**`WORK_DIR` is created once per process, not per run** (`pipeline/shared.py`),
despite a comment claiming otherwise. Intermediate rasters have fixed names
(`dem.tif`, `cost_surface.tif`, `cost_distance.tif`, `jacobs_masks.tif`). **Two
concurrent analyses in one worker overwrite each other.** There is no queue and
no job id in the filenames.

**`analysis_id` is just the rounded coordinates** — `f"{lat:.4f}_{lng:.4f}"`.
Two users starting from the same IPP collide and overwrite each other's results.
The `analyses` dict is global and never evicts, and `/tmp/wisar_results/*.json`
is never cleaned; after a restart those JSONs still reference the old process's
raster paths.

## Code that looks wrong and is not — do not "clean up"

- **Burn order in `build_cost_surface`**: trails and roads are burned *last* at
  impedance 1.0 so bridges and crossings stay passable over water.
- **`_compute_attractor_score_max` uses `max`, not `sum`** — summing would
  double-count Jacobs's overlapping categories.
- **`JACOBS_STREAM_STRAHLER_MIN = 3`** deliberately deviates from Jacobs's ≥5.
- **The endpoint named `cost_surface.png` no longer renders the cost surface**,
  and **`export-tarrs` handles isochrones too** (mode detected by an `hours`
  property, duplicated in `app.js` — keep both in sync). Both names are frozen
  for front-end compatibility.
- **CalTopo TARR descriptions are word-for-word frozen** because field reports
  reference the wording.
- **`WISAR_USER_AGENT` is mandatory** — Overpass returns 406 to the default
  requests UA.
- The `.png` routes are more specific than `/<filename>`; do not add a
  `<filename>` variant that shadows them.

## Suspected real bug, verify before touching

`app.js` multiplies p25/p50/p75 by per-band `CALIBRATION_MULTIPLIERS` **and**
sends `params.profile`; `server.py` then applies a *second*, scalar multiplier
(default 1.40) to the already-calibrated values. The two tables disagree in both
shape and values, and the README states calibration is front-end only. This
changes search areas that real teams act on — **verify against production
behaviour before changing either table.**

## Environment and integrations

CalTopo credentials come from `/etc/wisar.env` (root-only, mode 600) via the
systemd unit — `CALTOPO_ACCOUNT_ID`, `CALTOPO_CREDENTIAL_ID`,
`CALTOPO_CREDENTIAL_KEY`. They are **not** in this repo and must never be. The
app only warns if they are missing, so a broken export looks like a silent
no-op.

Every analysis hits the network live: USGS 3DEP, MRLC WMS, three Overpass
mirrors, three NHD MapServer layers. Nothing is HTTP-cached.

`app/requirements.txt` was captured from the production venv. The geospatial
stack is tightly coupled — rasterio, geopandas, pyogrio and fiona all bind the
same GDAL — so upgrade the set together on a rebuilt venv and run a real
analysis before deploying. System packages (`gdal-bin`, `libgdal-dev`,
`python3-gdal`, `libspatialindex-dev`) come from `provision.sh` phase 10.

## The OSM cache

`/var/www/sar.weleber.net/cache/osm/` holds `osm_cache.gpkg` plus metadata,
rebuilt weekly by cron from `tools/build_osm_cache.py` (11 Geofabrik state PBFs,
filtered with `osmium`, streamed as Arrow batches — it OOM'd on California
before the batching rewrite). It is a **failure-only fallback**: live Overpass
responses are never cached into it.

Cache paths are hardcoded in **two** places — `pipeline/osm_cache.py` and
`tools/build_osm_cache.py` — deliberately, so cron does not need the package on
`sys.path`. Change one, change both.

The 5.2 GB cache lives *outside* `app/`, which is why the deploy cannot touch
it. It is regenerable and deliberately not backed up.

## Known stale documentation

`app/static/metadata.html` is orphaned and stale; `app/README.md` describes
modes that no longer exist; several comments in `index.html` describe an inline-
JS layout and a `/api/caltopo` proxy that are both gone. The root `README.md` is
current. Do not trust in-repo prose over the code.

## Resolution caveat

Rasters are capped at 1000 px, which silently degrades cell size. Comments claim
30 m cells, but Travel Time at 3 mph × 12 h forces a ~60 km radius and roughly
120 m cells. Nothing warns the user, and the beta track that used to exist for
testing this was retired — changes go straight to the tool teams rely on.
