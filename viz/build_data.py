"""
build_data.py -- fetch one clear summer Landsat scene over Daegu and prepare
web-ready data for the 3D NDVI / land-surface-temperature page.

What it does (each step is cached in viz/data/cache/ and skipped on re-run):
  1. Search Planetary Computer for Landsat 8/9 Collection 2 Level-2 scenes
     (June-September, 2022-2026, scene cloud cover < 40 %).
  2. Windowed-read the QA band over the AOI for every candidate and keep the
     clearest scene (coverage >= 0.95, clear fraction >= 0.90, else >= 0.80).
  3. Windowed-read lwir11 / red / nir08 / qa_pixel for that scene only,
     scale raw DN to physical units, mask bad pixels, compute LST and NDVI.
  4. Sanity-check the values (stops with an error if they look wrong).
  5. Average 30 m pixels into a ~250 x 250 web grid.
  6. Fetch the Copernicus 30 m DEM and resample it onto the same grid.
  7. Fit an ordinary least squares line  lst_c ~ ndvi  over all grid cells.

Outputs (viz/data/): lst.png, ndvi.png, height.bin, grid_values.bin,
cells.json, stats.json

Usage:
    .venv/bin/python viz/build_data.py           # skip finished steps
    .venv/bin/python viz/build_data.py --force   # recompute everything
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np

# ----------------------------------------------------------------------------
# Tunable values
# ----------------------------------------------------------------------------
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
LANDSAT_COLLECTION = "landsat-c2-l2"
DEM_COLLECTION = "cop-dem-glo-30"

BBOX = (128.35, 35.70, 128.80, 36.05)        # lon_min, lat_min, lon_max, lat_max
YEARS = range(2022, 2027)                    # 2022..2026 inclusive
SEASON = ("06-01", "09-30")                  # June-September
PLATFORMS = ["landsat-8", "landsat-9"]
MAX_SCENE_CLOUD = 40                         # eo:cloud_cover upper limit (%)

MIN_COVERAGE = 0.95                          # share of AOI inside the scene
CLEAR_THRESHOLD = 0.90                       # share of covered AOI that is clear
CLEAR_FALLBACK = 0.80                        # used only if nothing passes 0.90

# Collection 2 Level-2 scaling (verified against STAC raster:bands at run time)
ST_SCALE, ST_OFFSET = 0.00341802, 149.0      # lwir11 DN -> Kelvin
SR_SCALE, SR_OFFSET = 0.0000275, -0.2        # red / nir08 DN -> reflectance
KELVIN_TO_C = 273.15

# qa_pixel bits that make a pixel unusable
QA_BITS = {"fill": 0, "dilated_cloud": 1, "cirrus": 2, "cloud": 3,
           "cloud_shadow": 4, "water": 7}
CLOUD_BITS = ["dilated_cloud", "cirrus", "cloud", "cloud_shadow"]
SR_VALID = (0.0, 1.0)                        # USGS valid reflectance range (DN 7273-43636)

TARGET_GRID = 250                            # aim for about this many cells per side
MIN_VALID_SHARE = 2 / 3                      # valid 30 m pixels needed per grid cell
LST_SANITY_C = (5.0, 65.0)                   # p1 and p99 of LST must lie inside
COLOR_PCT = (2, 98)                          # colour-ramp stretch percentiles
SAMPLE_CELLS = 3000
SEED = 42
MAX_DOWNLOAD_MB = 500

HERE = Path(__file__).resolve().parent
OUT = HERE / "data"
CACHE = OUT / "cache"
DOWNLOAD_LOG = CACHE / "download_log.json"


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def stac_client():
    """Open the Planetary Computer STAC API with automatic URL signing."""
    import planetary_computer
    import pystac_client
    return pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)


def signed_href(item, asset_key):
    """Return a freshly signed URL (signatures expire, so sign right before reading)."""
    import planetary_computer
    return planetary_computer.sign(item.assets[asset_key].href)


def bit_set(qa, bit):
    """True where the given bit of the QA array is 1."""
    return (qa >> bit) & 1 == 1


def load_download_log(force):
    """Running tally of bytes fetched, kept across re-runs."""
    if DOWNLOAD_LOG.exists() and not force:
        return json.loads(DOWNLOAD_LOG.read_text())
    return {"bytes_upper_bound": 0, "reads": 0}


def record_read(log, src, window):
    """
    Add an upper-bound byte estimate for one windowed read: the number of
    internal COG tiles the window touches times their uncompressed size.
    Real transfers are smaller because the tiles are compressed.
    """
    from rasterio.windows import Window
    full = Window(0, 0, src.width, src.height)
    try:
        w = window.intersection(full)
    except Exception:
        return  # window entirely outside the file
    bh, bw = src.block_shapes[0]
    tiles_x = math.floor((w.col_off + w.width - 1) / bw) - math.floor(w.col_off / bw) + 1
    tiles_y = math.floor((w.row_off + w.height - 1) / bh) - math.floor(w.row_off / bh) + 1
    nbytes = tiles_x * tiles_y * bh * bw * np.dtype(src.dtypes[0]).itemsize
    log["bytes_upper_bound"] += int(nbytes)
    log["reads"] += 1
    DOWNLOAD_LOG.write_text(json.dumps(log, indent=2))
    mb = log["bytes_upper_bound"] / 1e6
    if mb > MAX_DOWNLOAD_MB:
        raise RuntimeError(f"Download budget exceeded: {mb:.0f} MB > {MAX_DOWNLOAD_MB} MB")


def aoi_utm_bounds(crs):
    """AOI bounding box re-projected to the scene CRS (metres)."""
    from rasterio.warp import transform_bounds
    return transform_bounds("EPSG:4326", crs, *BBOX, densify_pts=21)


def read_window(src, bounds, log):
    """Windowed, boundless read of band 1 over `bounds`, aligned to the source pixel grid."""
    win = src.window(*bounds).round_offsets().round_lengths()
    record_read(log, src, win)
    fill = src.nodata if src.nodata is not None else 0
    data = src.read(1, window=win, boundless=True, fill_value=fill)
    return data, src.window_transform(win)


# ----------------------------------------------------------------------------
# Step 1: search + metadata check
# ----------------------------------------------------------------------------
def search_candidates(force):
    """Find summer Landsat 8/9 scenes over the AOI. Cached as candidates.json."""
    path = CACHE / "candidates.json"
    if path.exists() and not force:
        print(f"[1] using cached candidate list ({path.name})")
        return json.loads(path.read_text())

    client = stac_client()
    items = []
    for year in YEARS:
        search = client.search(
            collections=[LANDSAT_COLLECTION], bbox=BBOX,
            datetime=f"{year}-{SEASON[0]}/{year}-{SEASON[1]}",
            query={"eo:cloud_cover": {"lt": MAX_SCENE_CLOUD}, "platform": {"in": PLATFORMS}},
        )
        found = list(search.items())
        print(f"[1] {year}: {len(found)} scenes with cloud cover < {MAX_SCENE_CLOUD}%")
        items += found

    if not items:
        raise SystemExit("No candidate scenes found. Stopping.")
    check_scaling(items[0])

    rows = [{"id": it.id, "datetime": it.datetime.isoformat(),
             "platform": it.properties["platform"],
             "cloud_cover": it.properties["eo:cloud_cover"]} for it in items]
    path.write_text(json.dumps(rows, indent=2))
    return rows


def check_scaling(item):
    """Print asset keys and confirm the scale/offset constants match the STAC metadata."""
    print(f"[1] asset keys of {item.id}:\n    {sorted(item.assets)}")
    expected = {"lwir11": (ST_SCALE, ST_OFFSET), "red": (SR_SCALE, SR_OFFSET),
                "nir08": (SR_SCALE, SR_OFFSET)}
    for key in ["lwir11", "red", "nir08", "qa_pixel"]:
        band = item.assets[key].extra_fields["raster:bands"][0]
        print(f"    {key:9s} scale={band.get('scale')} offset={band.get('offset')} "
              f"nodata={band.get('nodata')} unit={band.get('unit')}")
        if key in expected:
            scale, offset = expected[key]
            assert math.isclose(band["scale"], scale) and math.isclose(band["offset"], offset), \
                f"{key}: STAC scale/offset differ from the constants at the top of this script"
    print("[1] scaling constants confirmed")


def fetch_items(ids):
    """Re-fetch STAC items by id (so URLs can be freshly signed)."""
    client = stac_client()
    items = list(client.search(collections=[LANDSAT_COLLECTION], ids=list(ids)).items())
    return {it.id: it for it in items}


# ----------------------------------------------------------------------------
# Step 2: QA screening
# ----------------------------------------------------------------------------
def screen_scenes(candidates, log, force):
    """Measure AOI coverage and clear fraction for every candidate from its QA band."""
    import rasterio
    path = CACHE / "qa_screen.json"
    if path.exists() and not force:
        print(f"[2] using cached QA screening ({path.name})")
        return json.loads(path.read_text())

    items = fetch_items(c["id"] for c in candidates)
    results = []
    for i, cand in enumerate(candidates, 1):
        item = items[cand["id"]]
        with rasterio.open(signed_href(item, "qa_pixel")) as src:
            src_nodata_fill = 1  # qa_pixel nodata value = fill bit set
            win = src.window(*aoi_utm_bounds(src.crs)).round_offsets().round_lengths()
            record_read(log, src, win)
            qa = src.read(1, window=win, boundless=True, fill_value=src_nodata_fill)
        covered = ~bit_set(qa, QA_BITS["fill"])
        cloudy = np.zeros_like(covered)
        for name in CLOUD_BITS:
            cloudy |= bit_set(qa, QA_BITS[name])
        coverage = covered.mean()
        clear = (covered & ~cloudy).sum() / max(covered.sum(), 1)
        results.append({**cand, "coverage": round(float(coverage), 4),
                        "clear_fraction": round(float(clear), 4)})
        print(f"[2] {i:2d}/{len(candidates)} {cand['id']}  coverage={coverage:.3f} clear={clear:.3f}")

    path.write_text(json.dumps(results, indent=2))
    return results


def pick_primary(screened):
    """Choose the clearest scene that passes coverage and the clear threshold."""
    covered = [s for s in screened if s["coverage"] >= MIN_COVERAGE]
    for threshold in (CLEAR_THRESHOLD, CLEAR_FALLBACK):
        passing = [s for s in covered if s["clear_fraction"] >= threshold]
        if passing:
            best = max(passing, key=lambda s: (s["clear_fraction"], s["coverage"]))
            print(f"[2] {len(passing)} scene(s) pass clear >= {threshold}; "
                  f"primary = {best['id']} (clear {best['clear_fraction']:.3f})")
            return best, threshold, len(passing)
    raise SystemExit(
        f"No scene has coverage >= {MIN_COVERAGE} and clear fraction >= {CLEAR_FALLBACK}. "
        "Stopping without producing data.")


# ----------------------------------------------------------------------------
# Step 3-4: LST and NDVI for the primary scene
# ----------------------------------------------------------------------------
def compute_lst_ndvi(primary, log, force):
    """Read the four bands, scale, mask, and return 30 m LST (deg C), NDVI and valid mask."""
    import rasterio
    path = CACHE / "primary_30m.npz"
    if path.exists() and not force:
        print(f"[3] using cached 30 m LST/NDVI ({path.name})")
        z = np.load(path, allow_pickle=False)
        return z["lst"], z["ndvi"], z["valid"], tuple(z["transform"]), str(z["crs"])

    item = fetch_items([primary["id"]])[primary["id"]]
    bands, transform, crs = {}, None, None
    for key in ["lwir11", "red", "nir08", "qa_pixel"]:
        with rasterio.open(signed_href(item, key)) as src:
            crs = crs or src.crs.to_string()
            bands[key], t = read_window(src, aoi_utm_bounds(src.crs), log)
            assert transform is None or t == transform, "band windows are not aligned"
            transform = t
        print(f"[3] read {key}: {bands[key].shape}")

    qa = bands["qa_pixel"]
    bad = (bands["lwir11"] == 0) | (bands["red"] == 0) | (bands["nir08"] == 0)
    for bit in QA_BITS.values():
        bad |= bit_set(qa, bit)

    # Scale once, on raw DN
    lst = bands["lwir11"].astype("float64") * ST_SCALE + ST_OFFSET - KELVIN_TO_C
    red = bands["red"].astype("float64") * SR_SCALE + SR_OFFSET
    nir = bands["nir08"].astype("float64") * SR_SCALE + SR_OFFSET
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir - red) / (nir + red)

    in_range = ((red >= SR_VALID[0]) & (red <= SR_VALID[1]) &
                (nir >= SR_VALID[0]) & (nir <= SR_VALID[1]))
    print(f"[3] pixels with reflectance outside {SR_VALID}: {(~in_range & ~bad).sum():,}")
    valid = ~bad & in_range & np.isfinite(lst) & np.isfinite(ndvi)
    lst[~valid] = np.nan
    ndvi[~valid] = np.nan
    print(f"[3] valid 30 m pixels: {valid.sum():,} of {valid.size:,} ({valid.mean():.1%})")

    sanity_check(lst, ndvi)
    np.savez_compressed(path, lst=lst.astype("float32"), ndvi=ndvi.astype("float32"),
                        valid=valid, transform=np.array(transform[:6]), crs=np.array(crs))
    return lst.astype("float32"), ndvi.astype("float32"), valid, tuple(transform[:6]), crs


def sanity_check(lst, ndvi):
    """Stop if values are physically implausible (usually means wrong scaling or masking)."""
    p1, p99 = np.nanpercentile(lst, [1, 99])
    lo, hi = np.nanmin(ndvi), np.nanmax(ndvi)
    print(f"[4] LST p1={p1:.2f} C  p99={p99:.2f} C;  NDVI min={lo:.3f} max={hi:.3f}")
    if not (LST_SANITY_C[0] <= p1 and p99 <= LST_SANITY_C[1]):
        raise SystemExit(f"LST percentiles outside {LST_SANITY_C} C -- check scaling/masking.")
    if not (-1 <= lo and hi <= 1):
        raise SystemExit("NDVI outside [-1, 1] -- check scaling/masking.")
    print("[4] sanity checks passed")


# ----------------------------------------------------------------------------
# Step 5: aggregate to the web grid
# ----------------------------------------------------------------------------
def block_factor(shape):
    """30 m pixels per grid cell side, chosen so the grid is about TARGET_GRID cells across."""
    return max(1, round(max(shape) / TARGET_GRID))


def trim(arr, f):
    """Crop so both dimensions are a multiple of f."""
    h, w = arr.shape
    return arr[: h // f * f, : w // f * f]


def block_mean(arr, valid, f):
    """Block nanmean; cells with fewer than MIN_VALID_SHARE valid pixels become NaN."""
    a, v = trim(arr, f), trim(valid, f)
    h, w = a.shape[0] // f, a.shape[1] // f
    a4 = np.where(v, a, 0).reshape(h, f, w, f).astype("float64")
    n = v.reshape(h, f, w, f).sum(axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = a4.sum(axis=(1, 3)) / n
    mean[n < MIN_VALID_SHARE * f * f] = np.nan
    return mean.astype("float32")


# ----------------------------------------------------------------------------
# Step 6: DEM
# ----------------------------------------------------------------------------
def fetch_dem(crs, grid_transform, shape, log, force):
    """Resample Copernicus GLO-30 onto the web grid (average resampling)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import transform_bounds
    from affine import Affine
    path = CACHE / "dem_grid.npy"
    if path.exists() and not force:
        print(f"[6] using cached DEM ({path.name})")
        return np.load(path)

    client = stac_client()
    items = list(client.search(collections=[DEM_COLLECTION], bbox=BBOX).items())
    print(f"[6] {len(items)} DEM tile(s): {[it.id for it in items]}")
    height = np.full(shape, np.nan, dtype="float32")
    gt = Affine(*grid_transform)
    grid_bounds = (gt.c, gt.f + gt.e * shape[0], gt.c + gt.a * shape[1], gt.f)
    for item in items:
        with rasterio.open(signed_href(item, "data")) as src:
            src_bounds = transform_bounds(crs, src.crs, *grid_bounds, densify_pts=21)
            record_read(log, src, src.window(*src_bounds).round_offsets().round_lengths())
            with WarpedVRT(src, crs=crs, transform=gt, width=shape[1], height=shape[0],
                           resampling=Resampling.average, src_nodata=-32767,
                           nodata=np.nan, dtype="float32") as vrt:
                tile = vrt.read(1)
        fill = np.isnan(height) & np.isfinite(tile)
        height[fill] = tile[fill]
    missing = np.isnan(height).sum()
    print(f"[6] DEM range {np.nanmin(height):.0f}-{np.nanmax(height):.0f} m; missing cells: {missing}")
    if missing:
        height[np.isnan(height)] = np.nanmin(height)
    np.save(path, height)
    return height


# ----------------------------------------------------------------------------
# Step 7: regression
# ----------------------------------------------------------------------------
def fit_ols(x, y):
    """OLS y = a + b x with a 95 % t-interval on b (assumes independent cells)."""
    from scipy import stats
    n = len(x)
    xm, ym = x.mean(), y.mean()
    sxx = ((x - xm) ** 2).sum()
    b = ((x - xm) * (y - ym)).sum() / sxx
    a = ym - b * xm
    resid = y - (a + b * x)
    sse = (resid ** 2).sum()
    r2 = 1 - sse / ((y - ym) ** 2).sum()
    se_b = math.sqrt(sse / (n - 2) / sxx)
    t = stats.t.ppf(0.975, n - 2)
    return {"intercept": a, "slope": b, "slope_se": se_b,
            "slope_ci95": [b - t * se_b, b + t * se_b], "r2": r2, "n": n}


# ----------------------------------------------------------------------------
# Outputs
# ----------------------------------------------------------------------------
def colorize(arr, cmap_name, vmin, vmax, path):
    """Save a colour-mapped RGBA PNG; NaN pixels become fully transparent."""
    import matplotlib
    from PIL import Image
    cmap = matplotlib.colormaps[cmap_name]
    norm = np.clip((arr - vmin) / (vmax - vmin), 0, 1)
    rgba = (cmap(np.nan_to_num(norm)) * 255).astype("uint8")
    rgba[..., 3] = np.where(np.isfinite(arr), 255, 0)
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


def round_list(values, nd):
    return [round(float(v), nd) for v in values]


def write_outputs(primary, threshold, n_passing, n_candidates, lst30, ndvi30,
                  lst_g, ndvi_g, height, f, transform, crs, log):
    """Write textures, heightmap, sampled cells and the stats file."""
    both = np.isfinite(lst_g) & np.isfinite(ndvi_g)
    lst_rng = round_list(np.percentile(lst_g[both], COLOR_PCT), 2)
    ndvi_rng = round_list(np.percentile(ndvi_g[both], COLOR_PCT), 3)

    # Textures at native 30 m (cropped to the grid extent) for a crisp map
    colorize(trim(lst30, f), "RdYlBu_r", *lst_rng, OUT / "lst.png")
    colorize(trim(ndvi30, f), "YlGn", *ndvi_rng, OUT / "ndvi.png")

    height.astype("<f4").tofile(OUT / "height.bin")
    # Grid-cell values (NaN = no data) so the page can highlight every cell in a brush range
    np.concatenate([lst_g.ravel(), ndvi_g.ravel()]).astype("<f4").tofile(OUT / "grid_values.bin")

    rows, cols = np.nonzero(both)
    rng = np.random.default_rng(SEED)
    pick = rng.choice(len(rows), size=min(SAMPLE_CELLS, len(rows)), replace=False)
    cells = [{"row": int(rows[i]), "col": int(cols[i]),
              "lst_c": round(float(lst_g[rows[i], cols[i]]), 2),
              "ndvi": round(float(ndvi_g[rows[i], cols[i]]), 4)} for i in sorted(pick)]
    (OUT / "cells.json").write_text(json.dumps(cells, separators=(",", ":")))

    ols = fit_ols(ndvi_g[both].astype("float64"), lst_g[both].astype("float64"))
    print(f"[7] OLS slope {ols['slope'] * 0.1:.3f} C per 0.1 NDVI, R2={ols['r2']:.3f}, n={ols['n']}")

    from datetime import datetime, timedelta, timezone
    t_utc = datetime.fromisoformat(primary["datetime"])
    t_kst = t_utc.astimezone(timezone(timedelta(hours=9)))
    a, b, c, d, e, g = transform
    cell = a * f
    stats = {
        "scene_id": primary["id"],
        "platform": primary["platform"],
        "datetime_utc": t_utc.isoformat(),
        "date": t_kst.strftime("%Y-%m-%d"),
        "time_kst": t_kst.strftime("%H:%M"),
        "scene_cloud_cover_pct": primary["cloud_cover"],
        "aoi_coverage": primary["coverage"],
        "aoi_clear_fraction": primary["clear_fraction"],
        "clear_threshold_used": threshold,
        "scenes_searched": n_candidates,
        "scenes_passing": n_passing,
        "color_ranges": {"lst_c": lst_rng, "ndvi": ndvi_rng,
                         "percentiles": list(COLOR_PCT),
                         "lst_cmap": "RdYlBu_r", "ndvi_cmap": "YlGn"},
        "ols": {
            "formula": "lst_c ~ ndvi",
            "intercept": round(ols["intercept"], 4),
            "slope_per_ndvi": round(ols["slope"], 4),
            "slope_per_0_1_ndvi": round(ols["slope"] * 0.1, 4),
            "slope_per_0_1_ci95": round_list([v * 0.1 for v in ols["slope_ci95"]], 4),
            "r2": round(ols["r2"], 4),
            "n": ols["n"],
            "note": "CI assumes independent cells; spatial autocorrelation makes it too narrow.",
        },
        "grid": {
            "rows": int(lst_g.shape[0]), "cols": int(lst_g.shape[1]),
            "cell_size_m": cell, "pixels_per_cell": f,
            "min_valid_share": round(MIN_VALID_SHARE, 4),
            "crs": crs,
            "bounds_utm": [c, g + e * f * lst_g.shape[0], c + cell * lst_g.shape[1], g],
            "valid_cells": int(both.sum()),
        },
        "texture": {"rows": int(trim(lst30, f).shape[0]), "cols": int(trim(lst30, f).shape[1]),
                    "pixel_size_m": a},
        "height": {"min_m": round(float(height.min()), 1), "max_m": round(float(height.max()), 1),
                   "source": "Copernicus DEM GLO-30 (cop-dem-glo-30)"},
        "bbox_lonlat": list(BBOX),
        "valid_value_ranges": {
            "lst_c_p1_p99": round_list(np.nanpercentile(lst30, [1, 99]), 2),
            "ndvi_min_max": round_list([np.nanmin(ndvi30), np.nanmax(ndvi30)], 4)},
        "qa_masked_bits": QA_BITS,
        "sampled_cells": len(cells),
        "sample_seed": SEED,
        "download_mb_upper_bound": round(log["bytes_upper_bound"] / 1e6, 1),
    }
    (OUT / "stats.json").write_text(json.dumps(stats, indent=2))
    print(f"[7] wrote outputs to {OUT}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--force", action="store_true", help="recompute every step")
    args = parser.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    log = load_download_log(args.force)

    candidates = search_candidates(args.force)
    screened = screen_scenes(candidates, log, args.force)
    primary, threshold, n_passing = pick_primary(screened)

    lst30, ndvi30, valid, transform, crs = compute_lst_ndvi(primary, log, args.force)
    f = block_factor(lst30.shape)
    lst_g = block_mean(lst30, valid, f)
    ndvi_g = block_mean(ndvi30, valid, f)
    print(f"[5] grid {lst_g.shape[1]} x {lst_g.shape[0]} cells of {f * transform[0]:.0f} m; "
          f"valid cells: {np.isfinite(lst_g).sum():,}")

    grid_transform = (transform[0] * f, 0.0, transform[2], 0.0, transform[4] * f, transform[5])
    height = fetch_dem(crs, grid_transform, lst_g.shape, log, args.force)

    write_outputs(primary, threshold, n_passing, len(candidates), lst30, ndvi30,
                  lst_g, ndvi_g, height, f, transform, crs, log)
    print(f"Downloads so far (upper bound, uncompressed tiles): "
          f"{log['bytes_upper_bound'] / 1e6:.1f} MB in {log['reads']} reads")


if __name__ == "__main__":
    main()
