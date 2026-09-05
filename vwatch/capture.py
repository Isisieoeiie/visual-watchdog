"""Screenshot capture, hardened against the usual sources of false positives."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Browser, Error as PlaywrightError, Page as BrowserPage
from playwright.sync_api import sync_playwright

from .config import Config, Page, Viewport

log = logging.getLogger("vwatch.capture")

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)

LAUNCH_ARGS = (
    "--force-color-profile=srgb",
    "--font-render-hinting=none",
    "--disable-lcd-text",
    "--hide-scrollbars",
    "--disable-features=PaintHolding",
)

# Runs before any page script. A page that reads the clock or the RNG would
# otherwise render differently on every visit.
FREEZE_SCRIPT = """
(() => {
  try {
    const FIXED = Date.UTC(2026, 0, 1, 12, 0, 0);
    let seed = 0x2f6e2b1;
    Math.random = () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed / 0x7fffffff;
    };
    const Real = Date;
    class Frozen extends Real {
      constructor(...args) {
        super(...(args.length ? args : [FIXED]));
      }
      static now() { return FIXED; }
    }
    Frozen.UTC = Real.UTC;
    Frozen.parse = Real.parse;
    window.Date = Frozen;
  } catch (err) {
    /* a page that has already frozen these is fine to leave alone */
  }
})();
"""

# Animations are driven to their final frame rather than switched off: an
# element that fades in from opacity 0 stays invisible under `animation: none`.
STABILIZE_CSS = """
*, *::before, *::after {
  animation-delay: -1ms !important;
  animation-duration: 1ms !important;
  animation-iteration-count: 1 !important;
  animation-fill-mode: forwards !important;
  transition-duration: 0s !important;
  transition-delay: 0s !important;
  scroll-behavior: auto !important;
  caret-color: transparent !important;
}
"""

LAZY_LOAD_SCRIPT = """
async ({ stepWait, limitPx, budgetMs }) => {
  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
  const step = Math.max(200, Math.floor(window.innerHeight * 0.8));
  // performance.now() rather than Date.now(): the init script pins the clock.
  const started = performance.now();
  for (let offset = 0; offset <= limitPx; offset += step) {
    if (offset > document.documentElement.scrollHeight) break;
    if (performance.now() - started > budgetMs) break;
    window.scrollTo(0, offset);
    await sleep(stepWait);
  }
  window.scrollTo(0, 0);
  await sleep(stepWait);
  return document.documentElement.scrollHeight;
}
"""

# Both waits race against a deadline inside the page. A lazy image that never
# enters the viewport never fires load or error, and page.evaluate has no
# timeout of its own, so an un-raced promise hangs the whole run.
FONTS_SCRIPT = """
(budgetMs) => Promise.race([
  document.fonts.ready.then(() => 'ready'),
  new Promise((done) => setTimeout(() => done('timeout'), budgetMs)),
])
"""

PENDING_IMAGES_SCRIPT = """
(budgetMs) => {
  const pending = Array.from(document.images).filter((img) => !img.complete);
  const decoded = Promise.all(pending.map((img) => new Promise((done) => {
    img.addEventListener('load', done, { once: true });
    img.addEventListener('error', done, { once: true });
  })));
  return Promise.race([
    decoded.then(() => 'decoded'),
    new Promise((done) => setTimeout(() => done('timeout'), budgetMs)),
  ]);
}
"""


@dataclass(frozen=True)
class Shot:
    page: str
    viewport: str
    url: str
    filename: str
    width: int
    height: int
    captured_at: str


def _context_options(page_cfg: Page, viewport: Viewport) -> dict:
    return {
        "viewport": {"width": viewport.width, "height": viewport.height},
        "device_scale_factor": 1,
        "locale": "en-US",
        "timezone_id": "UTC",
        "color_scheme": "light",
        "reduced_motion": "reduce",
        "forced_colors": "none",
        "is_mobile": viewport.mobile,
        "has_touch": viewport.mobile,
        "user_agent": MOBILE_UA if viewport.mobile else DESKTOP_UA,
    }


def _quiet(action, description: str) -> None:
    """Run a settling step, tolerating pages that never fully go idle."""
    try:
        action()
    except PlaywrightError as exc:
        log.debug("%s did not complete: %s", description, str(exc).splitlines()[0])


def _settle(page: BrowserPage, page_cfg: Page) -> None:
    budget = page_cfg.timeout_ms // 3
    scroll_limit = page_cfg.max_height or 100_000

    _quiet(lambda: page.wait_for_load_state("networkidle", timeout=budget), "network idle")
    _quiet(lambda: page.evaluate(FONTS_SCRIPT, budget), "font loading")
    _quiet(
        lambda: page.evaluate(
            LAZY_LOAD_SCRIPT,
            {"stepWait": 120, "limitPx": scroll_limit, "budgetMs": budget},
        ),
        "lazy-load scroll",
    )
    _quiet(lambda: page.evaluate(PENDING_IMAGES_SCRIPT, budget), "image decode")


def _masks(page: BrowserPage, page_cfg: Page) -> list:
    resolved = []
    for selector in page_cfg.mask:
        try:
            locator = page.locator(selector)
            if locator.count():
                resolved.append(locator)
            else:
                log.warning(
                    "%s: mask selector matched nothing, consider removing it: %s",
                    page_cfg.name,
                    selector,
                )
        except PlaywrightError as exc:
            log.warning("%s: invalid mask selector %r (%s)", page_cfg.name, selector, exc)
    return resolved


def _capture_height(page_cfg: Page, document_height: int, slug: str) -> int:
    """How tall a slice to keep, honouring the max_height guard."""
    if page_cfg.max_height and document_height > page_cfg.max_height:
        log.info(
            "%s is %dpx tall, capturing the top %dpx (raise max_height to keep more)",
            slug,
            document_height,
            page_cfg.max_height,
        )
        return page_cfg.max_height
    return max(1, document_height)


def _capture_one(
    browser: Browser,
    page_cfg: Page,
    viewport: Viewport,
    out_dir: Path,
    inject_css: str | None,
) -> Shot:
    context = browser.new_context(**_context_options(page_cfg, viewport))
    context.add_init_script(FREEZE_SCRIPT)
    page = context.new_page()
    try:
        page.goto(page_cfg.url, wait_until="load", timeout=page_cfg.timeout_ms)
        if page_cfg.wait_for:
            page.wait_for_selector(page_cfg.wait_for, timeout=page_cfg.timeout_ms // 3)

        _settle(page, page_cfg)
        page.add_style_tag(content=STABILIZE_CSS)
        if inject_css:
            page.add_style_tag(content=inject_css)
        page.wait_for_timeout(page_cfg.settle_ms)

        slug = page_cfg.slug(viewport)
        filename = f"{slug}.png"
        document_height = int(page.evaluate("() => document.documentElement.scrollHeight"))

        if page_cfg.full_page:
            # Resize the viewport to the slice we want instead of passing
            # full_page=True. full_page expands the render surface to the whole
            # document -- tens of thousands of pixels on a docs page -- and the
            # screenshot times out even when a clip would discard most of it.
            # A tall viewport renders only what we keep.
            height = _capture_height(page_cfg, document_height, slug)
            if height != viewport.height:
                page.set_viewport_size({"width": viewport.width, "height": height})
                page.wait_for_timeout(page_cfg.settle_ms)
                _quiet(lambda: page.evaluate(PENDING_IMAGES_SCRIPT, page_cfg.timeout_ms // 3),
                       "image decode after resize")
            clip = {"x": 0, "y": 0, "width": viewport.width, "height": height}
        else:
            height = viewport.height
            clip = None

        page.screenshot(
            path=str(out_dir / filename),
            clip=clip,
            mask=_masks(page, page_cfg),
            animations="disabled",
            caret="hide",
            scale="css",
            timeout=page_cfg.timeout_ms,
        )
        log.info("captured %s (%dx%d)", filename, viewport.width, height)
        return Shot(
            page=page_cfg.name,
            viewport=viewport.name,
            url=page_cfg.url,
            filename=filename,
            width=viewport.width,
            height=int(height),
            captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    finally:
        context.close()


def capture(
    config: Config,
    only: tuple[str, ...] = (),
    inject_css: str | None = None,
    into: Path | None = None,
) -> list[Shot]:
    """Screenshot every configured page and viewport into `into` (default: snapshots/)."""
    out_dir = into or config.snapshot_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = config.find(only)

    if inject_css:
        log.warning("injecting extra CSS; these snapshots are for demos, not baselines")

    shots: list[Shot] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=list(LAUNCH_ARGS))
        try:
            for page_cfg in pages:
                for viewport in page_cfg.viewports:
                    try:
                        shots.append(
                            _capture_one(browser, page_cfg, viewport, out_dir, inject_css)
                        )
                    except PlaywrightError as exc:
                        log.error(
                            "failed to capture %s: %s",
                            page_cfg.slug(viewport),
                            str(exc).splitlines()[0],
                        )
        finally:
            browser.close()

    (out_dir / "manifest.json").write_text(
        json.dumps([asdict(shot) for shot in shots], indent=2), encoding="utf-8"
    )
    return shots
