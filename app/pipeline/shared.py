# ===============================================================================
# Module:       pipeline/shared.py
# Purpose:      Shared constants, configuration, and utility functions used
#               across all pipeline submodules. This file exists to prevent
#               circular imports — submodules import from here instead of
#               from pipeline/__init__.py.
# Author:       Jamie F. Weleber
# Created:      March 2026
# ===============================================================================

import tempfile

# All intermediate files (DEM, NLCD, cost surface, etc.) are written to a
# temporary directory that is unique per pipeline run. This prevents
# collisions if multiple analyses run concurrently on the server.
WORK_DIR = tempfile.mkdtemp(prefix='wisar_')

# --- NLCD land cover impedance lookup table ---
# Maps National Land Cover Database (NLCD) class codes to impedance values
# on a 0-100 scale, following the IGT4SAR framework (Doherty et al. 2013,
# Danser 2018). 0 = no impedance (paved road), 99 = impassable (deep water).
NLCD_IMPEDANCE = {
    11:99, 12:85, 21:5, 22:10, 23:15, 24:20, 31:30, 32:40,
    41:45, 42:50, 43:35, 51:45, 52:45, 71:20, 72:45, 73:20,
    74:25, 81:25, 82:30, 90:80, 91:80, 92:80, 93:80, 94:80,
    95:80, 96:80, 97:80, 98:99, 99:99,
}


def repair_geometry(geom):
    """Repair invalid geometry using progressive strategies.

    Vector geometries from external sources (CalTopo, NHD, OSM) often have
    self-intersections or topology errors. This function tries increasingly
    aggressive repair strategies until the geometry is valid.

    Args:
        geom: A Shapely geometry object
    Returns:
        A valid Shapely geometry, or the original if repair fails
    """
    from shapely.validation import make_valid
    if geom is None or geom.is_empty:
        return geom
    if not geom.is_valid:
        try:
            geom = make_valid(geom)
        except Exception:
            try:
                geom = geom.buffer(0)
            except Exception:
                pass
    if geom.geom_type == 'MultiPolygon':
        geom = max(geom.geoms, key=lambda g: g.area)
    if geom.geom_type == 'GeometryCollection':
        polys = [g for g in geom.geoms if g.geom_type in ('Polygon', 'MultiPolygon')]
        if polys:
            geom = max(polys, key=lambda g: g.area)
    return geom


import math

def get_bbox_from_ipp(lat, lng, radius_km):
    """Compute a geographic bounding box centered on the IPP.

    The cosine correction ensures the bbox is square in meters, not just
    in degrees (longitude degrees shrink toward the poles).

    Args:
        lat, lng: IPP coordinates in decimal degrees (WGS84)
        radius_km: Half-width of the bounding box in kilometers
    Returns:
        Tuple of (west, south, east, north) in decimal degrees
    """
    km_per_deg_lat = 111.32
    km_per_deg_lng = 111.32 * math.cos(math.radians(lat))
    dlat = radius_km / km_per_deg_lat
    dlng = radius_km / km_per_deg_lng
    return (lng - dlng, lat - dlat, lng + dlng, lat + dlat)


def get_bbox_from_segments(segments_geojson, buffer_km):
    """Compute a bounding box from CalTopo search segment geometries.

    Args:
        segments_geojson: GeoJSON FeatureCollection with segment polygons
        buffer_km: Buffer distance around segments in kilometers
    Returns:
        Tuple of (west, south, east, north) in decimal degrees
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union

    geometries = []
    for feature in segments_geojson.get('features', []):
        try:
            geom = shape(feature['geometry'])
            geom = repair_geometry(geom)
            if geom and not geom.is_empty and geom.is_valid:
                geometries.append(geom)
        except Exception:
            continue
    if not geometries:
        raise ValueError("No valid segment geometries found")
    try:
        union = unary_union(geometries)
    except Exception:
        all_bounds = [g.bounds for g in geometries]
        min_x = min(b[0] for b in all_bounds)
        min_y = min(b[1] for b in all_bounds)
        max_x = max(b[2] for b in all_bounds)
        max_y = max(b[3] for b in all_bounds)
        from shapely.geometry import box
        union = box(min_x, min_y, max_x, max_y)
    bounds = union.bounds
    center_lat = (bounds[1] + bounds[3]) / 2
    km_per_deg_lat = 111.32
    km_per_deg_lng = 111.32 * math.cos(math.radians(center_lat))
    dlat = buffer_km / km_per_deg_lat
    dlng = buffer_km / km_per_deg_lng
    max_extent_deg = 50 / km_per_deg_lat
    width = (bounds[2] - bounds[0]) + 2 * dlng
    height = (bounds[3] - bounds[1]) + 2 * dlat
    if width > max_extent_deg or height > max_extent_deg:
        dlat = min(dlat, (max_extent_deg - (bounds[3] - bounds[1])) / 2)
        dlng = min(dlng, (max_extent_deg - (bounds[2] - bounds[0])) / 2)
    return (bounds[0] - dlng, bounds[1] - dlat, bounds[2] + dlng, bounds[3] + dlat)
