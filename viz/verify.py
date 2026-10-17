"""
verify.py -- open viz/daegu_summer_heat.html in headless Chromium and check it.

Checks:
  * no console errors or uncaught exceptions
  * no network requests besides the HTML file itself
  * every number tagged data-stat on the page equals the value in stats.json
  * page size under 15 MB
Screenshots (viz/screenshots/): desktop 1440x900 and phone 390x844 with the
swipe divider at 30 % and 70 % of the map width, plus an active scatter brush and a circled area.

Usage:
    .venv/bin/python viz/verify.py
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------------
# Tunable values
# ----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
PAGE = HERE / "daegu_summer_heat.html"
STATS = HERE / "data" / "stats.json"
SHOTS = HERE / "screenshots"
VIEWPORTS = {"desktop": (1440, 900), "phone": (390, 844)}
SPLITS = [0.30, 0.70]
BRUSH = {"ndvi": (0.10, 0.40), "lst_c": (40.0, 50.0)}   # hot, sparsely vegetated corner
MAX_MB = 15
GPU_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


def lookup(stats, path):
    node = stats
    for part in path.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def check_numbers(page, stats):
    """Compare every data-stat span with stats.json; return a list of problems."""
    spans = page.eval_on_selector_all(
        "[data-stat]",
        "els => els.map(e => ({path: e.dataset.stat, text: e.textContent.trim(), dp: e.dataset.dp,"
        " scale: e.dataset.scale, abs: e.dataset.abs, isText: e.dataset.text}))")
    problems = []
    for s in spans:
        expected = lookup(stats, s["path"])
        if s["isText"]:
            ok = s["text"] == str(expected)
        else:
            value = expected * float(s["scale"] or 1)
            if s["abs"]:
                value = abs(value)
            shown = float(s["text"].replace("−", "-").replace(",", ""))
            ok = round(value, int(s["dp"])) == shown
        if not ok:
            problems.append(f"{s['path']}: page shows {s['text']!r}, stats.json has {expected!r}")
    return len(spans), problems


def open_page(browser, size, log):
    """New page at the given viewport, with console/network listeners attached."""
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1)
    page = ctx.new_page()
    url = PAGE.resolve().as_uri()
    page.on("console", lambda m: m.type == "error" and log["console"].append(m.text))
    page.on("pageerror", lambda e: log["console"].append(str(e)))
    page.on("request", lambda r: r.url != url and log["requests"].append(r.url))
    page.goto(url)
    page.wait_for_function("window.__viz && window.__viz.ready", timeout=60000)
    page.evaluate("window.__viz.stopAuto()")
    page.wait_for_timeout(800)
    return page


def drag_divider(page, fraction):
    """Drag the swipe knob with the mouse to the given share of the window width."""
    box = page.locator("#swipe .knob").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    width = page.evaluate("window.__viz.mapWidth()")   # map area left of the side panel
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(fraction * width, y, steps=12)
    page.mouse.up()
    page.wait_for_timeout(300)


def brush_scatter(page):
    """Drag a rectangle on the scatter plot with the mouse."""
    (x0, y0) = page.evaluate(f"window.__viz.scatterPx({BRUSH['ndvi'][0]}, {BRUSH['lst_c'][1]})")
    (x1, y1) = page.evaluate(f"window.__viz.scatterPx({BRUSH['ndvi'][1]}, {BRUSH['lst_c'][0]})")
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=10)
    page.mouse.up()
    page.wait_for_timeout(400)
    return page.evaluate("window.__viz.selectionText()")


def circle_terrain(page):
    """Shift-drag a circle on the terrain near the middle of the 3D view."""
    w, h = page.viewport_size["width"], page.viewport_size["height"]
    cx, cy = w * 0.22, h * 0.55       # well left of the divider (kept at 50 %)
    page.keyboard.down("Shift")
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 90, cy, steps=10)
    page.mouse.up()
    page.keyboard.up("Shift")
    page.wait_for_timeout(400)
    return page.evaluate("window.__viz.selectionText()")


def main():
    SHOTS.mkdir(exist_ok=True)
    stats = json.loads(STATS.read_text())
    failures = []
    mb = PAGE.stat().st_size / 1e6
    print(f"page size {mb:.1f} MB")
    if mb > MAX_MB:
        failures.append(f"page is {mb:.1f} MB")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=GPU_ARGS)
        for name, size in VIEWPORTS.items():
            log = {"console": [], "requests": []}
            page = open_page(browser, size, log)

            n, problems = check_numbers(page, stats)
            print(f"[{name}] {n} numbers checked against stats.json, {len(problems)} mismatch(es)")
            failures += [f"[{name}] {m}" for m in problems]

            for f in SPLITS:
                drag_divider(page, f)
                shown = page.evaluate("window.__viz.getSplit()") * 100
                if abs(shown - f * 100) > 1.5:
                    failures.append(f"[{name}] divider at {shown:.1f}% instead of {f * 100:.0f}%")
                path = SHOTS / f"{name}_swipe{int(f * 100)}.png"
                page.screenshot(path=str(path))
                print(f"[{name}] saved {path.name}")

            if name == "phone":
                page.click("#sheet-toggle")
                page.wait_for_timeout(500)
                page.screenshot(path=str(SHOTS / "phone_sheet_open.png"))
                print("[phone] saved phone_sheet_open.png")
            drag_divider(page, 0.5)
            sel = brush_scatter(page)
            print(f"[{name}] brush -> {sel!r}")
            if not sel.startswith("Brushed range"):
                failures.append(f"[{name}] brush did not select anything")
            page.screenshot(path=str(SHOTS / f"{name}_brush.png"))
            print(f"[{name}] saved {name}_brush.png")

            if name == "desktop":
                page.keyboard.press("Escape")
                drag_divider(page, 0.5)
                sel = circle_terrain(page)
                print(f"[{name}] circle -> {sel!r}")
                if not sel.startswith("Circled area"):
                    failures.append("[desktop] terrain circle did not select anything")
                page.screenshot(path=str(SHOTS / "desktop_circle.png"))
                print("[desktop] saved desktop_circle.png")

            print(f"[{name}] console errors: {len(log['console'])}, network requests: {len(log['requests'])}")
            failures += [f"[{name}] console: {m}" for m in log["console"]]
            failures += [f"[{name}] request: {u[:120]}" for u in log["requests"]]
            page.context.close()
        browser.close()

    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
