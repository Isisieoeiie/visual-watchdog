"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from . import __version__, capture as capture_mod, compare as compare_mod, config as config_mod
from . import report as report_mod
from . import selftest as selftest_mod

EXIT_OK = 0
EXIT_CHANGES = 1
EXIT_ERROR = 2

log = logging.getLogger("vwatch")


def _read_css(value: str | None) -> str | None:
    """Accept CSS inline, or as @path to read it from a file."""
    if not value:
        return None
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def _summarise(results: list[compare_mod.Comparison]) -> None:
    for item in results:
        detail = f"SSIM {item.score:.4f}, {item.changed_fraction * 100:.2f}% changed"
        if item.note:
            detail = f"{detail} ({item.note})" if item.status != compare_mod.NEW else item.note
        log.info("%-9s %s - %s", item.status.upper(), item.slug, detail)


def _do_capture(config: config_mod.Config, args: argparse.Namespace) -> int:
    shots = capture_mod.capture(
        config,
        only=tuple(args.only),
        inject_css=_read_css(args.inject_css),
        into=Path(args.into) if args.into else None,
    )
    if not shots:
        log.error("nothing was captured")
        return EXIT_ERROR
    log.info("captured %d screenshot(s) into %s", len(shots), config.snapshot_dir)
    return EXIT_OK


def _do_compare(config: config_mod.Config, args: argparse.Namespace) -> int:
    results = compare_mod.compare(config, only=tuple(args.only))
    if not results:
        log.error("nothing to compare")
        return EXIT_ERROR

    _summarise(results)
    index = report_mod.write_report(config, results)
    log.info("report written to %s", index)
    summary = report_mod.write_summary(config, results)

    # When running under GitHub Actions, surface the table on the run's page.
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as handle:
                handle.write(summary.read_text(encoding="utf-8") + "\n")
        except OSError as exc:
            log.debug("could not write GITHUB_STEP_SUMMARY: %s", exc)

    failures = [item for item in results if item.failed]
    if failures:
        log.warning("%d of %d target(s) need review", len(failures), len(results))
        return EXIT_CHANGES
    return EXIT_OK


def _do_run(config: config_mod.Config, args: argparse.Namespace) -> int:
    captured = _do_capture(config, args)
    if captured != EXIT_OK:
        return captured
    return _do_compare(config, args)


def _do_approve(config: config_mod.Config, args: argparse.Namespace) -> int:
    config.baseline_dir.mkdir(parents=True, exist_ok=True)
    promoted = 0
    for page_cfg in config.find(tuple(args.only)):
        for viewport in page_cfg.viewports:
            snapshot = config.snapshot_dir / f"{page_cfg.slug(viewport)}.png"
            if not snapshot.exists():
                log.warning("no snapshot for %s, skipping", page_cfg.slug(viewport))
                continue
            shutil.copy2(snapshot, config.baseline_dir / snapshot.name)
            log.info("baseline updated: %s", snapshot.name)
            promoted += 1

    if not promoted:
        log.error("no snapshots to promote; run capture first")
        return EXIT_ERROR
    log.info("promoted %d baseline(s); commit %s to keep them", promoted, config.baseline_dir)
    return EXIT_OK


def _do_publish(config: config_mod.Config, _: argparse.Namespace) -> int:
    target = report_mod.publish(config)
    log.info("report copied to %s; commit it and point GitHub Pages at /docs", target)
    return EXIT_OK


def _do_selftest(_config: config_mod.Config | None, _: argparse.Namespace) -> int:
    try:
        selftest_mod.run()
    except AssertionError as exc:
        log.error("self-test failed: %s", exc)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        log.exception("self-test crashed: %s", exc)
        return EXIT_ERROR
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vwatch",
        description="Screenshot web pages and report visual changes against stored baselines.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c", "--config", default="targets.yaml", help="path to the target list (targets.yaml)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")

    subcommands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sub = subcommands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler, only=[], inject_css=None, into=None)
        sub.add_argument(
            "--only",
            nargs="+",
            default=[],
            metavar="PAGE",
            help="limit to these page names",
        )
        return sub

    capture_cmd = add("capture", _do_capture, "take fresh screenshots")
    capture_cmd.add_argument(
        "--inject-css",
        metavar="CSS",
        help="extra CSS to apply before the screenshot, or @file to read it "
        "from disk; use it to demo a change without editing the site",
    )
    capture_cmd.add_argument("--into", metavar="DIR", help="write screenshots here instead")

    add("compare", _do_compare, "diff the latest screenshots against the baselines")

    run_cmd = add("run", _do_run, "capture then compare")
    run_cmd.add_argument("--inject-css", metavar="CSS", help="see 'capture --inject-css'")
    run_cmd.add_argument("--into", metavar="DIR", help="write screenshots here instead")

    add("approve", _do_approve, "promote the latest screenshots to be the new baselines")
    add("publish", _do_publish, "copy the report into docs/ for GitHub Pages")

    selftest_cmd = subcommands.add_parser(
        "selftest",
        help="prove determinism and detection against the bundled fixture",
    )
    selftest_cmd.set_defaults(handler=_do_selftest, only=[], inject_css=None, into=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    try:
        if args.command == "selftest":
            return _do_selftest(None, args)
        config = config_mod.load(Path(args.config))
        return args.handler(config, args)
    except config_mod.ConfigError as exc:
        log.error("config problem: %s", exc)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.error("interrupted")
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
