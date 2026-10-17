"""
build_page.py -- assemble viz/daegu_summer_heat.html from viz/data/.

Everything is embedded in the one HTML file (textures and binary arrays as
base64, statistics as inline JSON, three.js inlined), so the page opens by
double-click with no network access.

Every number printed on the page comes from viz/data/stats.json. Each one is
wrapped in <span data-stat="..."> so viz/verify.py can check it.

Usage:
    .venv/bin/python viz/build_page.py
"""

import base64
import json
import re
from pathlib import Path

import matplotlib
import numpy as np

# ----------------------------------------------------------------------------
# Tunable values
# ----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
VENDOR = HERE / "vendor"                    # three.js r147 (UMD build) + OrbitControls
TEMPLATE = HERE / "page_template.html"
OUTPUT = HERE / "daegu_summer_heat.html"
LABELS = HERE.parent / "inputs" / "labels.geojson"   # optional, provided by the student
LEGEND_STOPS = 11                           # colour stops per legend bar
MAX_MB = 15
MINUS = "−"                            # typographic minus sign


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------
def fmt(value, dp, use_abs=False, thousands=False):
    """Format a number for display (typographic minus, optional thousands separator)."""
    v = abs(value) if use_abs else value
    text = f"{abs(v):,.{dp}f}" if thousands else f"{abs(v):.{dp}f}"
    return (MINUS if v < 0 else "") + text


def num(stats, path, dp, scale=1, use_abs=False, thousands=False):
    """
    A number from stats.json wrapped in a span that records where it came from.
    `path` is a dotted key, e.g. "ols.r2" or "color_ranges.lst_c.0".
    """
    value = lookup(stats, path) * scale
    attrs = f'data-stat="{path}" data-dp="{dp}"'
    if scale != 1:
        attrs += f' data-scale="{scale}"'
    if use_abs:
        attrs += ' data-abs="1"'
    return f'<span class="num" {attrs}>{fmt(value, dp, use_abs, thousands)}</span>'


def text_stat(stats, path):
    """A text value from stats.json (e.g. the date) in a checkable span."""
    return f'<span data-stat="{path}" data-text="1">{lookup(stats, path)}</span>'


def lookup(stats, path):
    """Follow a dotted path such as 'ols.slope_per_0_1_ci95.0' into the stats dict."""
    node = stats
    for part in path.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def ordinal(n):
    """2 -> '2nd', 98 -> '98th'."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def platform_name(stats):
    return stats["platform"].replace("landsat-", "Landsat ")


# ----------------------------------------------------------------------------
# Page text
# ----------------------------------------------------------------------------
def relationship_html(stats):
    """Relationship panel. Wording follows the sign and size of the fitted slope."""
    ols = stats["ols"]
    slope = ols["slope_per_0_1_ndvi"]
    lo, hi = ols["slope_per_0_1_ci95"]
    clear_negative = slope < 0 and hi < 0

    if clear_negative:
        r2 = ols["r2"]
        strength = "a strong" if r2 >= 0.5 else "a moderate" if r2 >= 0.2 else "a weak"
        sentence = (
            f"Greener cells were cooler on this date. On average, each +0.1 NDVI goes with a "
            f"surface temperature {num(stats, 'ols.slope_per_0_1_ndvi', 2, use_abs=True)} °C lower. "
            f"NDVI alone accounts for {num(stats, 'ols.r2', 0, scale=100)}% of the cell-to-cell "
            f"variation in LST, {strength} linear fit."
        )
    else:
        reason = "is not negative" if slope >= 0 else "includes zero"
        sentence = (
            f"No clear relationship was found: the fitted slope is "
            f"{num(stats, 'ols.slope_per_0_1_ndvi', 2)} °C per +0.1 NDVI and its 95% confidence "
            f"interval {reason}."
        )

    return f"""
    <section>
      <h2>LST change per +0.1 NDVI</h2>
      <div class="big">{num(stats, 'ols.slope_per_0_1_ndvi', 2)}<span class="unit">°C per +0.1 NDVI</span></div>
      <p class="lede">{sentence}</p>
      <dl class="kv">
        <dt>95% CI</dt><dd>{num(stats, 'ols.slope_per_0_1_ci95.0', 2)} to {num(stats, 'ols.slope_per_0_1_ci95.1', 2)} °C</dd>
        <dt>R²</dt><dd>{num(stats, 'ols.r2', 2)}</dd>
        <dt>n</dt><dd>{num(stats, 'ols.n', 0, thousands=True)} cells of {num(stats, 'grid.cell_size_m', 0)} m</dd>
        <dt>Scene</dt><dd>{platform_name(stats)}, {text_stat(stats, 'date')}, {text_stat(stats, 'time_kst')} KST</dd>
      </dl>
      <p class="caveat">Ordinary least squares, lst_c ~ ndvi. The interval assumes independent cells;
      neighbouring cells are correlated, so the real uncertainty is wider. This shows an
      association on one day, not cause and effect.</p>
    </section>"""


def sheet_summary(stats):
    return (f"<b>{num(stats, 'ols.slope_per_0_1_ndvi', 2)} °C</b>"
            f"<span>per +0.1 NDVI · R² {num(stats, 'ols.r2', 2)}</span>")


def legend_html(stats, cmap_name, key, label, unit_dp):
    """Colour bar with the fixed 2nd-98th percentile range at its ends."""
    cmap = matplotlib.colormaps[cmap_name]
    stops = [matplotlib.colors.to_hex(cmap(t)) for t in np.linspace(0, 1, LEGEND_STOPS)]
    p_lo, p_hi = stats["color_ranges"]["percentiles"]
    return f"""
      <div class="legend">
        <div class="lab"><span>{label}</span><span>{ordinal(p_lo)}–{ordinal(p_hi)} percentile stretch</span></div>
        <div class="bar" style="background:linear-gradient(90deg,{','.join(stops)})"></div>
        <div class="ends"><span>≤ {num(stats, f'color_ranges.{key}.0', unit_dp)}</span><span>≥ {num(stats, f'color_ranges.{key}.1', unit_dp)}</span></div>
      </div>"""


def footnote_html(stats):
    return f"""
      <p class="key">Surface temperature, not air temperature: a single {platform_name(stats)} overpass at
      {text_stat(stats, 'time_kst')} KST on a clear day ({text_stat(stats, 'date')}).</p>
      <p>Map pixels are {num(stats, 'texture.pixel_size_m', 0)} m; the chart and fit use
      {num(stats, 'grid.cell_size_m', 0)} m cells. Terrain: Copernicus DEM GLO-30.
      Landsat Collection 2 Level-2 scene {stats['scene_id']} via Microsoft Planetary Computer.</p>"""


def scatter_note(stats):
    return (f"A random {num(stats, 'sampled_cells', 0, thousands=True)} of "
            f"{num(stats, 'grid.valid_cells', 0, thousands=True)} cells are drawn, coloured by LST; "
            f"the line is fitted to all of them.")


# ----------------------------------------------------------------------------
# Optional labels
# ----------------------------------------------------------------------------
def load_labels(stats):
    """Read inputs/labels.geojson (points with a 'name') and convert to terrain km coordinates."""
    if not LABELS.exists():
        print("no inputs/labels.geojson -- no place labels")
        return []
    from rasterio.warp import transform
    feats = json.loads(LABELS.read_text())["features"]
    lons = [f["geometry"]["coordinates"][0] for f in feats]
    lats = [f["geometry"]["coordinates"][1] for f in feats]
    xs, ys = transform("EPSG:4326", stats["grid"]["crs"], lons, lats)
    x0, y0, x1, y1 = stats["grid"]["bounds_utm"]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    labels = [{"name": f["properties"]["name"], "x_km": (x - cx) / 1000, "z_km": (cy - y) / 1000}
              for f, x, y in zip(feats, xs, ys)]
    print(f"{len(labels)} place label(s) from {LABELS.name}")
    return labels


# ----------------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------------
def b64(path):
    return base64.b64encode(path.read_bytes()).decode("ascii")


def lst_ramp():
    """RGB stops of the LST colour map, used to colour scatter dots in the browser."""
    cmap = matplotlib.colormaps["RdYlBu_r"]
    return [[int(round(c * 255)) for c in cmap(t)[:3]] for t in np.linspace(0, 1, 32)]


def main():
    stats = json.loads((DATA / "stats.json").read_text())
    cells = json.loads((DATA / "cells.json").read_text())
    payload = {"stats": stats, "cells": cells, "lstRamp": lst_ramp(), "labels": load_labels(stats)}

    title = "Vegetation and land surface temperature across Daegu"
    subtitle = (f"{platform_name(stats)} · {text_stat(stats, 'date')}, {text_stat(stats, 'time_kst')} KST · "
                f"NDVI and LST draped over terrain")

    legends = (legend_html(stats, "YlGn", "ndvi", "NDVI (vegetation index, unitless)", 2)
               + legend_html(stats, "RdYlBu_r", "lst_c", "Land surface temperature (°C)", 1))

    parts = {
        "@@TITLE@@": title,
        "@@SUBTITLE@@": subtitle,
        "@@SHEET_SUMMARY@@": sheet_summary(stats),
        "@@RELATIONSHIP@@": relationship_html(stats),
        "@@SCATTER_NOTE@@": scatter_note(stats),
        "@@LEGENDS@@": legends,
        "@@FOOTNOTE@@": footnote_html(stats),
        "@@DATA_JSON@@": json.dumps(payload, separators=(",", ":")),
        "@@LST_PNG@@": b64(DATA / "lst.png"),
        "@@NDVI_PNG@@": b64(DATA / "ndvi.png"),
        "@@HEIGHT_BIN@@": b64(DATA / "height.bin"),
        "@@GRID_BIN@@": b64(DATA / "grid_values.bin"),
        "@@THREE_JS@@": (VENDOR / "three.min.js").read_text(),
        "@@ORBIT_JS@@": (VENDOR / "OrbitControls.js").read_text(),
    }
    template = TEMPLATE.read_text()
    found = set(re.findall(r"@@[A-Z0-9_]+@@", template))
    assert found == set(parts), f"placeholder mismatch: {found ^ set(parts)}"
    # One pass, so text inside inserted libraries is never treated as a placeholder
    html = re.sub(r"@@[A-Z0-9_]+@@", lambda m: parts[m.group(0)], template)

    OUTPUT.write_text(html, encoding="utf-8")
    mb = OUTPUT.stat().st_size / 1e6
    print(f"wrote {OUTPUT.name}: {mb:.1f} MB")
    if mb > MAX_MB:
        raise SystemExit(f"Page is {mb:.1f} MB, over the {MAX_MB} MB limit")


if __name__ == "__main__":
    main()
