# ===============================================================================
# Module:       pipeline/cost_distance.py
# Purpose:      Anisotropic cost-distance computation using Dijkstra's algorithm.
#               Computes the cumulative travel cost from the IPP to every cell,
#               accounting for slope (Tobler), land cover friction, cross-slope
#               penalty, and 3D surface distance.
# Author:       Jamie F. Weleber
# Created:      March 2026
# ===============================================================================

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
import os
import math
import heapq

from pipeline.shared import WORK_DIR


def compute_cost_distance(cost_surface_path, ipp_lat, ipp_lng, dem_path, output_path=None):
    """Compute anisotropic cost-distance from the IPP using Dijkstra's algorithm.

    This is the computational core of the TARR system. Unlike Euclidean distance
    rings, cost-distance accounts for terrain by computing cumulative "effort"
    from the IPP to every cell.

    "Anisotropic" means direction matters — uphill costs more than downhill on
    the same slope. Edge cost combines four factors:
      1. Tobler pace: speed reduction from slope (directional)
      2. Cross-slope penalty: traversing steep terrain laterally
      3. Friction: land cover impedance (trail=1.0, forest=1.8, etc.)
      4. Surface distance: actual 3D ground distance

    Dijkstra's algorithm guarantees the TRUE least-cost path from the IPP
    to every reachable cell.

    Args:
        cost_surface_path: Path to the friction raster GeoTIFF
        ipp_lat, ipp_lng: IPP coordinates in decimal degrees
        dem_path: Path to the DEM GeoTIFF
        output_path: Optional path for the output cost-distance GeoTIFF
    Returns:
        Path to the cost-distance GeoTIFF
    """
    if output_path is None:
        output_path = os.path.join(WORK_DIR, 'cost_distance.tif')

    with rasterio.open(cost_surface_path) as src:
        friction = src.read(1).astype(np.float64)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata
        height, width = friction.shape
    with rasterio.open(dem_path) as dem_src:
        dem_raw = dem_src.read(1).astype(np.float64)
        # Bilinear resampling for continuous elevation data
        if dem_raw.shape != friction.shape:
            dem_resampled = np.zeros((height, width), dtype=np.float64)
            reproject(source=rasterio.band(dem_src, 1), destination=dem_resampled,
                src_transform=dem_src.transform, src_crs=dem_src.crs,
                dst_transform=transform, dst_crs=crs, resampling=Resampling.bilinear)
            dem = dem_resampled
        else:
            dem = dem_raw

    dem[dem < -1000] = np.nan
    dem[dem > 10000] = np.nan

    # Inverse affine transform: geographic -> pixel coordinates
    # Note: returns (col, row), not (row, col)
    col, row = ~transform * (ipp_lng, ipp_lat)
    row, col = int(round(row)), int(round(col))
    if row < 0 or row >= height or col < 0 or col >= width:
        raise ValueError(f"IPP falls outside raster extent")
    print(f"  IPP pixel: row={row}, col={col}")
    print(f"  Running ANISOTROPIC cost-distance on {width}x{height} grid...")

    # Convert cell sizes from degrees to meters
    center_lat = (transform[5] + transform[5] + transform[4] * height) / 2
    cell_x_m = abs(transform[0]) * 111320 * math.cos(math.radians(center_lat))
    cell_y_m = abs(transform[4]) * 110540
    diag_m = math.sqrt(cell_x_m**2 + cell_y_m**2)

    friction[friction <= 0] = 50.0
    friction[np.isnan(friction)] = 50.0
    if nodata is not None:
        friction[friction == nodata] = 50.0

    # Flat-ground Tobler speed for normalization (~5.04 km/h)
    flat_speed = 6.0 * math.exp(-3.5 * abs(0.05))

    # Initialize Dijkstra
    INF = float('inf')
    dist = np.full((height, width), INF, dtype=np.float64)
    dist[row, col] = 0.0
    visited = np.zeros((height, width), dtype=bool)
    pq = [(0.0, row, col)]

    # 8-connected neighbors: (drow, dcol, horizontal_distance_m)
    neighbors = [
        (-1,-1,diag_m),(-1,0,cell_y_m),(-1,1,diag_m),
        (0,-1,cell_x_m),(0,1,cell_x_m),
        (1,-1,diag_m),(1,0,cell_y_m),(1,1,diag_m)]

    iterations = 0
    while pq:
        d, r, c = heapq.heappop(pq)
        if visited[r, c]:
            continue
        visited[r, c] = True
        iterations += 1
        if iterations % 500000 == 0:
            print(f"    {iterations} cells processed...")
        elev_here = dem[r, c]
        for dr, dc, horiz_dist in neighbors:
            nr, nc = r + dr, c + dc
            if 0 <= nr < height and 0 <= nc < width and not visited[nr, nc]:
                elev_there = dem[nr, nc]

                # Directional slope (rise/run) — positive = uphill
                if np.isnan(elev_here) or np.isnan(elev_there):
                    dh_dx = 0.0
                else:
                    dh_dx = (elev_there - elev_here) / horiz_dist

                # 3D surface distance
                elev_diff = 0.0 if (np.isnan(elev_here) or np.isnan(elev_there)) else (elev_there - elev_here)
                surface_dist = math.sqrt(horiz_dist**2 + elev_diff**2)

                # Tobler hiking function
                speed = 6.0 * math.exp(-3.5 * abs(dh_dx + 0.05))
                tobler_factor = flat_speed / speed

                # Cross-slope penalty
                slope_mag = abs(elev_diff) / horiz_dist if horiz_dist > 0 else 0
                cross_slope_factor = 1.0 + 0.3 * min(slope_mag, 1.0)

                # Land cover friction (average of current and neighbor)
                avg_friction = (friction[r, c] + friction[nr, nc]) / 2.0

                # Total edge cost
                edge_cost = tobler_factor * cross_slope_factor * avg_friction * surface_dist
                new_dist = d + edge_cost
                if new_dist < dist[nr, nc]:
                    dist[nr, nc] = new_dist
                    heapq.heappush(pq, (new_dist, nr, nc))

    print(f"  Anisotropic cost-distance complete. {iterations} cells.")

    profile = {'driver':'GTiff','dtype':'float32','width':width,'height':height,
               'count':1,'crs':crs,'transform':transform,'nodata':-9999}
    dist_out = dist.astype(np.float32)
    dist_out[dist_out == INF] = -9999
    with rasterio.open(output_path, 'w', **profile) as dst:
        dst.write(dist_out, 1)
    print(f"  Cost-distance written.")
    return output_path
