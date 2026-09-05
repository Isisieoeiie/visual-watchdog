"""Render a self-contained HTML report from a set of comparisons."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .compare import CHANGED, ERROR, NEW, PASS, SHIFTED, Comparison
from .config import Config

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
ASSET_DIR = "assets"

STATUS_EMOJI = {PASS: "🟢", SHIFTED: "🟡", CHANGED: "🔴", NEW: "🔵", ERROR: "🟣"}


def _counts(comparisons: list[Comparison]) -> dict[str, int]:
    return {
        "unchanged": sum(1 for item in comparisons if item.status == PASS),
        "shifted": sum(1 for item in comparisons if item.status == SHIFTED),
        "changed": sum(1 for item in comparisons if item.status == CHANGED),
        "new": sum(1 for item in comparisons if item.status == NEW),
        "error": sum(1 for item in comparisons if item.status == ERROR),
    }


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _copy_asset(source: str | None, destination: Path, name: str) -> str | None:
    if not source:
        return None
    origin = Path(source)
    if not origin.exists():
        return None
    target = destination / name
    shutil.copy2(origin, target)
    return f"{ASSET_DIR}/{name}"


def _relative_to_report(source: str | None, report_dir: Path) -> str | None:
    if not source:
        return None
    path = Path(source)
    try:
        return path.resolve().relative_to(report_dir.resolve()).as_posix()
    except ValueError:
        return None


def write_report(config: Config, comparisons: list[Comparison]) -> Path:
    report_dir = config.report_dir
    asset_dir = report_dir / ASSET_DIR
    asset_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for item in comparisons:
        entries.append(
            {
                "page": item.page,
                "viewport": item.viewport,
                "url": item.url,
                "status": item.status,
                "score": item.score,
                "changed_percent": item.changed_fraction * 100.0,
                "shift": item.shift,
                "height_delta": item.height_delta,
                "note": item.note,
                "region_count": len(item.boxes),
                "baseline": _copy_asset(
                    item.baseline, asset_dir, f"{item.slug}__baseline.png"
                ),
                "current": _copy_asset(item.current, asset_dir, f"{item.slug}__current.png"),
                "diff": _relative_to_report(item.diff, report_dir),
            }
        )

    counts = _counts(comparisons)

    html = _environment().get_template("report.html.j2").render(
        entries=entries,
        counts=counts,
        total=len(comparisons),
        failures=sum(1 for item in comparisons if item.failed),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    index = report_dir / "index.html"
    index.write_text(html, encoding="utf-8")
    return index


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def render_summary(comparisons: list[Comparison]) -> str:
    """A GitHub-flavoured markdown summary, for PR comments and job summaries."""
    total = len(comparisons)
    failures = sum(1 for item in comparisons if item.failed)
    counts = _counts(comparisons)

    headline = "all clear" if not failures else f"{failures} of {total} need review"
    lines = [
        f"### Visual watchdog — {headline}",
        "",
        (
            f"🟢 {counts['unchanged']} unchanged · 🟡 {counts['shifted']} shifted · "
            f"🔴 {counts['changed']} changed · 🔵 {counts['new']} new · "
            f"🟣 {counts['error']} error"
        ),
        "",
        "| | Page | Viewport | SSIM | Changed | Notes |",
        "| :-- | :-- | :-- | --: | --: | :-- |",
    ]
    for item in comparisons:
        emoji = STATUS_EMOJI.get(item.status, "")
        lines.append(
            f"| {emoji} {item.status} | {_md_escape(item.page)} | {item.viewport} "
            f"| {item.score:.4f} | {item.changed_fraction * 100:.2f}% "
            f"| {_md_escape(item.note)} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_summary(config: Config, comparisons: list[Comparison]) -> Path:
    config.report_dir.mkdir(parents=True, exist_ok=True)
    path = config.report_dir / "summary.md"
    path.write_text(render_summary(comparisons), encoding="utf-8")
    return path


def publish(config: Config) -> Path:
    """Copy the report into docs/ so GitHub Pages can serve it from the repo."""
    if not (config.report_dir / "index.html").exists():
        raise FileNotFoundError(
            f"no report at {config.report_dir / 'index.html'}; run compare first"
        )

    target = config.publish_dir
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(config.report_dir, target)
    (target / ".nojekyll").write_text("", encoding="utf-8")
    return target
