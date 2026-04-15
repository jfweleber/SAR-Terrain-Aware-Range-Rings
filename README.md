# WiSAR Decision Support Tool

**Terrain-Aware Range Rings (TARRs) for Wilderness Search and Rescue**

A web-based spatial analysis tool that generates anisotropic probability surfaces for SAR operations. Instead of drawing simple Euclidean distance rings around an Initial Planning Point (IPP), TARRs trace contours of equal travel cost across real terrain, compressing against steep slopes, dense forest, and water barriers, while compressing less along trails and valleys where a person can move easily.

**Live site:** [https://sar.weleber.net](https://sar.weleber.net)

![TARR Example — San Francisco Peaks](app/tarr_example.png)
---

## What this tool does

Given an IPP (the point where a lost person was last seen) and a subject profile (hiker, child, dementia patient, etc.), the tool:

1. **Downloads geospatial data** — elevation (USGS 3DEP), land cover (NLCD 2021), trails, roads, and power lines (OpenStreetMap), and hydrology (NHD)
2. **Builds a friction surface** — each 30m cell gets a cost multiplier based on land cover type, calibrated to off-trail speed literature (Imhof 1950). Trails, roads, and power line corridors are burned in at friction 1.0; water features from NHD and OSM act as high-impedance barriers.
3. **Computes anisotropic cost-distance** — Dijkstra's algorithm with per-edge Tobler's Hiking Function, cross-slope penalty, and 3D surface distance
4. **Applies per-band calibration** — Coconino County calibration multipliers (M25, M50, M75) scale each percentile threshold independently to correct the nonlinear contraction of TARRs in rugged terrain
5. **Generates probability contours (TARRs)** — Lost Person Behavior percentiles (Koester 2008) applied to the cost-distance surface to produce terrain-aware search area boundaries
6. **Ranks search segments by POA** — log-normal probability density integrated within CalTopo search segment polygons

## Key features

- **Two analysis modes:** IPP Only (single point + radius) and CalTopo Import (segments + auto-detected IPP)
- **28 subject categories** from Lost Person Behavior (Koester 2008) with eco-region and terrain selectors
- **Per-band calibration** — profile-specific multipliers at each percentile threshold, validated against 360 historical subjects from 253 Coconino County missions
- **CalTopo two-way integration** — import segments for ranking, export TARR contours and POA rankings back to CalTopo maps via the Team API
- **Probability density visualization** with percentile-band color ramp and travel corridor highlighting
- **KML and GeoJSON export** of TARR contours for CalTopo, Google Earth, TAK/CloudTAK, QGIS, and Avenza
- **GeoTIFF downloads** of cost-distance, cost surface, and probability rasters
- **Download caching** — DEM, NLCD, OSM, and NHD data cached by bounding box to avoid redundant API calls
- **Overpass API fallback** — five mirror endpoints tried in sequence for reliable OSM data retrieval

## Calibration

Applying Euclidean-derived find-distance statistics (Koester 2008) as cost-distance thresholds systematically contracts TARR contours because terrain friction inflates effective travel distance. The contraction is nonlinear — outer contours are more compressed than inner ones as friction accumulates over longer paths.

The tool corrects this with per-band multipliers derived from Coconino County historical data:

| Percentile | Uncalibrated | Calibrated | Target |
|-----------|-------------|-----------|--------|
| 25th | 23.9% | 26.2% | 25.0% |
| 50th | 41.1% | 50.0% | 50.0% |
| 75th | 58.9% | 77.1% | 75.0% |

Seven profiles with n≥20 subjects have profile-specific multipliers; remaining profiles use global defaults (M25=1.05, M50=1.35, M75=1.80). Full validation details are available in the tool's Validation modal.

## Architecture

```
app/
├── server.py              Flask web server, API endpoints, PNG renderers
├── pipeline/              Analysis pipeline (modular package)
│   ├── __init__.py        Public API re-exports
│   ├── shared.py          Constants, utilities, bbox functions
│   ├── downloads.py       Data acquisition (DEM, NLCD, OSM, NHD) with caching
│   ├── cost_surface.py    Friction surface construction
│   ├── cost_distance.py   Dijkstra anisotropic cost-distance
│   └── outputs.py         Probability, POA, TARR contour extraction
└── static/
    ├── index.html         Single-page Leaflet.js frontend, modals, accordion UI
    └── app.js             Application logic, calibration, CalTopo integration
```

## Data sources

| Data | Source | Resolution |
|------|--------|-----------|
| Elevation | USGS 3DEP (1/3 arc-second) | 30m |
| Land cover | NLCD 2021 | 30m |
| Trails, roads, power lines | OpenStreetMap (Overpass API, 5 mirror endpoints) | Vector |
| Hydrology | NHD (USGS MapServer) — waterbodies, area features, flowlines | Vector |
| Subject profiles | Koester (2008), via Ferguson (2013) IGT4SAR | Statistical |
| Calibration | Coconino County Sheriff's Office (360 subjects, 253 missions) | Per-profile |

## Methodology

The cost-distance computation combines four factors per cell transition:

- **Tobler's Hiking Function** (directional slope cost)
- **Land cover friction** (calibrated to Imhof 1950 off-trail speed reduction)
- **Cross-slope penalty** (lateral traversal difficulty)
- **3D surface distance** (Pythagorean with elevation change)

Friction multipliers range from 1.0 (trail/road/power line corridor) to 1.80 (evergreen forest) to 50.0 (water barrier). Power line rights-of-way are buffered at ~40m to represent cleared corridors. The full friction table and methodology are documented in the tool's Metadata modal.

Calibration multipliers are applied on the frontend before percentile distances are sent to the analysis pipeline. Each percentile (p25, p50, p75) receives its own multiplier, correcting the nonlinear contraction where terrain friction accumulates more over longer travel paths. The cost surface and cost-distance computation are unaffected — calibration adjusts only the statistical thresholds, not the terrain model.

## Tech stack

- **Backend:** Python 3.12, Flask, Gunicorn, Nginx
- **Frontend:** Leaflet.js, vanilla JavaScript (single-page app)
- **Geospatial:** rasterio, GDAL, shapely, geopandas, scipy, rasterstats
- **Server:** Ubuntu 24.04 on Linode (4GB RAM, 5 Gunicorn workers)

## References

- Danser, R.A. (2018). *Applying Least Cost Path Analysis to SAR Data.* USC Thesis.
- Doherty, P.J., Guo, Q., Doke, J., & Ferguson, D. (2014). Applied Geography, 47, 99-110.
- Ferguson, D. (2013). IGT4SAR. GitHub.
- Imhof, E. (1950). *Gelände und Karte.* Eugen Rentsch Verlag.
- Koester, R.J. (2008). *Lost Person Behavior.* dbS Productions.
- Tobler, W. (1993). Technical Report 93-1, NCGIA.

## Author

**Jamie F. Weleber**
Coconino County Sheriff's Search & Rescue

## License

AGPL-3.0 — see [LICENSE](LICENSE) for details.
