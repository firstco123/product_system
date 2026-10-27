"""Measure the admin screens' layout in a real browser, at phone to desktop widths.

Written for the 2026-10-03 responsive review, which found a yen total spilling
out of its tile, table columns squeezed to one character per line, and product
titles wrapping into seventeen lines. Screenshots catch those only when a person
looks at the right width; this measures every screen at every width and fails
on each of them:

* page-level horizontal overflow (tables scroll inside their own frame);
* a KPI value wider than its tile;
* any text in a table cell that wrapped onto a second line, except cells marked
  `wrap` — that is how 「ブレスレット」 ended up one character per line;
* a row whose detail panel does not open, or opens without its content.

READ ONLY. It loads pages and opens/closes panels in the browser; it never
submits a form, so it is safe against production after a deploy.

    py scripts/check_layout.py --base-url http://127.0.0.1:8099 --out .local/layout
    (credentials from ADMIN_USERNAME / ADMIN_PASSWORD)

Uses the installed Google Chrome through Playwright (`channel="chrome"`).
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

WIDTHS = (375, 425, 768, 1024, 1280)

PAGES = (
    ("overview", "/admin/analytics?period=28d"),
    ("overview-365d", "/admin/analytics?period=365d"),
    ("sales", "/admin/analytics/sales?period=28d"),
    ("stockout", "/admin/analytics/stockout-risk"),
    ("categories", "/admin/analytics/categories"),
    ("inventory", "/admin/inventory"),
    ("alerts", "/admin/alerts"),
    ("events", "/admin/events"),
    ("sync-errors", "/admin/sync-errors"),
    ("rakuten-report", "/admin/data-quality/rakuten"),
    ("mappings", "/admin/mappings"),
    ("dq-no-category", "/admin/data-quality/skus?kind=no_category"),
    ("dq-bundles", "/admin/data-quality/skus?kind=bundles"),
    ("inventory-search", "/admin/inventory?q=N108"),
    ("sales-search", "/admin/analytics/sales?period=28d&q=N108"),
    ("wholesale", "/admin/wholesale"),
    ("wholesale-upload", "/admin/wholesale/upload"),
)

#: NOT reachable from here: /admin/wholesale/preview renders only as the response
#: to a file upload, and this script never submits a form (see the module
#: docstring — that is what makes it safe to point at production). It is the
#: wholesale screen with the widest table, so its layout is the one checked by
#: eye rather than measured.

#: Returns a list of problem strings for the page as currently laid out.
MEASURE = """
() => {
  const problems = [];
  const visible = (el) => !!(el.offsetParent || el.getClientRects().length);
  const lines = (node) => {
    const r = document.createRange();
    r.selectNodeContents(node);
    const tops = [];
    for (const rect of r.getClientRects()) {
      if (rect.width < 1) continue;
      if (!tops.some((t) => Math.abs(t - rect.top) < 4)) tops.push(rect.top);
    }
    return tops.length;
  };

  const doc = document.documentElement;
  if (doc.scrollWidth > doc.clientWidth + 1) {
    problems.push(`page scrolls sideways: ${doc.scrollWidth}px content in ${doc.clientWidth}px`);
  }

  // Every number in every tile, whatever its classes. A block keeps its own box
  // inside the tile while its CONTENT spills out, so a block is measured by
  // scrollWidth; an inline number by where its right edge lands.
  document.querySelectorAll('.grid .rounded-xl .tabular-nums').forEach((v) => {
    const tile = v.closest('.rounded-xl');
    // Inside a table's own scrolling frame, reaching past the card is scrolling.
    if (!tile || !visible(v) || v.closest('.scroll-area')) return;
    const over = getComputedStyle(v).display === 'block'
      ? v.scrollWidth - v.clientWidth
      : v.getBoundingClientRect().right - tile.getBoundingClientRect().right;
    if (over > 1) {
      const value = v.textContent.trim();
      problems.push(`KPI value 「${value}」 overflows its tile by ${Math.ceil(over)}px`);
    }
  });

  // A short label — 「セット」「未対応」「十分」 — never has a reason to wrap,
  // wherever it is. Caught outside tables too: a badge in a search-result chip
  // broke 「セッ/ト」 at 375px (2026-10-12).
  const walkerAll = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let t = walkerAll.nextNode(); t; t = walkerAll.nextNode()) {
    const text = t.textContent.trim();
    const el = t.parentElement;
    if (!text || text.length > 6 || !el) continue;
    if (el.closest('template, pre, .wrap, #detailPanel')) continue;
    if (!visible(el)) continue;
    // Bold words inside a sentence wrap with the sentence; that is prose.
    const host = el.parentElement;
    if (getComputedStyle(el).display === 'inline' && host
        && host.textContent.trim().length > text.length + 2) continue;
    if (lines(t) > 1) problems.push(`short label wrapped: 「${text}」`);
  }

  document.querySelectorAll('table.data-table th, table.data-table td').forEach((cell) => {
    if (!visible(cell) || cell.classList.contains('wrap')) return;
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      if (!t.textContent.trim() || t.parentElement.closest('template, pre')) continue;
      if (lines(t) > 1) {
        problems.push(`wrapped in a table cell: 「${t.textContent.trim().slice(0, 30)}」`);
        break;
      }
    }
  });
  return problems;
}
"""

PANEL = """
async () => {
  const row = [...document.querySelectorAll('[data-detail]')].find(
    (el) => el.offsetParent || el.getClientRects().length);
  if (!row) return 'no clickable row on this page';
  const tpl = document.getElementById(row.dataset.detail);
  if (!tpl) return `row ${row.dataset.detail} has no panel template`;
  row.click();
  const panel = document.getElementById('detailPanel');
  if (panel.classList.contains('hidden')) return 'panel did not open on row click';
  const want = tpl.content.textContent.replace(/\\s+/g, ' ').trim().slice(0, 40);
  const got = document.getElementById('detailBody').textContent.replace(/\\s+/g, ' ').trim();
  if (!got.includes(want)) return 'panel opened without the row content';
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  if (!panel.classList.contains('hidden')) return 'Escape did not close the panel';
  return null;
}
"""


def measure(page: Page, url: str) -> list[str]:
    """Problems on one page at the current width. A page that fails to load is
    one FAIL line, not the end of the run."""
    try:
        response = page.goto(url, wait_until="load", timeout=45000)
    except PlaywrightError as exc:
        return [f"did not load: {str(exc).splitlines()[0][:80]}"]
    # Tailwind's CDN build styles the page from a script; give it a beat.
    page.wait_for_timeout(400)
    status = response.status if response else 0
    if status != 200:
        return [f"HTTP {status}"]
    problems = list(page.evaluate(MEASURE))
    if page.locator("[data-detail]").count():
        panel = page.evaluate(PANEL)
        if panel:
            problems.append(panel)
    return problems


def check(page: Page, base: str, out: Path | None) -> int:
    failures = 0
    for slug, path in PAGES:
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            problems = measure(page, base + path)
            if out is not None and not any(p.startswith("did not load") for p in problems):
                page.screenshot(path=str(out / f"{slug}_{width}.png"), full_page=True)
            mark = "PASS" if not problems else "FAIL"
            failures += bool(problems)
            print(f"{mark}  {slug:<15}{width:>5}px  {'; '.join(problems[:4])}", flush=True)
    return failures


def authenticate(context: BrowserContext, base: str, user: str, password: str) -> None:
    """Basic auth on requests to OUR host only.

    Not `http_credentials`: against Cloud Run that never completed a navigation
    (timed out at 30s on the first page, 2026-10-03), while the same request
    with the header set loaded in 1.3s. Not `extra_http_headers` either — that
    would send the admin password to the Tailwind CDN and Google Fonts too.
    """
    token = base64.b64encode(f"{user}:{password}".encode()).decode()

    def add_header(route: Route) -> None:
        route.continue_(headers={**route.request.headers, "authorization": f"Basic {token}"})

    context.route(f"{base}/**", add_header)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", required=True)
    p.add_argument("--out", default=None, help="directory for full-page screenshots")
    args = p.parse_args()
    out = Path(args.out) if args.out else None
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
    user = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ["ADMIN_PASSWORD"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        base = args.base_url.rstrip("/")
        context = browser.new_context()
        authenticate(context, base, user, password)
        failures = check(context.new_page(), base, out)
        browser.close()
    verdict = "all screens pass" if not failures else f"{failures} screen/width combinations fail"
    print(f"\n{verdict}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
