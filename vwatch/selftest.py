"""Engine self-test: prove capture is deterministic and diffs fire on real change.

Runs entirely against the bundled fixture page, so it does not depend on
committed baselines or on matching another machine's renderer. That makes it
the check CI can require on every pull request.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from . import capture as capture_mod
from . import compare as compare_mod
from . import config as config_mod

log = logging.getLogger("vwatch.selftest")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_DIR = REPO_ROOT / "dev"
CHANGE_CSS = (DEV_DIR / "change.css").read_text(encoding="utf-8")
SHIFT_CSS = "h1 { margin-top: 30px !important; }"

CONFIG_TEXT = """
defaults:
  viewports:
    - name: desktop
      width: 1280
      height: 800
      mobile: false
  full_page: true
  threshold: 0.995
  settle_ms: 200

pages:
  - name: fixture
    url: {url}
    mask:
      - "#volatile"
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _serve(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    handler = partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True, name="vwatch-fixture")
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/fixture.html"


def _expect(result: compare_mod.Comparison, status: str, where: str) -> None:
    if result.status != status:
        raise AssertionError(
            f"{where}: expected {status}, got {result.status} "
            f"(SSIM {result.score:.4f}, {result.note or 'no note'})"
        )


def run() -> None:
    if not (DEV_DIR / "fixture.html").exists():
        raise FileNotFoundError(f"missing fixture page: {DEV_DIR / 'fixture.html'}")

    server, url = _serve(DEV_DIR)
    work = Path(tempfile.mkdtemp(prefix="vwatch-selftest-"))
    try:
        config_path = work / "selftest.yaml"
        config_path.write_text(CONFIG_TEXT.format(url=url), encoding="utf-8")
        config = config_mod.load(config_path)

        log.info("1/3 capture twice - must be identical")
        capture_mod.capture(config)
        shutil.copytree(config.snapshot_dir, config.baseline_dir)
        capture_mod.capture(config)
        identical = compare_mod.compare(config)
        if len(identical) != 1:
            raise AssertionError(f"expected 1 comparison, got {len(identical)}")
        _expect(identical[0], compare_mod.PASS, "determinism")
        if identical[0].score < 1.0:
            raise AssertionError(
                f"determinism: SSIM {identical[0].score:.4f} — the freeze layer leaked"
            )
        log.info("   PASS  SSIM %.4f", identical[0].score)

        log.info("2/3 recolour the button - must report CHANGED")
        capture_mod.capture(config, inject_css=CHANGE_CSS)
        recoloured = compare_mod.compare(config)
        _expect(recoloured[0], compare_mod.CHANGED, "recolour")
        log.info("   CHANGED  %s", recoloured[0].note or "ok")

        log.info("3/3 nudge the heading down - must report SHIFTED, not a rewrite")
        capture_mod.capture(config, inject_css=SHIFT_CSS)
        shifted = compare_mod.compare(config)
        _expect(shifted[0], compare_mod.SHIFTED, "shift")
        if abs(shifted[0].shift) < 20:
            raise AssertionError(
                f"shift: recovered {shifted[0].shift}px, expected ~30px"
            )
        log.info("   SHIFTED  %s", shifted[0].note)

        log.info("self-test passed")
    finally:
        server.shutdown()
        shutil.rmtree(work, ignore_errors=True)
