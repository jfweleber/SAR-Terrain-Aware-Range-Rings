# ===============================================================================
# Module:       pipeline/outputs.py
# Purpose:      Probability surfaces, POA (Probability of Area) computation,
#               TARR contour extraction, and the main analysis orchestrator.
# Author:       Jamie F. Weleber
# Created:      March 2026
# ===============================================================================

import numpy as np
import rasterio
import os
import math
from shapely.geometry import shape

from pipeline.shared import WORK_DIR, repair_geometry
from pipeline.shared import get_bbox_from_ipp, get_bbox_from_segments
from pipeline.downloads import download_dem, download_nlcd, download_osm_features, download_nhd_features
from pipeline.cost_surface import build_cost_surface
from pipeline.cost_distance import compute_cost_distance


# ===============================================================================
# STEP 1: Probability surface generation
# ===============================================================================

def generate_probability_surface(cost_distance_path, pct_25_km, pct_50_km, pct_75_km, output_path=None):
    if output_path is None:
        output_path = os.path.join(WORK_DIR, 'probability.tif')
    p25 = pct_25_km * 1000
    p50 = pct_50_km * 1000
    p75 = pct_75_km * 1000
    with rasterio.open(cost_distance_path) as src:
        dist = src.read(1).astype(np.float64)
        profile = src.profile.copy()
    nodata_mask = (dist == -9999) | (dist < 0)
    prob = np.zeros_like(dist, dtype=np.float32)
    prob[dist <= p25] = 4.0
    prob[(dist > p25) & (dist <= p50)] = 3.0
    prob[(dist > p50) & (dist <= p75)] = 2.0
    prob[dist > p75] = 1.0
    prob[nodata_mask] = 0.0
    profile['dtype'] = 'float32'
    profile['nodata'] = 0
    with rasterio.open(output_path, 'w', **profile) as dst:
        dst.write(prob, 1)
    print(f"  Probability surface written.")
    return output_path

def compute_segment_poa(cost_distance_path, segments_geojson, pct_25_km, pct_50_km, pct_75_km):
    """Compute Probability of Area (POA) for each segment using log-normal distribution.
    
    Fits a log-normal distribution to the user's percentile inputs, evaluates
    the PDF at every cell's cost-distance value, then sums density within
    each segment polygon to compute POA.
    """
    from scipy.stats import lognorm
    from rasterstats import zonal_stats
    
    # Convert km to cost-distance meters
    p25 = pct_25_km * 1000
    p50 = pct_50_km * 1000
    p75 = pct_75_km * 1000
    
    # Fit log-normal parameters from percentiles
    # For log-normal: median = exp(mu), so mu = ln(median)
    mu = math.log(p50)
    # sigma from IQR: sigma = (ln(p75) - ln(p25)) / (2 * 0.6745)
    sigma = (math.log(p75) - math.log(p25)) / (2 * 0.6745)
    
    print(f"  Log-normal fit: mu={mu:.4f}, sigma={sigma:.4f}")
    print(f"  Expected median cost-distance: {math.exp(mu):.0f}m")
    
    # Read cost-distance raster
    with rasterio.open(cost_distance_path) as src:
        cd = src.read(1).astype(np.float64)
        transform = src.transform
        crs = src.crs
        height, width = cd.shape
    
    # Create probability density surface
    nodata_mask = (cd <= 0) | (cd == -9999) | np.isinf(cd) | np.isnan(cd)
    cd_clean = np.where(nodata_mask, 1.0, cd)  # avoid log(0)
    
    # Log-normal PDF: f(x) = (1/(x*sigma*sqrt(2pi))) * exp(-(ln(x)-mu)^2 / (2*sigma^2))
    log_cd = np.log(cd_clean)
    density = (1.0 / (cd_clean * sigma * np.sqrt(2 * np.pi))) * np.exp(-((log_cd - mu)**2) / (2 * sigma**2))
    density[nodata_mask] = 0.0
    
    total_density = float(np.sum(density))
    if total_density == 0:
        print("  Warning: total density is zero")
        return []
    
    print(f"  Total probability density sum: {total_density:.2f}")
    
    # Write density as temporary raster for zonal stats
    density_path = os.path.join(WORK_DIR, 'density.tif')
    profile = {'driver': 'GTiff', 'dtype': 'float64', 'width': width, 'height': height,
               'count': 1, 'crs': crs, 'transform': transform, 'nodata': 0}
    with rasterio.open(density_path, 'w', **profile) as dst:
        dst.write(density, 1)
    
    # Calculate raw density sums for each segment
    results = []
    features = segments_geojson.get('features', [])
    
    for i, feature in enumerate(features):
        title = feature.get('properties', {}).get('title', f'Segment {i+1}')
        number = feature.get('properties', {}).get('number', '')
        res_type = feature.get('properties', {}).get('resourceType', 'GROUND')
        
        try:
            # Repair geometry if invalid
            from shapely.geometry import mapping as geom_mapping
            geom = shape(feature['geometry'])
            geom = repair_geometry(geom)
            feature = dict(feature)
            feature['geometry'] = geom_mapping(geom)
            stats = zonal_stats(
                feature,
                density_path,
                stats=['sum', 'count'],
                nodata=0
            )
            
            if stats and stats[0]['sum'] is not None:
                seg_density = float(stats[0]['sum'])
                seg_cells = int(stats[0]['count'])
            else:
                seg_density = 0
                seg_cells = 0
            
            results.append({
                'title': title,
                'number': number,
                'resource_type': res_type,
                'cells': seg_cells,
                'density_sum': round(seg_density, 4),
                'index': i
            })
            
        except Exception as e:
            print(f"    Error computing POA for {title}: {e}")
            results.append({
                'title': title,
                'number': number,
                'resource_type': res_type,
                'cells': 0,
                'density_sum': 0,
                'index': i
            })
    
    # Normalize POA across segments (not full raster) so values sum to 100%
    # This ensures buffer/radius size does not affect POA rankings and
    # separates the physics-based spatial model from ROW considerations
    segment_density_total = sum(r['density_sum'] for r in results)
    
    if segment_density_total > 0:
        for r in results:
            r['poa'] = round((r['density_sum'] / segment_density_total) * 100.0, 2)
            print(f"    {r['title']}: POA={r['poa']:.2f}%, cells={r['cells']}")
    else:
        print("  Warning: total segment density is zero")
        for r in results:
            r['poa'] = 0.0
    
    print(f"  Segment density total: {segment_density_total:.2f} (of {total_density:.2f} raster total)")
    
    # Sort by POA descending
    results.sort(key=lambda x: x['poa'], reverse=True)
    
    # Calculate cumulative POA
    cumulative = 0
    for r in results:
        cumulative += r['poa']
        r['cumulative_poa'] = round(cumulative, 2)
    
    return results

def round_coords(geom_dict, precision=5):
    """Round all coordinates in a GeoJSON geometry dict to the given precision.
    
    At 30m source resolution, 5 decimal places (~1.1m) captures all meaningful
    spatial information without false precision. This also significantly reduces
    GeoJSON file size and CalTopo URL payload length.
    """
    def _round(coords):
        if isinstance(coords[0], (list, tuple)):
            return [_round(c) for c in coords]
        return [round(v, precision) for v in coords]
    result = dict(geom_dict)
    result['coordinates'] = _round(geom_dict['coordinates'])
    return result


def extract_contour_polygons(cost_distance_path, pct_25_km, pct_50_km, pct_75_km):
    """Extract percentile contour boundaries as GeoJSON polygons.
    
    Returns polygons for the 25th, 50th, and 75th percentile zones
    suitable for rendering as vector layers or exporting to CalTopo.
    """
    from rasterio.features import shapes
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union
    
    p25 = pct_25_km * 1000
    p50 = pct_50_km * 1000
    p75 = pct_75_km * 1000
    
    with rasterio.open(cost_distance_path) as src:
        cd = src.read(1).astype(np.float64)
        transform = src.transform
    
    nodata_mask = (cd <= 0) | (cd == -9999) | np.isinf(cd) | np.isnan(cd)
    
    contours = []
    for threshold, label, color in [(p25, '25%', '#ffffff'), (p50, '50%', '#ffca00'), (p75, '75%', '#ff6a1a')]:
        binary = np.zeros_like(cd, dtype=np.uint8)
        binary[(cd <= threshold) & (~nodata_mask)] = 1
        
        try:
            polys = []
            for geom, val in shapes(binary, transform=transform):
                if val == 1:
                    poly = shape(geom)
                    if poly.is_valid and not poly.is_empty:
                        polys.append(poly)
            
            if polys:
                merged = unary_union(polys)
                if merged.geom_type == 'MultiPolygon':
                    largest = max(merged.geoms, key=lambda g: g.area)
                else:
                    largest = merged
                
                largest = repair_geometry(largest)
                # Smooth the polygon: buffer out then back in to round jagged edges
                try:
                    smoothed = largest.buffer(0.001).buffer(-0.0008)
                    smoothed = repair_geometry(smoothed)
                    if smoothed is None or smoothed.is_empty:
                        smoothed = largest
                except Exception:
                    smoothed = largest
                # Then simplify to reduce coordinate count
                simplified = smoothed.simplify(0.0001, preserve_topology=True)
                if simplified.is_empty or not simplified.is_valid:
                    simplified = largest.simplify(0.0001, preserve_topology=True)
                
                # Get centroid for label placement
                centroid = simplified.centroid
                
                contours.append({
                    'type': 'Feature',
                    'properties': {
                        'percentile': label,
                        'threshold_m': threshold,
                        'color': color,
                        'label_lat': round(centroid.y, 5),
                        'label_lng': round(centroid.x, 5),
                    },
                    'geometry': round_coords(mapping(simplified))
                })
                print(f"    {label} contour: {len(simplified.exterior.coords)} vertices")
            else:
                print(f"    {label} contour: no polygons found")
        except Exception as e:
            print(f"    {label} contour error: {e}")
    
    return {'type': 'FeatureCollection', 'features': contours}

def run_analysis(ipp_lat, ipp_lng, pct_25_km, pct_50_km, pct_75_km,
                 mode='ipp', radius_km=5.0, buffer_km=2.0, segments_geojson=None):
    print("=" * 60)
    print("WiSAR Analysis Pipeline")
    print("=" * 60)
    print("\n[1/7] Computing bounding box...")
    if mode == 'caltopo' and segments_geojson:
        # Union of two extents to ensure full coverage:
        #   1. Segment extent + 1 km (covers all search segments)
        #   2. IPP + calibrated p75 + 1 km (covers full TARR reach)
        seg_bbox = get_bbox_from_segments(segments_geojson, buffer_km)
        ipp_radius_km = pct_75_km + 1.0
        ipp_bbox = get_bbox_from_ipp(ipp_lat, ipp_lng, ipp_radius_km)
        bbox = (min(seg_bbox[0], ipp_bbox[0]), min(seg_bbox[1], ipp_bbox[1]),
                max(seg_bbox[2], ipp_bbox[2]), max(seg_bbox[3], ipp_bbox[3]))
        print(f"  Segment bbox: W={seg_bbox[0]:.4f}, S={seg_bbox[1]:.4f}, E={seg_bbox[2]:.4f}, N={seg_bbox[3]:.4f}")
        print(f"  IPP+p75 bbox: W={ipp_bbox[0]:.4f}, S={ipp_bbox[1]:.4f}, E={ipp_bbox[2]:.4f}, N={ipp_bbox[3]:.4f}")
    else:
        bbox = get_bbox_from_ipp(ipp_lat, ipp_lng, radius_km)
    print(f"  Bbox: W={bbox[0]:.4f}, S={bbox[1]:.4f}, E={bbox[2]:.4f}, N={bbox[3]:.4f}")
    print("\n[2/7] Downloading DEM...")
    dem_path = download_dem(bbox)
    print("\n[3/7] Downloading NLCD...")
    nlcd_path = download_nlcd(bbox)
    print("\n[4/7] Downloading OSM features...")
    osm_features = download_osm_features(bbox)
    print("\n[5/7] Downloading NHD hydrology...")
    nhd_features = download_nhd_features(bbox)
    
    print("\n[6/7] Building cost surface...")
    cost_path = build_cost_surface(dem_path, nlcd_path, osm_features, nhd_features=nhd_features)
    print("\n[7/8] Computing cost-distance...")
    cd_path = compute_cost_distance(cost_path, ipp_lat, ipp_lng, dem_path)
    if pct_25_km > 0 and pct_50_km > 0 and pct_75_km > 0:
        print("\n[8/8] Generating probability surface...")
        prob_path = generate_probability_surface(cd_path, pct_25_km, pct_50_km, pct_75_km)
    else:
        print("\n[8/8] Skipping probability surface (no percentiles provided).")
        prob_path = None
    print("\nAnalysis complete.")
    # Extract contour polygons as GeoJSON if percentiles provided
    contour_geojson = None
    if pct_25_km > 0 and pct_50_km > 0 and pct_75_km > 0:
        print("\n[8] Extracting contour polygons...")
        contour_geojson = extract_contour_polygons(cd_path, pct_25_km, pct_50_km, pct_75_km)
    
    # Compute segment POA if we have both segments and percentiles
    poa_results = []
    if segments_geojson and pct_25_km > 0 and pct_50_km > 0 and pct_75_km > 0:
        print("\n[9] Computing segment POA rankings...")
        poa_results = compute_segment_poa(cd_path, segments_geojson, pct_25_km, pct_50_km, pct_75_km)
    
    return {
        'bbox': bbox, 'dem_path': dem_path, 'nlcd_path': nlcd_path,
        'cost_surface_path': cost_path, 'cost_distance_path': cd_path,
        'probability_path': prob_path, 'work_dir': WORK_DIR,
        'poa_results': poa_results,
        'contour_geojson': contour_geojson,
    }
