"""Capture a PNG of the bootstrap'd Superset dashboard for the README.

The Superset thumbnail API requires Celery + Redis + a Selenium worker
which the demo doesn't run. Instead, use a headless Chromium via
Playwright to log in to the demo Superset, navigate to the dashboard,
wait for the chart to render, and write the screenshot to
docs/superset_dashboard.png so the README can embed it as a static
image.

Run after `docker compose up -d cube superset` and after the
bootstrap has finished (watch the superset logs for "bootstrap: all
done"). The script is idempotent — re-run any time the dashboard
changes:

    uv run --no-project --with playwright python3 superset/screenshot.py

Requires Playwright's Chromium browser, which the Playwright Python
package downloads on first use:

    uv run --no-project --with playwright python3 -m playwright install chromium

(The bundle is ~150 MB but the cache is shared across projects.)
"""
from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SUPERSET_URL = "http://localhost:8088"
DASHBOARD_SLUG = "mcp-toolbox-cube-demo"
USERNAME = "admin"
PASSWORD = "admin"

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "superset_dashboard.png"


def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                viewport={"width": 1400, "height": 560},
                device_scale_factor=2,  # crisp on retina
            )
            page = ctx.new_page()

            page.goto(f"{SUPERSET_URL}/login/", wait_until="networkidle")
            page.fill('input[name="username"]', USERNAME)
            page.fill('input[name="password"]', PASSWORD)
            page.click('input[type="submit"]')
            page.wait_for_url(lambda url: "/login" not in url, timeout=30_000)

            # standalone=2 strips Superset chrome (top nav + edit bar)
            # so the screenshot is just the dashboard body.
            page.goto(
                f"{SUPERSET_URL}/superset/dashboard/{DASHBOARD_SLUG}/?standalone=2",
                wait_until="networkidle",
            )
            # Wait for at least one bar in the dist_bar viz to render.
            # dist_bar uses d3 + SVG; the bars are <rect> elements
            # inside the chart container.
            try:
                page.wait_for_selector(
                    ".chart-container svg .nv-bar, .chart-container svg rect.bar, "
                    ".chart-container .nv-distributionBars rect",
                    timeout=30_000,
                )
            except PlaywrightTimeoutError:
                # As a fallback, just confirm the chart container is
                # not in its error state (a banner with role="alert").
                if page.locator('.chart-container [role="alert"]').count() > 0:
                    print(
                        "screenshot: chart panel rendered an alert — "
                        "the screenshot may show an error banner.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "screenshot: bar elements didn't render in 30s. "
                        "Capturing anyway.",
                        file=sys.stderr,
                    )
            # A short settle so the bar geometry + value labels finish drawing.
            page.wait_for_timeout(2000)

            # Screenshot just the chart card so the README image
            # isn't padded with empty grid background. Try a few
            # selectors that Superset uses for the chart holder and
            # fall back to a viewport capture.
            for selector in [
                ".dashboard-component-chart-holder",
                ".chart-container",
                ".grid-content",
                ".dashboard-content",
            ]:
                loc = page.locator(selector).first
                if loc.count() > 0:
                    loc.screenshot(path=str(OUTPUT_PATH))
                    break
            else:
                page.screenshot(path=str(OUTPUT_PATH), full_page=False)
            print(f"screenshot: wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
