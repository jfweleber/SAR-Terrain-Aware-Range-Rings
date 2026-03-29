# ===============================================================================
# Script Name:  server.py
# Purpose:      Flask web server for the WiSAR Decision Support Tool.
#               Serves the single-page frontend (index.html), proxies CalTopo
#               API requests to avoid CORS issues, runs the analysis pipeline
#               on user request, and renders map overlay PNGs from the raster
#               outputs (cost surface, terrain difficulty, percentile contours).
# Author:       Jamie F. Weleber
# Created:      March 2026
# Affiliation:  Coconino County SAR / Graduate Research
# ===============================================================================

# ===============================================================================
# IMPORTS
# ===============================================================================
from flask import Flask, jsonify, request, send_from_directory, send_file
import urllib.request           # Low-level HTTP client for CalTopo proxy
import json                     # JSON parsing for API request/response data
import os                       # File path operations
import threading                # Thread-safe access to shared analysis results
import traceback                # Stack trace formatting for error logging


# ===============================================================================
# STEP 1: Application setup and result storage
# ===============================================================================

# Flask app configuration: static files (index.html) are served from ./static/
app = Flask(__name__, static_folder='static')

# In-memory cache of analysis results, keyed by analysis_id.
# This allows fast retrieval of results for PNG rendering without
# re-reading JSON from disk on every request.
analyses = {}

# Thread lock for safe concurrent access to the analyses dict.
# Gunicorn may handle multiple requests simultaneously, so we need
# to prevent race conditions when reading/writing analysis results.
analysis_lock = threading.Lock()

# Persistent results directory — survives Gunicorn restarts (unlike the
# pipeline's temp WORK_DIR which is per-run). Analysis result metadata
# is saved here as JSON so results can be recovered after a server restart.
RESULTS_DIR = '/tmp/wisar_results'
os.makedirs(RESULTS_DIR, exist_ok=True)


def save_result(analysis_id, result):
    """Save analysis result to both disk (JSON) and in-memory cache.

    We store results in two places:
      1. JSON file on disk — survives server restarts
      2. In-memory dict — fast access for subsequent PNG render requests

    Args:
        analysis_id: Unique identifier (typically "lat_lng" of the IPP)
        result: Dict containing all pipeline output paths and metadata
    """
    path = os.path.join(RESULTS_DIR, analysis_id + '.json')
    with open(path, 'w') as f:
        json.dump(result, f)
    with analysis_lock:
        analyses[analysis_id] = result


def load_result(analysis_id):
    """Load analysis result from in-memory cache, falling back to disk.

    Checks the in-memory dict first (fast path). If not found, tries to
    load from the JSON file on disk (happens after server restart).

    Args:
        analysis_id: Unique identifier for the analysis run
    Returns:
        Result dict, or None if not found anywhere
    """
    with analysis_lock:
        if analysis_id in analyses:
            return analyses[analysis_id]
    path = os.path.join(RESULTS_DIR, analysis_id + '.json')
    if os.path.exists(path):
        with open(path, 'r') as f:
            result = json.load(f)
        with analysis_lock:
            analyses[analysis_id] = result
        return result
    return None


# ===============================================================================
# STEP 2: Static file serving and CalTopo proxy
# ===============================================================================

@app.route('/')
def index():
    """Serve the main application page (index.html) from the static folder."""
    return send_from_directory('static', 'index.html')


@app.route('/api/caltopo/<map_id>')
def get_caltopo_data(map_id):
    """Proxy endpoint for fetching CalTopo map data.

    The browser can't call CalTopo's API directly due to CORS (Cross-Origin
    Resource Sharing) restrictions — CalTopo doesn't include the necessary
    headers to allow requests from other domains. This endpoint relays the
    request server-side so the browser talks to our Flask server (same origin)
    instead of directly to CalTopo.

    We extract two things from the CalTopo response:
      1. Assignment segments — the search area polygons drawn by the planner
      2. Markers — specifically looking for an "IPP" marker for auto-detection

    Args:
        map_id: The CalTopo map identifier from the share URL
    Returns:
        JSON with segments (GeoJSON FeatureCollection), IPP if found, and counts
    """
    try:
        # CalTopo's /since/0 endpoint returns all features on the map
        url = f'https://caltopo.com/api/v1/map/{map_id}/since/0'
        req = urllib.request.Request(url, headers={'User-Agent': 'WiSAR-Decision-Support/0.1'})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode())
        # CalTopo's response structure: result.state.features[]
        features = data.get('result', {}).get('state', {}).get('features', [])
        # Filter to just Assignment features (search segments) — CalTopo also
        # returns markers, tracks, and other feature types we don't need
        segments = [f for f in features if f.get('properties', {}).get('class') == 'Assignment']
        markers = [f for f in features if f.get('properties', {}).get('class') == 'Marker']
        # Look for an IPP marker — if the planner placed one in CalTopo,
        # we can auto-detect the IPP location instead of requiring manual entry
        ipp = None
        for m in markers:
            title = (m.get('properties', {}).get('title', '') or '').strip().upper()
            if title == 'IPP':
                coords = m.get('geometry', {}).get('coordinates', [])
                if len(coords) >= 2:
                    # GeoJSON coordinates are [lng, lat] — opposite of typical (lat, lng)
                    ipp = {'lat': coords[1], 'lng': coords[0], 'source': 'caltopo'}
                break
        return jsonify({'status':'ok','segment_count':len(segments),
            'segments':{'type':'FeatureCollection','features':segments},
            'ipp':ipp,'marker_count':len(markers)})
    except urllib.error.URLError as e:
        return jsonify({'status':'error','message':f'Could not reach CalTopo: {str(e)}'}), 502
    except Exception as e:
        return jsonify({'status':'error','message':str(e)}), 500


# ===============================================================================
# STEP 3: Main analysis endpoint
# ===============================================================================

@app.route('/api/analyze', methods=['POST'])
def run_analysis_endpoint():
    """Run the WiSAR analysis pipeline on user-submitted parameters.

    This is the main API endpoint that the frontend calls when the user clicks
    "Generate Probability Surface." It receives the IPP location, percentiles,
    analysis mode, and optional CalTopo segments, then runs the full pipeline:
    data download → cost surface → cost-distance → probability → contours → POA.

    The endpoint is synchronous — it blocks until the full pipeline completes
    (typically 30-90 seconds). The frontend shows a progress animation while
    waiting for the response.

    Request body (JSON):
        ipp: {lat, lng} — IPP coordinates
        percentiles: {p25, p50, p75} — find-distance percentiles in km
        mode: 'ipp' or 'caltopo'
        radius: analysis radius in meters (IPP mode)
        buffer: segment buffer in meters (CalTopo mode)
        segments: GeoJSON FeatureCollection (CalTopo mode)

    Returns:
        JSON with analysis_id, bounds, POA rankings, contour GeoJSON, and
        URLs for map overlay PNGs
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'status':'error','message':'No JSON data provided'}), 400

        # --- Parse and validate IPP coordinates ---
        ipp = data.get('ipp', {})
        ipp_lat = float(ipp.get('lat', 0))
        ipp_lng = float(ipp.get('lng', 0))
        if ipp_lat == 0 or ipp_lng == 0:
            return jsonify({'status':'error','message':'Invalid IPP coordinates'}), 400

        # --- Parse percentiles ---
        # Percentiles come from the LPB (Lost Person Behavior) subject profile
        # selected by the user, representing find-distance statistics in km
        percentiles = data.get('percentiles', {})
        p25 = float(percentiles.get('p25', 0)) if percentiles else 0
        p50 = float(percentiles.get('p50', 0)) if percentiles else 0
        p75 = float(percentiles.get('p75', 0)) if percentiles else 0
        has_percentiles = p25 > 0 and p50 > 0 and p75 > 0

        # Validate that percentiles are monotonically increasing
        if has_percentiles and (p25 >= p50 or p50 >= p75):
            return jsonify({'status':'error','message':'Percentiles must be three increasing positive values'}), 400

        if not has_percentiles:
            # Dummy values — the pipeline still needs them for the cost-distance
            # computation but they won't produce meaningful probability output
            p25, p50, p75 = 1.0, 2.0, 3.0

        # --- Parse analysis parameters ---
        mode = data.get('mode', 'ipp')
        # Frontend sends radius/buffer in meters; pipeline expects km
        radius_km = float(data.get('radius', 5000)) / 1000
        buffer_km = float(data.get('buffer', 2000)) / 1000
        segments_geojson = data.get('segments', None)

        # --- Run the pipeline ---
        # This is a lazy import so the pipeline module is only loaded when
        # an analysis is actually requested, not on server startup
        from pipeline import run_analysis
        result = run_analysis(ipp_lat=ipp_lat, ipp_lng=ipp_lng,
            pct_25_km=p25, pct_50_km=p50, pct_75_km=p75,
            mode=mode, radius_km=radius_km, buffer_km=buffer_km,
            segments_geojson=segments_geojson)

        # --- Store results for subsequent PNG render requests ---
        analysis_id = f"{ipp_lat:.4f}_{ipp_lng:.4f}"
        # Attach percentiles to the result so PNG renderers can access them
        # without needing to receive them again in a separate request
        result['percentiles'] = {'p25': p25, 'p50': p50, 'p75': p75}
        save_result(analysis_id, result)

        # Read the raster bounds so the frontend knows where to place the
        # map overlay (Leaflet needs the geographic extent of the image)
        import rasterio
        with rasterio.open(result['probability_path']) as src:
            bounds = src.bounds

        poa_results = result.get('poa_results', [])
        contour_geojson = result.get('contour_geojson', None)

        return jsonify({'status':'ok','analysis_id':analysis_id,
            'has_percentiles':has_percentiles,
            'poa_results':poa_results,
            'contour_geojson':contour_geojson,
            'bounds':{'west':bounds.left,'south':bounds.bottom,'east':bounds.right,'north':bounds.top},
            'cost_surface_url':f'/api/results/{analysis_id}/cost_surface.png',
            'percentiles_url':f'/api/results/{analysis_id}/percentiles.png',
            'cost_distance_url':f'/api/results/{analysis_id}/cost_distance.tif'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status':'error','message':str(e)}), 500


# ===============================================================================
# STEP 4: Result file serving (GeoTIFFs)
# ===============================================================================

@app.route('/api/results/<analysis_id>/<filename>')
def serve_result(analysis_id, filename):
    """Serve raw GeoTIFF files for download (cost-distance, cost surface, DEM, probability).

    These are the full-resolution raster outputs that users can download for
    use in external GIS software (QGIS, ArcGIS, CalTopo, etc.).

    Args:
        analysis_id: Unique analysis identifier
        filename: One of: probability.tif, cost_distance.tif, cost_surface.tif, dem.tif
    Returns:
        The requested GeoTIFF file as an attachment download
    """
    result = load_result(analysis_id)
    if not result:
        return jsonify({'status':'error','message':'Analysis not found'}), 404
    # Map URL filenames to actual file paths stored during the analysis
    file_map = {'probability.tif':result.get('probability_path'),
        'cost_distance.tif':result.get('cost_distance_path'),
        'cost_surface.tif':result.get('cost_surface_path'),'dem.tif':result.get('dem_path')}
    filepath = file_map.get(filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({'status':'error','message':'File not found'}), 404
    return send_file(filepath, mimetype='image/tiff', as_attachment=True, download_name=filename)


# ===============================================================================
# STEP 5: Map overlay PNG renderers
# ===============================================================================
# These endpoints dynamically render the raster data as transparent PNG images
# that Leaflet overlays on the map. The PNGs are generated on-the-fly from
# the GeoTIFF outputs rather than being pre-rendered, so they always reflect
# the current analysis results.

@app.route('/api/results/<analysis_id>/cost_surface.png')
def serve_cost_png(analysis_id):
    """Render the probability density surface as a color-ramped PNG overlay.

    Despite the URL name (cost_surface.png), this actually renders the
    log-normal probability density — the "heat map" that shows where the
    subject is most likely to be found. The color ramp goes from blue (low
    density) through green and yellow to red (peak find probability).

    The rendering process:
      1. Read the cost-distance raster
      2. Fit a log-normal distribution from the subject's percentiles
      3. Evaluate the PDF at each cell's cost-distance value
      4. Map normalized PDF values to the blue→red color ramp
      5. Fade alpha beyond the 75th percentile to de-emphasize low-probability areas

    Args:
        analysis_id: Unique analysis identifier
    Returns:
        Transparent RGBA PNG image of the probability density surface
    """
    result = load_result(analysis_id)
    if not result:
        return jsonify({'status':'error','message':'Analysis not found'}), 404
    cd_path = result.get('cost_distance_path')
    if not cd_path or not os.path.exists(cd_path):
        return jsonify({'status':'error','message':'File not found'}), 404
    try:
        import rasterio
        import numpy as np
        from PIL import Image
        import io
        import math
        with rasterio.open(cd_path) as src:
            data = src.read(1).astype(np.float64)
        nodata_mask = (data <= 0) | (data == -9999) | np.isinf(data) | np.isnan(data)
        height, width = data.shape

        # --- Fit log-normal distribution from stored percentiles ---
        # Same math as in pipeline.compute_segment_poa: mu from median,
        # sigma from IQR using the inverse normal CDF constant 0.6745
        pct = result.get('percentiles', {})
        p25_m = float(pct.get('p25', 1.0)) * 1000
        p50_m = float(pct.get('p50', 2.0)) * 1000
        p75_m = float(pct.get('p75', 3.0)) * 1000
        mu = math.log(max(p50_m, 1))
        sigma = (math.log(max(p75_m, 1)) - math.log(max(p25_m, 1))) / (2 * 0.6745)
        sigma = max(sigma, 0.01)  # Prevent division by zero

        # --- Evaluate log-normal PDF at each cell ---
        safe_data = np.where(nodata_mask, 1.0, np.maximum(data, 1.0))
        log_data = np.log(safe_data)
        pdf = (1.0 / (safe_data * sigma * math.sqrt(2 * math.pi))) * np.exp(-0.5 * ((log_data - mu) / sigma) ** 2)
        pdf[nodata_mask] = 0
        # Normalize to 0-1 range for color mapping
        max_pdf = np.max(pdf)
        if max_pdf > 0:
            norm = pdf / max_pdf
        else:
            norm = np.zeros_like(pdf)
        norm = np.clip(norm, 0, 1)

        # --- Map normalized values to the color ramp ---
        # Blue (low density) → Green → Yellow → Orange → Red (peak density)
        # Each stop is (threshold, R, G, B). Linear interpolation between stops.
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        stops = [(0.0, 30,80,180), (0.10, 43,108,196), (0.20, 46,140,160),
                 (0.30, 46,165,92), (0.42, 80,195,60), (0.55, 180,210,40),
                 (0.68, 232,184,32), (0.80, 240,120,30), (0.90, 232,70,38),
                 (1.0, 210,45,35)]
        # Initialize with the lowest-stop color (blue)
        r_arr = np.full_like(norm, 30.0)
        g_arr = np.full_like(norm, 80.0)
        b_arr = np.full_like(norm, 180.0)
        for i in range(len(stops)-1):
            t0, r0, g0, b0 = stops[i]
            t1, r1, g1, b1 = stops[i+1]
            mask = (norm >= t0) & (norm < t1) if i < len(stops)-2 else (norm >= t0) & (norm <= t1)
            # Linear interpolation between this stop and the next
            frac = np.where(mask, (norm - t0) / (t1 - t0), 0)
            r_arr = np.where(mask, r0 + frac * (r1 - r0), r_arr)
            g_arr = np.where(mask, g0 + frac * (g1 - g0), g_arr)
            b_arr = np.where(mask, b0 + frac * (b1 - b0), b_arr)
        rgba[:,:,0] = r_arr.clip(0,255).astype(np.uint8)
        rgba[:,:,1] = g_arr.clip(0,255).astype(np.uint8)
        rgba[:,:,2] = b_arr.clip(0,255).astype(np.uint8)

        # --- Alpha channel: full opacity in core area, fading beyond p75 ---
        # This visually de-emphasizes the low-probability containment zone
        # so the map isn't overwhelmed by color in areas where the subject
        # is unlikely to be found
        base_alpha = 170
        alpha = np.where(nodata_mask, 0, base_alpha).astype(np.float64)
        beyond_p75 = (data > p75_m) & (~nodata_mask)
        # Fade linearly from full alpha at p75 to 15% alpha at p75 + 80%
        fade = np.clip(1.0 - (data - p75_m) / (p75_m * 0.8), 0.15, 1.0)
        alpha = np.where(beyond_p75, alpha * fade, alpha)
        rgba[:,:,3] = alpha.clip(0,255).astype(np.uint8)

        # --- Encode as PNG and return ---
        img = Image.fromarray(rgba, 'RGBA')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status':'error','message':str(e)}), 500


@app.route('/api/results/<analysis_id>/terrain.png')
def serve_terrain_png(analysis_id):
    """Render the terrain difficulty layer as a green-to-red PNG overlay.

    Combines slope steepness and land cover friction into a single "difficulty"
    score. This layer helps SAR planners visually identify terrain barriers
    and easy-travel corridors independently of the probability model.

    The difficulty score is the maximum of:
      - Slope component: 0° = easy, 15° = moderate, 30° = hard, 45° = extreme
      - Friction component: trail = easy, forest = moderate, wetland = hard

    Using max (not sum) means terrain is rated by its hardest component —
    a flat wetland is just as difficult as a steep trail.

    Args:
        analysis_id: Unique analysis identifier
    Returns:
        Transparent RGBA PNG of terrain difficulty (green = easy, red = hard)
    """
    result = load_result(analysis_id)
    if not result:
        return jsonify({'status':'error','message':'Analysis not found'}), 404
    nlcd_path = result.get('nlcd_path')
    cost_path = result.get('cost_surface_path')
    dem_path = result.get('dem_path')
    if not cost_path or not os.path.exists(cost_path):
        return jsonify({'status':'error','message':'File not found'}), 404
    try:
        import rasterio
        import numpy as np
        from PIL import Image
        import io
        from scipy.signal import convolve2d
        import math
        # Read friction grid and DEM
        with rasterio.open(cost_path) as src:
            friction = src.read(1).astype(np.float64)
            transform = src.transform
            height, width = friction.shape
        with rasterio.open(dem_path) as src:
            dem = src.read(1).astype(np.float64)
            if dem.shape != (height, width):
                from rasterio.warp import reproject, Resampling
                dem2 = np.zeros((height, width), dtype=np.float64)
                reproject(source=rasterio.band(src, 1), destination=dem2,
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=transform, dst_crs=src.crs, resampling=Resampling.bilinear)
                dem = dem2

        # --- Compute slope using Horn's method (same as pipeline) ---
        dem[dem < -1000] = np.nan
        dem[dem > 10000] = np.nan
        center_lat = (transform[5] + transform[5] + transform[4] * height) / 2
        cx = abs(transform[0]) * 111320 * math.cos(math.radians(center_lat))
        cy = abs(transform[4]) * 110540
        kx = np.array([[-1,0,1],[-2,0,2],[-1,0,1]]) / (8.0 * cx)
        ky = np.array([[-1,-2,-1],[0,0,0],[1,2,1]]) / (8.0 * cy)
        dzdx = convolve2d(dem, kx, mode='same', boundary='symm')
        dzdy = convolve2d(dem, ky, mode='same', boundary='symm')
        slope_deg = np.degrees(np.arctan(np.sqrt(dzdx**2 + dzdy**2)))

        # --- Combine slope and friction into difficulty score (0-100) ---
        # Slope component: maps degrees to a 0-90 score
        slope_score = np.clip(slope_deg * 2.0, 0, 90)
        # Friction component: maps friction multiplier to a 0-95 score
        fric_score = np.clip((friction - 1.0) * 20.0, 0, 95)
        # Take the max — terrain is as hard as its hardest component
        difficulty = np.maximum(slope_score, fric_score)
        nodata_mask = np.isnan(dem) | (friction <= 0) | (friction == -9999)
        norm = difficulty / 100.0
        norm = np.clip(norm, 0, 1)

        # --- Map to green-to-red color ramp ---
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        stops = [(0.0, 20,140,40), (0.1, 50,175,50), (0.2, 100,200,45),
                 (0.3, 160,210,30), (0.4, 210,215,15), (0.5, 240,195,0),
                 (0.6, 245,150,10), (0.7, 235,100,15), (0.8, 215,55,12),
                 (0.9, 185,25,10), (1.0, 140,12,10)]
        r_arr = np.full_like(norm, 140.0)
        g_arr = np.full_like(norm, 12.0)
        b_arr = np.full_like(norm, 10.0)
        for i in range(len(stops)-1):
            t0, r0, g0, b0 = stops[i]
            t1, r1, g1, b1 = stops[i+1]
            mask = (norm >= t0) & (norm < t1) if i < len(stops)-2 else (norm >= t0) & (norm <= t1)
            frac = np.where(mask, (norm - t0) / (t1 - t0), 0)
            r_arr = np.where(mask, r0 + frac * (r1 - r0), r_arr)
            g_arr = np.where(mask, g0 + frac * (g1 - g0), g_arr)
            b_arr = np.where(mask, b0 + frac * (b1 - b0), b_arr)
        rgba[:,:,0] = r_arr.clip(0,255).astype(np.uint8)
        rgba[:,:,1] = g_arr.clip(0,255).astype(np.uint8)
        rgba[:,:,2] = b_arr.clip(0,255).astype(np.uint8)
        rgba[:,:,3] = np.where(nodata_mask, 0, 150).astype(np.uint8)

        img = Image.fromarray(rgba, 'RGBA')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status':'error','message':str(e)}), 500


@app.route('/api/results/<analysis_id>/percentiles.png')
def serve_percentile_png(analysis_id):
    """Render the percentile zone boundaries as a PNG overlay with contour lines.

    This is a raster-based fallback for the percentile contours — the primary
    contours are now rendered as vector GeoJSON on the frontend, but this PNG
    version is kept for the downloadable "Percentile Contours (PNG)" option.

    The rendering draws semi-transparent filled zones at each percentile
    threshold, then overlays thin contour lines at the zone boundaries using
    edge detection (finite differences). Labels are placed at the rightmost
    edge of each contour line near the vertical center of the image.

    Args:
        analysis_id: Unique analysis identifier
    Returns:
        Transparent RGBA PNG of percentile zones with contour lines and labels
    """
    result = load_result(analysis_id)
    if not result:
        return jsonify({'status':'error','message':'Analysis not found'}), 404
    prob_path = result.get('probability_path')
    if not prob_path or not os.path.exists(prob_path):
        return jsonify({'status':'error','message':'File not found'}), 404
    try:
        import rasterio
        import numpy as np
        from PIL import Image, ImageDraw
        import io
        with rasterio.open(prob_path) as src:
            data = src.read(1)
        height, width = data.shape

        # --- Draw semi-transparent filled zones ---
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        rgba[data == 4] = [220, 38, 38, 100]    # 25th percentile zone (red)
        rgba[data == 3] = [245, 158, 11, 80]    # 50th percentile zone (orange)
        rgba[data == 2] = [250, 204, 21, 60]    # 75th percentile zone (yellow)

        # --- Draw contour lines at zone boundaries ---
        # Edge detection using finite differences: where adjacent cells have
        # different zone values, there's a boundary. We dilate by 1px using
        # a cross-shaped structuring element for slightly thicker lines.
        for zone_val, color in [(4, [255,255,255,220]), (3, [255,200,50,200]), (2, [255,100,30,200])]:
            mask = (data >= zone_val).astype(np.uint8)
            kernel_h = np.abs(np.diff(mask, axis=1))
            kernel_v = np.abs(np.diff(mask, axis=0))
            edge = np.zeros_like(mask)
            edge[:, :-1] |= kernel_h
            edge[:, 1:] |= kernel_h
            edge[:-1, :] |= kernel_v
            edge[1:, :] |= kernel_v
            # Thicken the contour line with morphological dilation
            from scipy.ndimage import binary_dilation
            struct = np.array([[0,1,0],[1,1,1],[0,1,0]], dtype=bool)
            edge = binary_dilation(edge, structure=struct, iterations=1).astype(np.uint8)
            edge_mask = edge == 1
            rgba[edge_mask, 0] = color[0]
            rgba[edge_mask, 1] = color[1]
            rgba[edge_mask, 2] = color[2]
            rgba[edge_mask, 3] = color[3]

        # --- Add percentile labels ---
        img = Image.fromarray(rgba, 'RGBA')
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
        except:
            font = ImageFont.load_default()
        # Place labels at the rightmost edge of each contour, staggered vertically
        label_offsets = {4: -20, 3: 0, 2: 20}
        for zone_val, label, lcolor in [(4, '25%', (255,255,255)), (3, '50%', (255,220,80)), (2, '75%', (255,120,50))]:
            zmask = (data >= zone_val).astype(np.uint8)
            kh2 = np.abs(np.diff(zmask, axis=1))
            epts = np.zeros((height, width), dtype=np.uint8)
            epts[:, :-1] |= kh2
            # Search for the rightmost edge pixel near the vertical center
            target_row = height // 2 + label_offsets[zone_val]
            row_start = max(0, target_row - 5)
            row_end = min(height, target_row + 5)
            row_edge = epts[row_start:row_end, :]
            ys, xs = np.where(row_edge > 0)
            if len(xs) > 0:
                lx = int(np.max(xs)) + 5
                ly = target_row - 6
            else:
                continue
            lx = min(lx, width - 30)
            ly = max(ly, 2)
            # Draw a small rounded rectangle background for readability
            draw.rounded_rectangle([lx-2, ly-1, lx+26, ly+13], radius=2, fill=(0,0,0,160))
            draw.text((lx, ly), label, fill=lcolor+(255,), font=font)

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status':'error','message':str(e)}), 500


# ===============================================================================
# STEP 6: Development server entry point
# ===============================================================================
# In production, the app is served by Gunicorn (configured in the systemd
# service file), not by Flask's built-in development server. This block
# only runs when executing server.py directly for local development/testing.
# The if __name__ == "__main__" guard is a Python convention that prevents
# this block from running when the file is imported as a module.

if __name__ == '__main__':
    app.run(debug=True, port=5000)
