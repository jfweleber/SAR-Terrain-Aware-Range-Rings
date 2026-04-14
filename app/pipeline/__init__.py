# ===============================================================================
# Package:      pipeline
# Purpose:      WiSAR (Wilderness Search and Rescue) Analysis Pipeline
#               Downloads geospatial data, builds an anisotropic cost-distance
#               surface, and generates probability surfaces and TARR contours.
# Author:       Jamie F. Weleber
# Created:      March 2026
# Affiliation:  Coconino County SAR / Graduate Research
#
# Structure:
#   pipeline/
#   ├── __init__.py          This file — public API re-exports
#   ├── shared.py            Shared constants, utilities, bbox functions
#   ├── downloads.py         Data acquisition (DEM, NLCD, OSM, NHD)
#   ├── cost_surface.py      Friction surface construction and slope analysis
#   ├── cost_distance.py     Anisotropic Dijkstra cost-distance computation
#   └── outputs.py           Probability surfaces, POA, TARR contour extraction
#
# Usage:
#   from pipeline import run_analysis
#   results = run_analysis(lat, lng, p25, p50, p75, radius_km=8.0)
# ===============================================================================

# Re-export shared constants and utilities so existing code that does
# "from pipeline import WORK_DIR" or "pipeline.repair_geometry()" still works
from pipeline.shared import WORK_DIR, NLCD_IMPEDANCE, repair_geometry
from pipeline.shared import get_bbox_from_ipp, get_bbox_from_segments

# Re-export all public functions from submodules
from pipeline.downloads import download_dem, download_nlcd, download_osm_features, download_nhd_features
from pipeline.cost_surface import build_cost_surface, compute_slope, tobler_pace
from pipeline.cost_distance import compute_cost_distance
from pipeline.outputs import generate_probability_surface, compute_segment_poa, extract_contour_polygons, run_analysis
