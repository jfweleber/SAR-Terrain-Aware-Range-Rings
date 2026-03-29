# ===============================================================================
# Module:       pipeline/cost_surface.py
# Purpose:      Friction surface construction from land cover, trails, and
#               hydrology data. Converts raw impedance values to calibrated
#               friction multipliers based on off-trail speed literature.
# Author:       Jamie F. Weleber
# Created:      March 2026
# ===============================================================================

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.features import rasterize
from scipy.signal import convolve2d
import os
import math

from pipeline.shared import WORK_DIR, NLCD_IMPEDANCE, repair_geometry


# ===============================================================================
# STEP 1: Slope and terrain analysis utilities
# ===============================================================================

def tobler_pace(slope_degrees):
    """Compute the Tobler hiking function pace factor for a given slope.

    Tobler's Hiking Function (Tobler 1993) estimates walking speed as a
    function of terrain slope:  speed = 6 * exp(-3.5 * |slope + 0.05|) km/h

    The +0.05 offset shifts peak speed to slight downhill (~2.86 degrees),
    reflecting the biomechanics of walking. We return the inverse (pace)
    as a multiplier: 1.0 = flat trail speed, >1.0 = slower than flat.

    Args:
        slope_degrees: Terrain slope in degrees (can be a numpy array)
    Returns:
        Pace factor (multiplier relative to flat ground speed)
    """
    slope_rad = np.radians(slope_degrees)
    dh_dx = np.tan(slope_rad)
    speed = 6.0 * np.exp(-3.5 * np.abs(dh_dx + 0.05))
    flat_speed = 6.0 * np.exp(-3.5 * np.abs(0.05))
    pace = flat_speed / speed
    return np.clip(pace, 1.0, 50.0)


def compute_slope(dem_path):
    """Compute terrain slope from a DEM using Horn's method (Sobel operator).

    Horn's method computes slope from a 3x3 moving window, weighting the
    cardinal neighbors (N/S/E/W) more heavily than diagonals. This matches
    how ArcGIS Pro's Slope tool works internally.

    Args:
        dem_path: Path to the DEM GeoTIFF
    Returns:
        2D numpy array of slope values in degrees
    """
    with rasterio.open(dem_path) as src:
        dem = src.read(1).astype(np.float64)
        transform = src.transform
    center_lat = (src.bounds.top + src.bounds.bottom) / 2
    cellsize_x = abs(transform[0]) * 111320 * math.cos(math.radians(center_lat))
    cellsize_y = abs(transform[4]) * 110540
    dem[dem < -1000] = np.nan
    dem[dem > 10000] = np.nan
    # Sobel kernels for partial derivatives (Horn's method)
    kx = np.array([[-1,0,1],[-2,0,2],[-1,0,1]]) / (8.0 * cellsize_x)
    ky = np.array([[-1,-2,-1],[0,0,0],[1,2,1]]) / (8.0 * cellsize_y)
    dzdx = convolve2d(dem, kx, mode='same', boundary='symm')
    dzdy = convolve2d(dem, ky, mode='same', boundary='symm')
    return np.degrees(np.arctan(np.sqrt(dzdx**2 + dzdy**2)))


# ===============================================================================
# STEP 2: Build the cost surface
# ===============================================================================

def build_cost_surface(dem_path, nlcd_path, osm_features, nhd_features=None, output_path=None):
    """Build a friction-based cost surface from land cover, trails, and hydrology.

    This is the core data fusion step. The build follows a hierarchical order:
      1. NLCD land cover -> impedance values
      2. NHD water features -> high impedance barriers
      3. OSM waterways -> moderate impedance
      4. OSM trails/roads -> low impedance (LAST, so trails always win)

    Trail burn-in happens last so trails get friction = 1.0 even if they cross
    through forest or over bridges. Slope is NOT baked in — it is computed
    directionally per-edge during Dijkstra cost-distance (anisotropic model).

    Friction multipliers calibrated to Imhof (1950) off-trail speed literature.

    Args:
        dem_path: Path to DEM GeoTIFF (grid dimensions/CRS reference)
        nlcd_path: Path to NLCD GeoTIFF, or None for uniform impedance
        osm_features: Dict with 'trails', 'roads', 'waterways' GeoDataFrames
        nhd_features: GeoDataFrame of NHD water polygons (optional)
        output_path: Optional output path for cost surface GeoTIFF
    Returns:
        Path to the cost surface GeoTIFF
    """
    if output_path is None:
        output_path = os.path.join(WORK_DIR, 'cost_surface.tif')
    print("  Skipping slope bake-in (anisotropic mode: Tobler computed per-edge in cost-distance)...")
    with rasterio.open(dem_path) as dem_src:
        dem_transform = dem_src.transform
        dem_crs = dem_src.crs
        dem_width = dem_src.width
        dem_height = dem_src.height

    # --- Sub-step A: Reclassify NLCD to impedance ---
    print("  Reclassifying NLCD to impedance...")
    if nlcd_path and os.path.exists(nlcd_path):
        try:
            with rasterio.open(nlcd_path) as nlcd_src:
                nlcd_resampled = np.zeros((dem_height, dem_width), dtype=np.float32)
                reproject(source=rasterio.band(nlcd_src, 1), destination=nlcd_resampled,
                    src_transform=nlcd_src.transform, src_crs=nlcd_src.crs,
                    dst_transform=dem_transform, dst_crs=dem_crs, resampling=Resampling.nearest)
            impedance = np.full_like(nlcd_resampled, 30.0)
            for nval, ival in NLCD_IMPEDANCE.items():
                impedance[nlcd_resampled == nval] = float(ival)
            from scipy.ndimage import binary_dilation
            water_mask = (nlcd_resampled == 11) | (nlcd_resampled == 12)
            if np.any(water_mask):
                water_dilated = binary_dilation(water_mask, iterations=1)
                impedance[(water_dilated) & (impedance < 90)] = 99.0
        except Exception as e:
            print(f"  NLCD reclassify failed: {e}. Using uniform.")
            impedance = np.full((dem_height, dem_width), 20.0, dtype=np.float32)
    else:
        impedance = np.full((dem_height, dem_width), 20.0, dtype=np.float32)

    # --- Sub-step B: Burn in trails and roads ---
    print("  Burning in trails and roads...")
    trail_mask = np.zeros((dem_height, dem_width), dtype=np.uint8)
    for label, gdf in [('trails', osm_features.get('trails')), ('roads', osm_features.get('roads'))]:
        if gdf is not None and len(gdf) > 0:
            try:
                buffered = gdf.geometry.buffer(0.0003)
                shapes = [(geom, 1) for geom in buffered if geom is not None and not geom.is_empty]
                if shapes:
                    burned = rasterize(shapes, out_shape=(dem_height, dem_width), transform=dem_transform, fill=0, dtype=np.uint8)
                    trail_mask = np.maximum(trail_mask, burned)
            except Exception as e:
                print(f"  Warning burning {label}: {e}")
    impedance[trail_mask == 1] = 1.0

    # --- Sub-step C: Burn in OSM waterways ---
    if osm_features.get('waterways') is not None and len(osm_features['waterways']) > 0:
        print("  Burning in waterways...")
        try:
            for _, row in osm_features['waterways'].iterrows():
                imp_val = 60 if row['type'] == 'river' else 40
                buf = row.geometry.buffer(0.0001)
                if buf and not buf.is_empty:
                    burned = rasterize([(buf, imp_val)], out_shape=(dem_height, dem_width), transform=dem_transform, fill=0, dtype=np.uint8)
                    wmask = (burned > 0) & (trail_mask == 0)
                    impedance[wmask] = np.maximum(impedance[wmask], burned[wmask].astype(np.float32))
        except Exception as e:
            print(f"  Warning burning waterways: {e}")

    # --- Sub-step D: Burn in NHD water features ---
    if nhd_features is not None and len(nhd_features) > 0:
        print("  Burning in NHD water features...")
        try:
            for _, row in nhd_features.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue
                geom = repair_geometry(geom)
                if geom is None or geom.is_empty:
                    continue
                imp_val = row.get('impedance', 99)
                try:
                    burned = rasterize(
                        [(geom, imp_val)],
                        out_shape=(dem_height, dem_width),
                        transform=dem_transform,
                        fill=0,
                        dtype=np.uint8
                    )
                    nhd_mask = (burned > 0) & (trail_mask == 0)
                    impedance[nhd_mask] = np.maximum(impedance[nhd_mask], burned[nhd_mask].astype(np.float32))
                except Exception as e:
                    pass
        except Exception as e:
            print(f"  Warning: NHD burn-in failed: {e}")

    # --- Sub-step E: Convert impedance to friction multipliers ---
    # Calibrated to Imhof (1950): off-trail velocity ~0.6x on-trail
    friction = np.where(impedance <= 1, 1.00, 1.15)
    friction = np.where((impedance > 1) & (impedance <= 5), 1.00, friction)
    friction = np.where((impedance > 5) & (impedance <= 10), 1.05, friction)
    friction = np.where((impedance > 10) & (impedance <= 15), 1.10, friction)
    friction = np.where((impedance > 15) & (impedance <= 20), 1.15, friction)
    friction = np.where((impedance > 20) & (impedance <= 25), 1.15, friction)
    friction = np.where((impedance > 25) & (impedance <= 30), 1.25, friction)
    friction = np.where((impedance > 30) & (impedance <= 35), 1.50, friction)
    friction = np.where((impedance > 35) & (impedance <= 45), 1.60, friction)
    friction = np.where((impedance > 45) & (impedance <= 55), 1.80, friction)
    friction = np.where((impedance > 55) & (impedance <= 85), 3.00, friction)
    friction = np.where(impedance > 85, 50.00, friction)
    friction = np.clip(friction, 1.00, 50.00)
    cost_surface = friction

    profile = {'driver':'GTiff','dtype':'float32','width':dem_width,'height':dem_height,
               'count':1,'crs':dem_crs,'transform':dem_transform,'nodata':-9999}
    with rasterio.open(output_path, 'w', **profile) as dst:
        cs = cost_surface.astype(np.float32)
        cs[np.isnan(cs)] = -9999
        dst.write(cs, 1)
    print(f"  Cost surface written.")
    return output_path
