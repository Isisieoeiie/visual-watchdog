"""Load, merge and validate the target configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_VIEWPORTS = (
    {"name": "mobile", "width": 390, "height": 844, "mobile": True},
    {"name": "desktop", "width": 1440, "height": 900, "mobile": False},
)

PAGE_DEFAULTS: dict[str, Any] = {
    "full_page": True,
    # Guard against documentation pages that run to tens of thousands of pixels:
    # Chromium takes minutes to stitch those, and SSIM over 50M pixels needs
    # gigabytes. 0 disables the cap.
    "max_height": 6000,
    "threshold": 0.995,
    "local_threshold": 0.85,
    "colour_tolerance": 24.0,
    "min_region": 120,
    "max_changed_fraction": 0.0002,
    "shift_tolerance": 0,
    "max_shift": 300,
    "settle_ms": 400,
    "timeout_ms": 45000,
    "wait_for": None,
    "mask": (),
}


class ConfigError(ValueError):
    """Raised when the config file is missing something or self-contradictory."""


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int
    mobile: bool = False


@dataclass(frozen=True)
class Page:
    name: str
    url: str
    viewports: tuple[Viewport, ...]
    mask: tuple[str, ...]
    full_page: bool
    max_height: int
    threshold: float
    local_threshold: float
    colour_tolerance: float
    min_region: int
    max_changed_fraction: float
    shift_tolerance: int
    max_shift: int
    settle_ms: int
    timeout_ms: int
    wait_for: str | None

    def slug(self, viewport: Viewport) -> str:
        return f"{self.name}__{viewport.name}"


@dataclass(frozen=True)
class Config:
    pages: tuple[Page, ...]
    root: Path

    @property
    def baseline_dir(self) -> Path:
        return self.root / "baselines"

    @property
    def snapshot_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def report_dir(self) -> Path:
        return self.root / "report"

    @property
    def publish_dir(self) -> Path:
        return self.root / "docs"

    def find(self, names: tuple[str, ...]) -> tuple[Page, ...]:
        if not names:
            return self.pages
        known = {page.name for page in self.pages}
        unknown = [name for name in names if name not in known]
        if unknown:
            raise ConfigError(
                f"no such page(s): {', '.join(unknown)}. Known: {', '.join(sorted(known))}"
            )
        return tuple(page for page in self.pages if page.name in names)


def _viewport(raw: Any, where: str) -> Viewport:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: each viewport must be a mapping, got {type(raw).__name__}")
    missing = {"name", "width", "height"} - raw.keys()
    if missing:
        raise ConfigError(f"{where}: viewport is missing {', '.join(sorted(missing))}")
    width, height = int(raw["width"]), int(raw["height"])
    if width < 1 or height < 1:
        raise ConfigError(f"{where}: viewport '{raw['name']}' has a non-positive size")
    return Viewport(str(raw["name"]), width, height, bool(raw.get("mobile", False)))


def _as_tuple(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    raise ConfigError(f"{where}: expected a string or list of strings")


def _page(raw: Any, defaults: dict[str, Any], index: int) -> Page:
    if not isinstance(raw, dict):
        raise ConfigError(f"pages[{index}]: must be a mapping, got {type(raw).__name__}")
    if "name" not in raw or "url" not in raw:
        raise ConfigError(f"pages[{index}]: needs both 'name' and 'url'")

    name = str(raw["name"])
    where = f"page '{name}'"
    url = str(raw["url"])
    if not url.startswith(("http://", "https://", "file://")):
        raise ConfigError(f"{where}: url must start with http://, https:// or file://")

    merged = {**defaults, **{k: v for k, v in raw.items() if v is not None}}

    viewports = tuple(
        _viewport(item, where) for item in merged.get("viewports") or DEFAULT_VIEWPORTS
    )
    if not viewports:
        raise ConfigError(f"{where}: needs at least one viewport")
    seen = [vp.name for vp in viewports]
    if len(set(seen)) != len(seen):
        raise ConfigError(f"{where}: duplicate viewport names")

    threshold = float(merged["threshold"])
    if not 0.0 < threshold <= 1.0:
        raise ConfigError(f"{where}: threshold must be between 0 and 1, got {threshold}")
    local_threshold = float(merged["local_threshold"])
    if not 0.0 < local_threshold <= 1.0:
        raise ConfigError(f"{where}: local_threshold must be between 0 and 1")
    colour_tolerance = float(merged["colour_tolerance"])
    if colour_tolerance < 0:
        raise ConfigError(f"{where}: colour_tolerance cannot be negative")
    max_changed_fraction = float(merged["max_changed_fraction"])
    if not 0.0 <= max_changed_fraction < 1.0:
        raise ConfigError(f"{where}: max_changed_fraction must be between 0 and 1")

    return Page(
        name=name,
        url=url,
        viewports=viewports,
        mask=_as_tuple(merged.get("mask"), where),
        full_page=bool(merged["full_page"]),
        max_height=max(0, int(merged["max_height"])),
        threshold=threshold,
        local_threshold=local_threshold,
        colour_tolerance=colour_tolerance,
        min_region=max(1, int(merged["min_region"])),
        max_changed_fraction=max_changed_fraction,
        shift_tolerance=max(0, int(merged["shift_tolerance"])),
        max_shift=max(0, int(merged["max_shift"])),
        settle_ms=max(0, int(merged["settle_ms"])),
        timeout_ms=max(1000, int(merged["timeout_ms"])),
        wait_for=merged["wait_for"],
    )


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    defaults = {**PAGE_DEFAULTS, **(raw.get("defaults") or {})}
    entries = raw.get("pages")
    if not entries:
        raise ConfigError(f"{path}: no 'pages' listed")

    pages = tuple(_page(entry, defaults, i) for i, entry in enumerate(entries))
    names = [page.name for page in pages]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise ConfigError(f"{path}: duplicate page names: {', '.join(sorted(duplicates))}")

    return Config(pages=pages, root=path.parent.resolve())
