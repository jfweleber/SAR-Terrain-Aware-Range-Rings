#!/usr/bin/env python3
# ===============================================================================
# Script:       tools/build_osm_cache.py
# Purpose:      Build or refresh the local OSM cache used by the WiSAR pipeline
#               as a fallback when public Overpass endpoints fail. Downloads
#               Geofabrik state-level PBF extracts, filters them with
#               osmium-tool to just the feature categories used by the live
#               OSM path (trails, roads, waterways, power lines), and writes
#               a single spatially-indexed GeoPackage to
#               /var/www/sar.weleber.net/cache/osm/.
#
#               Intended to run weekly via cron. Safe to re-run: writes to a
#               temporary GeoPackage first and only replaces the live cache
#               at the end, so concurrent reads from the running Flask server
#               never see a half-built file.
#
# Dependencies:
#   - osmium-tool (apt install osmium-tool)  — PBF filtering
#   - geopandas                              — reads filtered PBF via GDAL
#   - pyogrio or fiona (bundled with geopandas) — I/O backend
#
# Usage:
#   Manual (first build):    python3 tools/build_osm_cache.py
#   Manual (verbose):        python3 tools/build_osm_cache.py --verbose
#   Manual (keep PBFs):      python3 tools/build_osm_cache.py --keep-pbfs
#   Manual (one state):      python3 tools/build_osm_cache.py --states arizona
#   Cron (weekly Sun 3AM MST): 0 10 * * 0 /usr/bin/python3 /var/www/sar.weleber.net/tools/build_osm_cache.py >> /var/www/sar.weleber.net/cache/osm/build.log 2>&1
#
# Author:       Jamie F. Weleber
# Created:      April 2026 (v1.11 cache fallback)
# ===============================================================================

import os                       # Filesystem and path operations
import sys                      # Exit codes and stderr
import json                     # Write the metadata sidecar
import time                     # Timing the build for the log
import shutil                   # Atomic file replacement and disk space checks
import argparse                 # CLI flags (--verbose, --keep-pbfs, --states)
import logging                  # Structured logging to stdout for cron capture
import subprocess               # Invoke osmium-tool for PBF filtering
from datetime import datetime, timezone  # ISO 8601 timestamps in metadata

import requests                 # HTTP download of PBF files from Geofabrik
import geopandas as gpd         # Read filtered PBFs, merge, write GeoPackage
import pandas as pd             # concat for combining per-state frames


# ===============================================================================
# STEP 1: Configuration
# ===============================================================================

# Cache output location — must match the path in pipeline/osm_cache.py.
# Kept as constants in two places (not imported) so this script runs
# standalone from cron without needing the pipeline package on sys.path.
CACHE_DIR = '/var/www/sar.weleber.net/cache/osm'
CACHE_GPKG = os.path.join(CACHE_DIR, 'osm_cache.gpkg')
CACHE_GPKG_TMP = os.path.join(CACHE_DIR, 'osm_cache.gpkg.tmp')
CACHE_METADATA = os.path.join(CACHE_DIR, 'osm_cache_metadata.json')
CACHE_METADATA_TMP = os.path.join(CACHE_DIR, 'osm_cache_metadata.json.tmp')
PBF_WORK_DIR = os.path.join(CACHE_DIR, 'pbf')

# Geofabrik state extracts covered by the cache. Keyed by a short slug used
# for local filenames. Expand this list by adding more states; each adds
# one more PBF download but the filter/merge step scales linearly.
GEOFABRIK_STATES = {
    'arizona':    'https://download.geofabrik.de/north-america/us/arizona-latest.osm.pbf',
    'california': 'https://download.geofabrik.de/north-america/us/california-latest.osm.pbf',
    'utah':       'https://download.geofabrik.de/north-america/us/utah-latest.osm.pbf',
    'nevada':     'https://download.geofabrik.de/north-america/us/nevada-latest.osm.pbf',
    'new-mexico': 'https://download.geofabrik.de/north-america/us/new-mexico-latest.osm.pbf',
    'washington': 'https://download.geofabrik.de/north-america/us/washington-latest.osm.pbf',
    'oregon':     'https://download.geofabrik.de/north-america/us/oregon-latest.osm.pbf',
    'idaho':      'https://download.geofabrik.de/north-america/us/idaho-latest.osm.pbf',
    'montana':    'https://download.geofabrik.de/north-america/us/montana-latest.osm.pbf',
    'wyoming':    'https://download.geofabrik.de/north-america/us/wyoming-latest.osm.pbf',
    'colorado':   'https://download.geofabrik.de/north-america/us/colorado-latest.osm.pbf',
}

# Highway tag values kept as "trails" (foot-traffic corridors SAR subjects follow).
# Must match the regex used by download_osm_features() in pipeline/downloads.py.
TRAIL_TAGS = {'path', 'footway', 'track', 'bridleway', 'cycleway'}

# Highway tag values kept as "roads" (vehicular corridors). Same match as live.
ROAD_TAGS = {'residential', 'tertiary', 'secondary', 'primary', 'trunk',
             'motorway', 'unclassified', 'service'}

# Waterway tag values kept. Same match as live.
WATERWAY_TAGS = {'stream', 'river', 'canal', 'drain', 'ditch'}

# Power tag values kept.
POWER_TAGS = {'line', 'minor_line'}

# Minimum free disk space to start (GB) — prevents a cron job from filling
# the root filesystem and breaking the running Flask process.
MIN_FREE_DISK_GB = 18

# osmium-tool command name — resolved via PATH. Exposed as a constant so
# Debian-like systems that ship it as `osmium-tool` can swap in one place.
OSMIUM_BIN = 'osmium'


# ===============================================================================
# STEP 2: Logging setup
# ===============================================================================

def setup_logging(verbose):
    """Configure stdout logging with timestamps for cron capture."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='[%(asctime)s] %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        stream=sys.stdout,
    )


# ===============================================================================
# STEP 3: Preflight checks
# ===============================================================================

def check_disk_space(required_gb):
    """Abort early if disk is too full to safely build the cache.

    Worst-case full build (11 states): the new osm_cache.gpkg.tmp is built
    alongside the still-live osm_cache.gpkg, plus ~4 GB of raw and filtered
    PBFs held until end-of-run cleanup. Peak is roughly 16-17 GB. The 18 GB
    floor keeps this preflight an honest guard rather than a number the real
    build can quietly exceed.
    """
    try:
        stat = shutil.disk_usage(CACHE_DIR)
    except FileNotFoundError:
        # CACHE_DIR doesn't exist yet on first run — check its parent
        parent = os.path.dirname(CACHE_DIR)
        stat = shutil.disk_usage(parent)
    free_gb = stat.free / (1024 ** 3)
    if free_gb < required_gb:
        logging.error(f"Only {free_gb:.1f} GB free at {CACHE_DIR}, need {required_gb} GB. Aborting.")
        sys.exit(3)
    logging.info(f"Disk space OK: {free_gb:.1f} GB free")


def check_osmium_available():
    """Verify osmium-tool is installed and callable.

    Fails fast with a helpful message if not — better than a cryptic
    FileNotFoundError deep in the filter step, especially from cron where
    no one is watching stderr.
    """
    try:
        result = subprocess.run(
            [OSMIUM_BIN, '--version'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"osmium --version exited {result.returncode}")
        # First line is typically "osmium version X.Y.Z"
        version_line = result.stdout.split('\n')[0].strip()
        logging.info(f"osmium-tool available: {version_line}")
    except FileNotFoundError:
        logging.error("osmium-tool not found on PATH. Install with: "
                      "sudo apt install osmium-tool")
        sys.exit(6)
    except Exception as e:
        logging.error(f"osmium-tool check failed: {e}")
        sys.exit(6)


def ensure_directories():
    """Create cache and PBF working directories if they don't exist."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(PBF_WORK_DIR, exist_ok=True)


# ===============================================================================
# STEP 4: Download PBF files from Geofabrik
# ===============================================================================

def download_pbf(slug, url, dest_path):
    """Download a single Geofabrik PBF.

    If a file is already on disk and was modified within the last 24 hours,
    we skip the download — Geofabrik rebuilds extracts daily around 20:20
    UTC, so a same-day local copy is current enough for a weekly cache.
    This makes manual re-runs during development cheap.

    Args:
        slug: Short identifier used for progress logging (e.g., 'arizona')
        url: Full Geofabrik download URL for the .osm.pbf file
        dest_path: Local filesystem destination

    Returns:
        bool: True if the download succeeded or a valid cached copy exists.
    """
    if os.path.isfile(dest_path):
        mtime = os.path.getmtime(dest_path)
        age_hours = (time.time() - mtime) / 3600
        if age_hours < 24:
            size_mb = os.path.getsize(dest_path) / (1024 ** 2)
            logging.info(f"  {slug}: using cached PBF ({size_mb:.0f} MB, "
                         f"{age_hours:.1f} h old)")
            return True

    logging.info(f"  {slug}: downloading {url}")
    start = time.time()
    try:
        # Stream the response so multi-GB California doesn't load into memory.
        # 120s per-chunk timeout (not total) handles Geofabrik's sometimes
        # slow bulk downloads without falsely timing out.
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total_bytes = int(r.headers.get('content-length', 0))
            # Write to .part first, rename on success — so a killed download
            # doesn't leave a truncated PBF that looks complete to the next run
            tmp_path = dest_path + '.part'
            downloaded = 0
            last_report = start
            with open(tmp_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):  # 1 MB chunks
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        # Progress log every 30s for large files
                        now = time.time()
                        if now - last_report > 30 and total_bytes > 0:
                            pct = 100 * downloaded / total_bytes
                            logging.info(f"    {slug}: {pct:.0f}% "
                                         f"({downloaded/1024/1024:.0f}/"
                                         f"{total_bytes/1024/1024:.0f} MB)")
                            last_report = now
            os.rename(tmp_path, dest_path)
        elapsed = time.time() - start
        size_mb = os.path.getsize(dest_path) / (1024 ** 2)
        logging.info(f"  {slug}: downloaded {size_mb:.0f} MB in {elapsed:.0f}s")
        return True
    except Exception as e:
        logging.error(f"  {slug}: download failed — {e}")
        tmp_path = dest_path + '.part'
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


# ===============================================================================
# STEP 5: Filter PBF with osmium-tool
# ===============================================================================

def filter_pbf_with_osmium(raw_pbf_path, filtered_pbf_path):
    """Filter a raw Geofabrik PBF down to just our feature classes.

    osmium-tool's `tags-filter` subcommand reads the raw PBF once and writes
    a new PBF containing only ways matching the specified tag expressions,
    plus the nodes those ways reference. This shrinks each state from
    100 MB–1.2 GB down to typically 30–100 MB — a ~10x reduction that makes
    the subsequent GDAL read much faster and lighter on memory.

    Filter expressions match the tag patterns used by download_osm_features()
    in pipeline/downloads.py. The syntax `w/highway=path,footway,...` means
    "ways where the highway tag is any of these values"; multiple expressions
    combine as OR.

    Args:
        raw_pbf_path: Path to the full Geofabrik state extract
        filtered_pbf_path: Where to write the filtered PBF

    Returns:
        bool: True if filtering succeeded, False on any osmium-tool error.
    """
    # Build the filter expressions. Comma-separated values inside a single
    # expression mean "any of these"; combining highway/waterway/power into
    # one osmium run is faster than three separate passes over the PBF.
    trail_expr = 'w/highway=' + ','.join(sorted(TRAIL_TAGS))
    road_expr = 'w/highway=' + ','.join(sorted(ROAD_TAGS))
    waterway_expr = 'w/waterway=' + ','.join(sorted(WATERWAY_TAGS))
    power_expr = 'w/power=' + ','.join(sorted(POWER_TAGS))

    cmd = [
        OSMIUM_BIN, 'tags-filter',
        '-o', filtered_pbf_path,
        '--overwrite',   # allow re-running without manually deleting prior output
        raw_pbf_path,
        trail_expr, road_expr, waterway_expr, power_expr,
    ]

    logging.debug(f"  running: {' '.join(cmd)}")
    start = time.time()
    try:
        # Let osmium-tool write progress to stderr but capture it so we can
        # log warnings without noisy unfiltered output in journald.
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800,  # 30 min max
        )
        if result.returncode != 0:
            logging.error(f"  osmium tags-filter failed (exit {result.returncode}): "
                          f"{result.stderr.strip()}")
            return False
        elapsed = time.time() - start
        out_size_mb = os.path.getsize(filtered_pbf_path) / (1024 ** 2)
        in_size_mb = os.path.getsize(raw_pbf_path) / (1024 ** 2)
        logging.info(f"    filtered {in_size_mb:.0f} MB -> {out_size_mb:.0f} MB "
                     f"in {elapsed:.0f}s")
        return True
    except subprocess.TimeoutExpired:
        logging.error(f"  osmium tags-filter timed out after 30 minutes")
        return False


# ===============================================================================
# STEP 6: Stream-read filtered PBF in Arrow batches and write per-category slices
# ===============================================================================

def _split_batch(batch_gdf):
    """Split a single Arrow batch GeoDataFrame into four category frames.

    Uses vectorized boolean masking on highway/waterway/power tags — the
    same logic as the (previous non-batched) read_filtered_pbf, but applied
    to one ~65,000-feature batch at a time instead of the whole state.

    The input frame is expected to have columns: geometry, name, highway,
    waterway, other_tags (as loaded via pyogrio's Arrow stream with the
    columns= parameter).

    Args:
        batch_gdf: GeoDataFrame of one Arrow batch (no CRS yet; caller sets it)

    Returns:
        Dict of four small GeoDataFrames — may have zero rows in any category.
    """
    # Drop rows with null/empty geometry up front — can't contribute to any
    # category and would fail the GPKG write if included.
    batch_gdf = batch_gdf[
        batch_gdf.geometry.notna() & ~batch_gdf.geometry.is_empty
    ].copy()

    if len(batch_gdf) == 0:
        return {k: _empty_frame(k) for k in ('trails', 'roads', 'waterways', 'powerlines')}

    # Normalize name: may contain NaN from Arrow → pandas conversion
    if 'name' in batch_gdf.columns:
        batch_gdf['name'] = batch_gdf['name'].fillna('').astype(str)
    else:
        batch_gdf['name'] = ''

    # Extract 'power' and 'width' from other_tags with vectorized regex.
    # HSTORE format: '"key1"=>"val1","key2"=>"val2"'. regex is applied to
    # the whole batch in one pass via pandas str accessor.
    if 'other_tags' in batch_gdf.columns:
        batch_gdf['_power'] = batch_gdf['other_tags'].str.extract(
            r'"power"=>"([^"]*)"', expand=False)
        batch_gdf['_width'] = batch_gdf['other_tags'].str.extract(
            r'"width"=>"([^"]*)"', expand=False).fillna('')
    else:
        batch_gdf['_power'] = None
        batch_gdf['_width'] = ''

    # Safety: make sure highway/waterway columns exist so .isin doesn't crash
    # on a weirdly-empty batch (shouldn't happen after osmium filtering but
    # defensive coding here costs nothing).
    if 'highway' not in batch_gdf.columns:
        batch_gdf['highway'] = None
    if 'waterway' not in batch_gdf.columns:
        batch_gdf['waterway'] = None

    # Build boolean masks — vectorized .isin() respects NaN correctly.
    # Precedence matches the live Overpass path: trails > roads > waterways > powerlines.
    trails_mask = batch_gdf['highway'].isin(TRAIL_TAGS)
    roads_mask = batch_gdf['highway'].isin(ROAD_TAGS) & ~trails_mask
    waterways_mask = (batch_gdf['waterway'].isin(WATERWAY_TAGS)
                      & ~trails_mask & ~roads_mask)
    powerlines_mask = (batch_gdf['_power'].isin(POWER_TAGS)
                       & ~trails_mask & ~roads_mask & ~waterways_mask)

    # Slice into category frames with the right columns for each schema.
    trails = batch_gdf.loc[trails_mask, ['geometry', 'name']].copy()
    trails['type'] = 'trail'
    trails = trails[['geometry', 'type', 'name']]

    roads = batch_gdf.loc[roads_mask, ['geometry', 'name']].copy()
    roads['type'] = 'road'
    roads = roads[['geometry', 'type', 'name']]

    waterways = batch_gdf.loc[
        waterways_mask, ['geometry', 'name', 'waterway', '_width']
    ].copy()
    waterways['type'] = waterways['waterway']
    waterways['width'] = waterways['_width']
    waterways = waterways[['geometry', 'type', 'name', 'width']]

    powerlines = batch_gdf.loc[powerlines_mask, ['geometry', 'name', '_power']].copy()
    powerlines['type'] = powerlines['_power']
    powerlines = powerlines[['geometry', 'type', 'name']]

    return {
        'trails': trails,
        'roads': roads,
        'waterways': waterways,
        'powerlines': powerlines,
    }


def process_state_in_batches(filtered_pbf_path, state_slug, output_path,
                              is_first_state, source_state_tag):
    """Stream a filtered PBF in Arrow batches and write each category slice.

    This is the memory-efficient replacement for the previous
    read_filtered_pbf() + append_state_to_gpkg() sequence. Peak memory
    usage is bounded to one Arrow batch (~65,000 features, ~30-50 MB)
    regardless of the state's total size — so California's ~6M features
    fit on a 4 GB server with room to spare.

    Flow:
      1. Open the filtered PBF via pyogrio's Arrow stream interface
      2. For each batch: convert to GeoDataFrame, vectorized split into
         4 categories, append each non-empty category to its GPKG layer
      3. Accumulate feature counts and bounds across batches
      4. Ensure all four layers exist in the GPKG even if some categories
         had zero features (important for the first state — osm_cache.py
         expects all four layer names to resolve)

    Args:
        filtered_pbf_path: PBF already filtered by osmium tags-filter
        state_slug: Short identifier for logging and the source_state column
        output_path: Destination GeoPackage path
        is_first_state: True when writing the very first state to the GPKG —
            determines whether layer creation (mode='w') or append (mode='a')
            is used for the first batch. Subsequent batches within the same
            state always use mode='a'.
        source_state_tag: String value written to the 'source_state' column
            of every row (typically the state_slug, for debuggability)

    Returns:
        Tuple of (feature_counts_dict, bounds_tuple_or_None) for metadata.
        feature_counts_dict maps each layer name → total rows written.
        bounds_tuple is (minx, miny, maxx, maxy) across all categories, or
        None if the state produced zero features.
    """
    # Import pyogrio's Arrow interface lazily — allows the rest of the
    # script (e.g., --help) to run even on systems where pyogrio is too old
    # to have open_arrow. The script will still fail if we actually try to
    # process data, just with a clearer error message here.
    try:
        from pyogrio.raw import open_arrow
    except ImportError as e:
        logging.error(f"  pyogrio.raw.open_arrow not available: {e}")
        logging.error("  Upgrade pyogrio: pip install --upgrade pyogrio")
        raise

    # pyarrow is needed to iterate the ArrayStream — it's usually pulled in
    # as a transitive dep of pyogrio but not guaranteed.
    try:
        import pyarrow as pa
    except ImportError:
        logging.error("  pyarrow not installed. Install with: pip install pyarrow")
        raise

    # shapely.from_wkb is how we convert Arrow's WKB geometry bytes back
    # into shapely geometry objects.
    from shapely import from_wkb

    logging.info(f"  {state_slug}: streaming filtered PBF in Arrow batches")
    t0 = time.time()

    # Running totals, updated as each batch completes.
    total_counts = {'trails': 0, 'roads': 0, 'waterways': 0, 'powerlines': 0}
    # Bounds accumulated as (minx_so_far, miny_so_far, maxx_so_far, maxy_so_far).
    # None until the first non-empty batch produces a bbox.
    overall_bounds = None
    # Track which layers have already been created in the GPKG during this
    # state's write pass — after first write of a layer, subsequent writes
    # must use mode='a' even if is_first_state=True.
    # We also need to ensure ALL four layers exist by the end, so any layer
    # that never received a batch-write gets an empty placeholder at the end.
    layers_written = set()
    batch_count = 0

    with open_arrow(
        filtered_pbf_path,
        layer='lines',
        use_pyarrow=True,                   # return a pyarrow RecordBatchReader
        columns=['name', 'highway', 'waterway', 'other_tags'],
    ) as source:
        meta, reader = source
        geom_col = meta.get('geometry_name') or 'wkb_geometry'

        for batch in reader:
            batch_count += 1
            # Convert the Arrow batch to a pandas DataFrame, then wrap as
            # GeoDataFrame after decoding WKB geometry bytes. This is the
            # same work that gpd.read_file would do internally, just for
            # one batch at a time.
            df = batch.to_pandas()
            # Decode WKB → shapely geometries for this batch only
            df['geometry'] = from_wkb(df[geom_col].values)
            if geom_col != 'geometry':
                df = df.drop(columns=[geom_col])
            batch_gdf = gpd.GeoDataFrame(df, geometry='geometry', crs='EPSG:4326')

            # Split the batch into four category frames
            categories = _split_batch(batch_gdf)

            # Track bounds from this batch (across all categories)
            for cat_gdf in categories.values():
                if len(cat_gdf) > 0:
                    b = cat_gdf.total_bounds  # (minx, miny, maxx, maxy)
                    if overall_bounds is None:
                        overall_bounds = tuple(b)
                    else:
                        overall_bounds = (
                            min(overall_bounds[0], b[0]),
                            min(overall_bounds[1], b[1]),
                            max(overall_bounds[2], b[2]),
                            max(overall_bounds[3], b[3]),
                        )

            # Write each non-empty category to the GeoPackage. The first
            # write to a given layer uses mode='w' only if this is the
            # first state overall — otherwise 'a' to append to the layer
            # created by a prior state. Subsequent writes within this
            # state always 'a'.
            for layer_name, cat_gdf in categories.items():
                if len(cat_gdf) == 0:
                    continue
                # Ensure CRS (slicing/copying in _split_batch may drop it)
                cat_gdf = gpd.GeoDataFrame(
                    cat_gdf, geometry='geometry', crs='EPSG:4326')
                # Add source_state tag for debugging
                cat_gdf['source_state'] = source_state_tag

                if is_first_state and layer_name not in layers_written:
                    mode = 'w'
                else:
                    mode = 'a'

                cat_gdf.to_file(
                    output_path,
                    layer=layer_name,
                    driver='GPKG',
                    mode=mode,
                    SPATIAL_INDEX='YES',
                )
                layers_written.add(layer_name)
                total_counts[layer_name] += len(cat_gdf)

            # Explicit cleanup before reading the next batch — Python's GC
            # is usually smart enough but being explicit keeps peak memory
            # predictable in a long-running batch loop.
            del df, batch_gdf, categories

            # Progress log every 20 batches (~1.3M features)
            if batch_count % 20 == 0:
                running = sum(total_counts.values())
                logging.debug(f"    {state_slug}: {batch_count} batches, "
                              f"{running} features written so far")

    # If this is the first state and some categories had zero features
    # across the entire stream, they were never written — create empty
    # layers now so osm_cache.py doesn't fail when it tries to read them.
    if is_first_state:
        for layer_name in ('trails', 'roads', 'waterways', 'powerlines'):
            if layer_name not in layers_written:
                logging.debug(f"  {state_slug}: creating empty layer '{layer_name}'")
                empty = _empty_frame(layer_name)
                empty['source_state'] = pd.Series(dtype='object')
                empty.to_file(
                    output_path,
                    layer=layer_name,
                    driver='GPKG',
                    mode='w',
                    SPATIAL_INDEX='YES',
                )

    elapsed = time.time() - t0
    logging.info(f"  {state_slug}: {total_counts['trails']} trails, "
                 f"{total_counts['roads']} roads, "
                 f"{total_counts['waterways']} waterways, "
                 f"{total_counts['powerlines']} powerlines "
                 f"({batch_count} batches, {elapsed:.0f}s)")
    return total_counts, overall_bounds


def _empty_frame(layer_name):
    """Return an empty GeoDataFrame with the canonical schema for a layer."""
    schemas = {
        'trails': ['geometry', 'type', 'name'],
        'roads': ['geometry', 'type', 'name'],
        'waterways': ['geometry', 'type', 'name', 'width'],
        'powerlines': ['geometry', 'type', 'name'],
    }
    return gpd.GeoDataFrame(columns=schemas[layer_name], crs='EPSG:4326')


def _empty_layers():
    """Return a dict of empty per-category frames — used when a read fails."""
    return {
        'trails': _empty_frame('trails'),
        'roads': _empty_frame('roads'),
        'waterways': _empty_frame('waterways'),
        'powerlines': _empty_frame('powerlines'),
    }


# ===============================================================================
# STEP 7: (removed — merged into STEP 6's batch-streaming implementation)
# ===============================================================================

def append_state_to_gpkg(state_layers, state_slug, output_path, is_first_state):
    """Deprecated — superseded by process_state_in_batches().

    Retained here only to avoid breaking any external script that imports
    this module. Not called by main() in the v1.11-final code path.
    """
    mode = 'w' if is_first_state else 'a'
    per_layer_counts = {}
    for layer_name in ('trails', 'roads', 'waterways', 'powerlines'):
        gdf = state_layers.get(layer_name)
        if gdf is not None and len(gdf) > 0:
            gdf = gdf.copy()
            gdf['source_state'] = state_slug
        else:
            if not is_first_state:
                per_layer_counts[layer_name] = 0
                continue
            gdf = _empty_frame(layer_name)
            gdf['source_state'] = pd.Series(dtype='object')
        per_layer_counts[layer_name] = len(gdf)
        gdf.to_file(output_path, layer=layer_name, driver='GPKG',
                    mode=mode, SPATIAL_INDEX='YES')
    return per_layer_counts


# ===============================================================================
# STEP 8: Metadata sidecar
# ===============================================================================

def write_metadata(counts, states, bbox, pbf_dates):
    """Write the cache metadata sidecar atomically.

    The sidecar is what pipeline/osm_cache.py reads to report cache age
    and coverage to the user. Written to a .tmp file first and renamed
    so readers never see a partial JSON file.
    """
    meta = {
        # ISO 8601 UTC — matches what cache_age_days() expects to parse
        'built_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'states': states,
        'bbox': list(bbox),   # (west, south, east, north) in EPSG:4326
        'feature_counts': counts,
        'source_pbf_dates': pbf_dates,
        'cache_version': 1,   # bump if the GeoPackage schema changes
    }
    with open(CACHE_METADATA_TMP, 'w') as f:
        json.dump(meta, f, indent=2)
    os.rename(CACHE_METADATA_TMP, CACHE_METADATA)
    logging.info(f"Metadata written to {CACHE_METADATA}")


# ===============================================================================
# STEP 9: Main driver
# ===============================================================================

def main():
    parser = argparse.ArgumentParser(description='Build the WiSAR OSM cache')
    parser.add_argument('--verbose', action='store_true',
                        help='Enable debug-level logging')
    parser.add_argument('--keep-pbfs', action='store_true',
                        help='Retain downloaded and filtered PBFs after build '
                             '(for debugging; default removes them to save disk)')
    parser.add_argument('--states', nargs='+', default=None,
                        help='Subset of states to build (default: all configured)')
    args = parser.parse_args()

    setup_logging(args.verbose)
    t_total = time.time()
    logging.info("=" * 70)
    logging.info("WiSAR OSM Cache Build")
    logging.info("=" * 70)

    states_to_build = args.states or list(GEOFABRIK_STATES.keys())
    unknown = [s for s in states_to_build if s not in GEOFABRIK_STATES]
    if unknown:
        logging.error(f"Unknown states: {unknown}. Known: {list(GEOFABRIK_STATES.keys())}")
        sys.exit(1)

    ensure_directories()
    check_disk_space(MIN_FREE_DISK_GB)
    check_osmium_available()

    # --- Step A: Download raw PBFs ---
    logging.info(f"\n[1/5] Downloading {len(states_to_build)} PBF extracts...")
    raw_pbf_paths = {}
    pbf_dates = {}
    for slug in states_to_build:
        url = GEOFABRIK_STATES[slug]
        dest = os.path.join(PBF_WORK_DIR, f'{slug}-latest.osm.pbf')
        if not download_pbf(slug, url, dest):
            logging.error(f"Aborting: could not download {slug}")
            sys.exit(4)
        raw_pbf_paths[slug] = dest
        mtime = os.path.getmtime(dest)
        pbf_dates[slug] = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime('%Y-%m-%d')

    # --- Step B: Filter each PBF with osmium-tool ---
    logging.info(f"\n[2/5] Filtering PBFs with osmium-tool...")
    filtered_pbf_paths = {}
    for slug, raw_path in raw_pbf_paths.items():
        filtered_path = os.path.join(PBF_WORK_DIR, f'{slug}-filtered.osm.pbf')
        logging.info(f"  {slug}:")
        if not filter_pbf_with_osmium(raw_path, filtered_path):
            logging.warning(f"  {slug}: filter failed, skipping this state")
            continue
        filtered_pbf_paths[slug] = filtered_path

    if not filtered_pbf_paths:
        logging.error("All states failed filtering. Aborting.")
        sys.exit(5)

    # --- Step C & D combined: Read, write, and drop per state ---
    # Streaming pattern — load one state, append to GeoPackage, release its
    # frames before reading the next state. This keeps peak memory bounded
    # to roughly one state's worth of features (~1-2 GB for California),
    # which fits comfortably on a 4 GB server. The previous all-at-once
    # approach OOM'd on California.
    logging.info(f"\n[3/4] Reading states and streaming to GeoPackage...")
    # Remove any stale .tmp from a prior crashed run
    if os.path.isfile(CACHE_GPKG_TMP):
        os.remove(CACHE_GPKG_TMP)

    # Accumulators for metadata, filled as we process each state.
    # feature_counts tracks total rows per layer across all states;
    # coverage_bounds tracks the union bbox; successful_states tracks
    # which states we actually got data from (partial builds are OK).
    feature_counts = {'trails': 0, 'roads': 0, 'waterways': 0, 'powerlines': 0}
    coverage_bounds = []
    successful_states = []
    is_first = True

    for slug, filtered_path in filtered_pbf_paths.items():
        try:
            state_counts, state_bounds = process_state_in_batches(
                filtered_path, slug, CACHE_GPKG_TMP,
                is_first_state=is_first, source_state_tag=slug)
        except Exception as e:
            logging.error(f"  {slug}: batch processing failed — {e}")
            # If the first state fails outright, we have no GeoPackage to
            # append to. Abort rather than continue with a broken state.
            if is_first:
                logging.error("First-state build failed, cannot continue.")
                sys.exit(7)
            continue

        # Accumulate totals and coverage bounds across states
        for layer, count in state_counts.items():
            feature_counts[layer] += count
        if state_bounds is not None:
            coverage_bounds.append(state_bounds)

        successful_states.append(slug)
        is_first = False

    if not successful_states:
        logging.error("No states successfully written to GeoPackage. Aborting.")
        sys.exit(5)

    # Compute the overall coverage bbox as the union of per-state bboxes.
    if coverage_bounds:
        minx = min(b[0] for b in coverage_bounds)
        miny = min(b[1] for b in coverage_bounds)
        maxx = max(b[2] for b in coverage_bounds)
        maxy = max(b[3] for b in coverage_bounds)
        cache_bbox = (minx, miny, maxx, maxy)
    else:
        cache_bbox = (0, 0, 0, 0)
        logging.warning("No feature geometries found to compute coverage bbox!")

    # Atomic replacement: on POSIX, os.replace is a single filesystem op, so
    # in-flight reads from the Flask process complete against the old file
    # and the next read starts against the new file — never half-written.
    logging.info(f"Promoting {CACHE_GPKG_TMP} → {CACHE_GPKG}...")
    os.replace(CACHE_GPKG_TMP, CACHE_GPKG)

    # --- Step E: Write metadata sidecar ---
    logging.info(f"\n[4/4] Writing metadata sidecar...")
    write_metadata(feature_counts, successful_states, cache_bbox, pbf_dates)

    # --- Cleanup ---
    if not args.keep_pbfs:
        logging.info("Removing downloaded and filtered PBFs (use --keep-pbfs to retain)...")
        for pbf_path in list(raw_pbf_paths.values()) + list(filtered_pbf_paths.values()):
            try:
                os.remove(pbf_path)
            except OSError as e:
                logging.warning(f"  could not remove {pbf_path}: {e}")

    elapsed = time.time() - t_total
    gpkg_size_mb = os.path.getsize(CACHE_GPKG) / (1024 ** 2)
    logging.info("=" * 70)
    logging.info(f"Build complete in {elapsed/60:.1f} minutes")
    logging.info(f"Cache: {CACHE_GPKG} ({gpkg_size_mb:.0f} MB)")
    logging.info(f"Coverage: {successful_states}")
    logging.info(f"Features: {feature_counts}")
    logging.info("=" * 70)
    return 0


if __name__ == '__main__':
    sys.exit(main())
