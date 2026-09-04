# ===============================================================================
# Module:       pipeline/jacobs_masks.py
# Purpose:      Compute the four terrain-attractor boolean masks per Jacobs
#               (2015) "Terrain Based Probability Models for SAR" for use in
#               the beta-only heatmap rendering variants. These masks are NOT
#               consumed by the cost surface, cost-distance, or TARR contour
#               math — they exist solely as inputs to the rendering pipeline
#               (server.py `serve_jacobs_*_png` routes) and represent visual
#               cues about where empirical SAR finds have historically
#               clustered, independent of distance from IPP.
#
#               Source paper: Jacobs, M. (2015). Terrain Based Probability
#               Models for SAR. Self-published.
#
#               Honesty notes carried over from the design discussion:
#                 - Jacobs's PDEN multipliers are derived from Oregon/NY/AZ
#                   ISRID data, mostly hikers and hunters. We use his findings
#                   to decide WHAT to highlight (which masks to compute), not
#                   to claim HOW MUCH to weight specific magnitudes. The
#                   visual treatment downstream uses conservative scaling.
#                 - These masks are valid for the visualization layer only.
#                   They are not bolted into the probability surface, segment
#                   POA calculation, or TARR contour extraction. The
#                   thesis-validated math is untouched.
#
# Author:       Jamie F. Weleber
# Created:      May 2026 (beta v0.1 — pre-v1.15 evaluation build)
# ===============================================================================

import os
import numpy as np
import rasterio
from rasterio.features import rasterize
from scipy.ndimage import uniform_filter, binary_dilation

from pipeline.shared import WORK_DIR


# ===============================================================================
# Jacobs mask thresholds
#
# These constants encode "which features matter" decisions from the paper,
# not magnitude calibration. Magnitudes are handled in the rendering layer.
# ===============================================================================

# Stream proximity: Jacobs's paper used Strahler >= 5 as the cutoff for
# meaningful standalone stream-PDEN signal in his (mostly eastern/PNW) data.
# In Coconino / Colorado Plateau terrain, Strahler 5+ flowlines are rare —
# Sycamore Creek (a named, operationally significant feature) is Strahler
# 3 or 4 in NHD, and a strict >=5 filter empties the stream mask entirely
# for most analyses we run. Per the design principle in
# jacobs_heatmap_design_notes.md ("Use Jacobs's findings to decide WHAT to
# highlight, not to claim HOW MUCH to weight it"), we lower the cutoff to
# 3 to match the operational reality of where SAR coordinators are
# searching. The cost-surface downloader already treats Strahler 3 as the
# "moderate creek" cutoff (downloads.py: 5m buffer, impedance 60), so this
# brings the Jacobs mask in line with the existing cost-surface definition
# of a significant water feature.
JACOBS_STREAM_STRAHLER_MIN = 3

# Stream buffer for proximity mask (~80m). Jacobs's paper used cumulative
# track offsets up to 200m but his stronger PDEN findings came from offsets
# under 80m. We use the tighter buffer for visualization clarity.
JACOBS_STREAM_BUFFER_DEG = 0.00072   # ~80m at mid-latitudes (1 deg ~= 111 km)

# Trail buffer for the stream-trail intersection mask. Jacobs's strongest
# single finding (~7-13x PDEN) was for the geometric intersection of trail
# and stream corridors, accounting for <1% of search area. We use a slightly
# wider trail buffer here than the cost surface uses (40m vs 30m) because
# the intersection mask is meant to capture "near both features" rather
# than "exactly on a trail."
JACOBS_TRAIL_BUFFER_DEG = 0.00036    # ~40m at mid-latitudes

# Local-window size for percentile-basis elevation. Jacobs used a 2km
# surrounding circle to compute percentile rank. At a typical 10m DEM
# resolution, 2km = ~200 cells, so the uniform_filter window is 201
# cells per side (odd for symmetry). This is the most computationally
# intensive piece of the module — see _compute_local_elevation_percentile
# for performance notes.
JACOBS_ELEV_WINDOW_CELLS = 201

# Low/high elevation cutoffs. Jacobs's strongest elevation findings were
# at the bottom and top 10% (percentile-basis < 0.1 / > 0.9) of local
# elevation distribution. Within those tail decades, the bottom 5% and
# top 5% showed even more concentrated signal. We adopt the 10% cutoffs
# as default — tightening to 5% would shrink the mask substantially.
JACOBS_LOW_ELEV_PERCENTILE = 0.10
JACOBS_HIGH_ELEV_PERCENTILE = 0.90


# ===============================================================================
# Mask computation primitives
# ===============================================================================

def _rasterize_geometries(geometries, transform, shape, buffer_deg=None):
    """Rasterize a list of shapely geometries into a boolean numpy array.

    Used for both stream and trail masks. Optionally buffers geometries
    before rasterization — this is how we apply Jacobs's offset distances
    to centerline geometry.

    Args:
        geometries: iterable of shapely geometry objects (any type)
        transform: rasterio Affine transform for the output grid
        shape: (height, width) tuple for the output grid
        buffer_deg: optional buffer distance in degrees (decimal lat/lng)
    Returns:
        Boolean numpy array of shape (height, width), True where any
        geometry overlaps the cell.
    """
    valid_shapes = []
    for geom in geometries:
        if geom is None or geom.is_empty:
            continue
        if buffer_deg is not None and buffer_deg > 0:
            try:
                geom = geom.buffer(buffer_deg)
                if geom.is_empty:
                    continue
            except Exception:
                # Some geometries can't be buffered cleanly (degenerate
                # linestrings, self-intersections). Skip rather than fail —
                # the mask is a coarse visual cue, not a precise boundary.
                continue
        valid_shapes.append((geom, 1))

    if not valid_shapes:
        return np.zeros(shape, dtype=bool)

    try:
        burned = rasterize(
            valid_shapes,
            out_shape=shape,
            transform=transform,
            fill=0,
            dtype=np.uint8,
            all_touched=True,   # Include any cell touched by geometry; bias toward inclusion
        )
        return burned > 0
    except Exception as e:
        print(f"  Jacobs mask rasterize failed: {e}")
        return np.zeros(shape, dtype=bool)


def _compute_local_elevation_percentile(dem, nodata_mask, window_cells):
    """Compute each cell's elevation percentile rank within a local window.

    For each cell, this returns a value in [0, 1] representing what fraction
    of the surrounding window has equal or lower elevation. 0 = cell is the
    lowest point in its window; 1 = highest.

    Jacobs's paper used a 2km surrounding circle. We approximate that with a
    square window of `window_cells` per side (~2km at 10m DEM resolution).
    Square vs circle introduces minor edge artifacts but is dramatically
    faster — important for keeping render-time performance acceptable.

    The exact percentile rank operation is expensive (O(window^2) per cell).
    We use a fast approximation: compute the local mean and local stddev
    using uniform_filter (O(N) total via separable convolution), then assume
    elevation is approximately normally distributed within the window and
    compute percentile rank from (cell - mean) / stddev. This is good enough
    for identifying tail features (top/bottom 10%) — what Jacobs's findings
    actually require — without the cost of true rank computation.

    Args:
        dem: 2D numpy array of elevation values
        nodata_mask: boolean array True where DEM is invalid
        window_cells: window edge length in cells (must be odd)
    Returns:
        Float array of same shape, values in [0, 1] for valid cells,
        0 for nodata cells.
    """
    # Mask nodata to nan so uniform_filter ignores them — but uniform_filter
    # doesn't natively handle nan. We replace with the global mean as a
    # placeholder, then re-mask the result. Edge cells near large nodata
    # regions will have slightly biased local means, but again, the mask is
    # a visual cue and small bias at the search-area boundary is acceptable.
    valid_dem = dem.copy()
    if np.any(nodata_mask):
        global_mean = float(np.nanmean(dem[~nodata_mask])) if np.any(~nodata_mask) else 0.0
        valid_dem[nodata_mask] = global_mean

    # Local mean via uniform filter — fast O(N) per pixel regardless of window size
    local_mean = uniform_filter(valid_dem, size=window_cells, mode='reflect')

    # Local variance: E[X^2] - (E[X])^2
    local_mean_sq = uniform_filter(valid_dem ** 2, size=window_cells, mode='reflect')
    local_var = np.maximum(local_mean_sq - local_mean ** 2, 1e-6)
    local_std = np.sqrt(local_var)

    # Z-score, then convert to approximate percentile rank using the normal
    # CDF approximation. We use scipy.stats.norm.cdf for accuracy; if scipy
    # availability becomes an issue, a polynomial approximation is also fine.
    from scipy.stats import norm
    z = (valid_dem - local_mean) / local_std
    percentile = norm.cdf(z)

    # Zero out nodata cells so they don't appear in tail masks
    percentile[nodata_mask] = 0.0
    return percentile.astype(np.float32)


# ===============================================================================
# Main entry point
# ===============================================================================

def compute_jacobs_masks(cost_distance_path, dem_path, osm_features, nhd_features,
                          output_path=None):
    """Compute the four Jacobs attractor masks and write to a 4-band GeoTIFF.

    The output raster has the same grid (CRS, transform, shape) as the
    cost-distance raster, so downstream rendering can apply the masks
    pixel-by-pixel against the cost-distance/probability surfaces without
    any reprojection.

    Band layout:
        Band 1: stream proximity (Strahler >= JACOBS_STREAM_STRAHLER_MIN, within ~80m)
        Band 2: stream-trail intersections (intersection of stream buffer
                and trail/road/powerline ROW buffer)
        Band 3: low-elevation pockets (local percentile-basis elevation < 0.1)
        Band 4: high-elevation prominence (local percentile-basis elevation > 0.9)
        Band 5: trail proximity (~40m buffer of trails/roads/powerlines)

    Each band stores 0 = mask off, 1 = mask on.

    Args:
        cost_distance_path: Path to cost-distance GeoTIFF (defines output grid)
        dem_path: Path to DEM GeoTIFF
        osm_features: Dict with 'trails', 'roads', 'powerlines' GeoDataFrames
                      (same dict structure consumed by build_cost_surface)
        nhd_features: GeoDataFrame from download_nhd_features. Must contain
                      'type' column to filter flowlines from waterbodies, and
                      'ftype' column carrying Strahler order on flowline rows.
        output_path: Optional output path for the masks GeoTIFF
    Returns:
        Path to the 5-band masks GeoTIFF.
    """
    if output_path is None:
        output_path = os.path.join(WORK_DIR, 'jacobs_masks.tif')

    print("  Computing Jacobs terrain-attractor masks...")

    # --- Step 1: Read grid reference from cost-distance raster ---
    # We anchor everything to the cost-distance grid because the rendering
    # pipeline reads cost-distance first and overlays masks on top.
    with rasterio.open(cost_distance_path) as cd_src:
        cd_transform = cd_src.transform
        cd_height = cd_src.height
        cd_width = cd_src.width
        cd_crs = cd_src.crs

    grid_shape = (cd_height, cd_width)

    # --- Step 2: Stream proximity mask (Band 1) ---
    # Jacobs's stream PDEN finding applied to flowlines with Strahler order
    # >= JACOBS_STREAM_STRAHLER_MIN (see constant definition above for the
    # Coconino-vs-Jacobs-paper rationale). The NHD download stores Strahler
    # order in the 'ftype' column for rows where 'type' == 'flowline'.
    # Capillary streams (Strahler 1-2) remain excluded because their
    # standalone PDEN signal is weak per Jacobs.
    stream_mask = np.zeros(grid_shape, dtype=bool)
    if nhd_features is not None and len(nhd_features) > 0:
        try:
            # Filter to flowlines with sufficient Strahler order. NHD's stored
            # geometry is already buffered for the cost surface (~10m for
            # order 5), but Jacobs's mask wants ~80m. We start from the
            # already-buffered geometry and expand it.
            mask_rows = nhd_features[
                (nhd_features['type'] == 'flowline')
                & (nhd_features['ftype'] >= JACOBS_STREAM_STRAHLER_MIN)
            ]
            print(f"    Stream mask: {len(mask_rows)} flowlines with Strahler >= {JACOBS_STREAM_STRAHLER_MIN}")
            if len(mask_rows) > 0:
                # The geometry in nhd_features is already buffered to a small
                # radius (see downloads.py line 518). We expand by the
                # difference to reach ~80m total.
                stream_mask = _rasterize_geometries(
                    mask_rows.geometry,
                    cd_transform,
                    grid_shape,
                    buffer_deg=JACOBS_STREAM_BUFFER_DEG,
                )
                print(f"    Stream mask cells: {int(np.sum(stream_mask))}")
        except Exception as e:
            print(f"    Stream mask computation failed: {e}")

    # --- Step 3: Trail proximity mask (Band 5) ---
    # Trails, roads, and powerline ROWs together. Jacobs's data shows
    # near-trail PDEN of ~5x (uninjured) to ~7x (injured), second only to
    # stream-trail intersections among his findings. We use a 40m buffer,
    # which is slightly wider than the cost surface's 30m corridor burn-in
    # so that pixels just off the trail centerline still get the empirical
    # find-cluster signal, even when they don't qualify as friction=1.0
    # cells in the cost surface.
    #
    # This mask is also used as input to the stream-trail intersection
    # computation (Step 4 below).
    trail_geoms = []
    for key in ('trails', 'roads', 'powerlines'):
        gdf = osm_features.get(key)
        if gdf is not None and len(gdf) > 0:
            trail_geoms.extend(list(gdf.geometry))
    trail_mask = _rasterize_geometries(
        trail_geoms,
        cd_transform,
        grid_shape,
        buffer_deg=JACOBS_TRAIL_BUFFER_DEG,
    )
    print(f"    Trail proximity cells: {int(np.sum(trail_mask))}")

    # --- Step 4: Stream-trail intersection mask (Band 2) ---
    # Jacobs's strongest single finding: 7-13x PDEN, <1% of search area.
    # The intersection mask is boolean AND of stream and trail masks. This
    # is the most concentrated empirical signal in the whole paper and
    # warrants the strongest visual treatment in the rendering layer.
    intersection_mask = stream_mask & trail_mask
    intersection_count = int(np.sum(intersection_mask))
    print(f"    Stream-trail intersection cells: {intersection_count}")

    # --- Step 5: Low and high elevation pockets (Bands 3 and 4) ---
    # Computed from a local-window elevation percentile. The window size
    # (~2km per Jacobs) is the most expensive operation in this module,
    # but uniform_filter is O(N) so it scales linearly with total raster
    # size regardless of window radius.
    low_elev_mask = np.zeros(grid_shape, dtype=bool)
    high_elev_mask = np.zeros(grid_shape, dtype=bool)
    if dem_path and os.path.exists(dem_path):
        try:
            with rasterio.open(dem_path) as dem_src:
                dem = dem_src.read(1).astype(np.float64)
                # DEM may have a different grid than cost-distance if
                # NLCD/OSM caused reprojection elsewhere. Resample to
                # the cost-distance grid if so.
                if dem.shape != grid_shape:
                    from rasterio.warp import reproject, Resampling
                    dem_resampled = np.zeros(grid_shape, dtype=np.float64)
                    reproject(
                        source=rasterio.band(dem_src, 1),
                        destination=dem_resampled,
                        src_transform=dem_src.transform,
                        src_crs=dem_src.crs,
                        dst_transform=cd_transform,
                        dst_crs=cd_crs,
                        resampling=Resampling.bilinear,
                    )
                    dem = dem_resampled

            # Standard DEM nodata handling: anything < -1000m or > 10000m is junk
            dem_nodata = np.isnan(dem) | (dem < -1000) | (dem > 10000)
            elev_percentile = _compute_local_elevation_percentile(
                dem, dem_nodata, JACOBS_ELEV_WINDOW_CELLS
            )

            low_elev_mask = (elev_percentile < JACOBS_LOW_ELEV_PERCENTILE) & (~dem_nodata)
            high_elev_mask = (elev_percentile > JACOBS_HIGH_ELEV_PERCENTILE) & (~dem_nodata)
            print(f"    Low-elevation pocket cells: {int(np.sum(low_elev_mask))}")
            print(f"    High-elevation prominence cells: {int(np.sum(high_elev_mask))}")
        except Exception as e:
            print(f"    Elevation percentile computation failed: {e}")

    # --- Step 6: Stack into 5-band GeoTIFF ---
    # uint8 storage: 0 = off, 1 = on. We use uint8 rather than bool because
    # rasterio doesn't write bool dtype natively, and uint8 is the smallest
    # type that supports a clear "is this a mask cell" read at PNG render time.
    #
    # Band 5 (trail proximity) was added after first field tests showed that
    # excluding trails from the exported masks underweighted Jacobs's
    # second-strongest finding. The cost-surface corridor highlighting
    # remains a separate visual treatment in the render endpoints.
    stacked = np.stack([
        stream_mask.astype(np.uint8),
        intersection_mask.astype(np.uint8),
        low_elev_mask.astype(np.uint8),
        high_elev_mask.astype(np.uint8),
        trail_mask.astype(np.uint8),
    ], axis=0)

    profile = {
        'driver': 'GTiff',
        'dtype': 'uint8',
        'width': cd_width,
        'height': cd_height,
        'count': 5,
        'crs': cd_crs,
        'transform': cd_transform,
        'nodata': 255,           # Reserve 255 for nodata; masks are 0 or 1
        'compress': 'lzw',
    }
    with rasterio.open(output_path, 'w', **profile) as dst:
        dst.write(stacked)
        # Annotate bands so anyone inspecting the file with `gdalinfo`
        # can see what each band represents without guessing.
        dst.set_band_description(1, f'Jacobs stream proximity (Strahler >= {JACOBS_STREAM_STRAHLER_MIN}, ~80m)')
        dst.set_band_description(2, 'Jacobs stream-trail intersection')
        dst.set_band_description(3, 'Jacobs low-elevation pocket (local percentile < 0.1)')
        dst.set_band_description(4, 'Jacobs high-elevation prominence (local percentile > 0.9)')
        dst.set_band_description(5, 'Jacobs trail proximity (~40m, all OSM linear features)')

    print(f"    Jacobs masks written to {output_path}")
    return output_path
