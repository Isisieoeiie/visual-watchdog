"""Perceptual comparison of snapshots against baselines."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from skimage.measure import label, regionprops
from skimage.metrics import structural_similarity
from skimage.morphology import closing, disk, remove_small_objects

from .config import Config, Page, Viewport

log = logging.getLogger("vwatch.compare")

PASS = "pass"
SHIFTED = "shifted"
CHANGED = "changed"
NEW = "new"
ERROR = "error"

STATUS_ORDER = {ERROR: 0, CHANGED: 1, SHIFTED: 2, NEW: 3, PASS: 4}

# float32 throughout: a full-page shot of a long docs page runs to tens of
# millions of pixels, and SSIM allocates several intermediates of the same shape.
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
TINT = np.array([255.0, 40.0, 70.0], dtype=np.float32)
BOX_COLOUR = (255, 205, 0)
MAX_BOXES = 8

# Aligning has to buy at least this much SSIM before we believe a difference is
# a shift rather than a genuine change.
ALIGN_GAIN = 0.02
# SSIM needs an 11x11 window once gaussian weights are on.
MIN_SIDE = 11
# Changed pixels within this radius are treated as one region, so recolouring a
# line of text reads as one finding instead of one per glyph.
CLOSE_RADIUS = 3


@dataclass
class Comparison:
    page: str
    viewport: str
    url: str
    status: str
    score: float = 1.0
    changed_fraction: float = 0.0
    shift: int = 0
    height_delta: int = 0
    boxes: list[tuple[int, int, int, int]] = field(default_factory=list)
    baseline: str | None = None
    current: str | None = None
    diff: str | None = None
    note: str = ""

    @property
    def slug(self) -> str:
        return f"{self.page}__{self.viewport}"

    @property
    def failed(self) -> bool:
        return self.status in {CHANGED, SHIFTED, ERROR}

    @property
    def sort_key(self) -> tuple[int, float, str]:
        return (STATUS_ORDER.get(self.status, 9), -self.changed_fraction, self.slug)


def _load(path: Path) -> np.ndarray:
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"))


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb.astype(np.float32) @ LUMA


def _pad_below(rgb: np.ndarray, height: int, fill: int = 255) -> np.ndarray:
    if rgb.shape[0] >= height:
        return rgb
    padding = np.full((height - rgb.shape[0], *rgb.shape[1:]), fill, dtype=rgb.dtype)
    return np.concatenate([rgb, padding], axis=0)


def _ssim(a: np.ndarray, b: np.ndarray) -> tuple[float, np.ndarray]:
    score, detail = structural_similarity(
        a,
        b,
        data_range=255.0,
        gaussian_weights=True,
        sigma=1.5,
        use_sample_covariance=False,
        full=True,
    )
    return float(score), detail


def _overlap(a: np.ndarray, b: np.ndarray, shift: int) -> tuple[np.ndarray, np.ndarray]:
    """Crop both arrays so that a[i] lines up with b[i + shift]."""
    if shift >= 0:
        return a[: a.shape[0] - shift], b[shift:]
    return a[-shift:], b[: b.shape[0] + shift]


def detect_vertical_shift(
    baseline: np.ndarray, current: np.ndarray, max_shift: int
) -> tuple[int, float]:
    """Find the offset d where baseline row i best matches current row i + d.

    A page whose header grows a few pixels moves everything below it, which a
    pixel-counting diff reports as a near-total change. Correlating the rows'
    brightness profiles recovers that offset cheaply.
    """
    limit = min(max_shift, baseline.shape[0] // 4)
    if limit < 1:
        return 0, 1.0

    profile_a = baseline.mean(axis=1)
    profile_b = current.mean(axis=1)
    profile_a = profile_a - profile_a.mean()
    profile_b = profile_b - profile_b.mean()

    best_shift, best_score = 0, -np.inf
    for shift in range(-limit, limit + 1):
        left, right = _overlap(profile_a, profile_b, shift)
        if left.size < baseline.shape[0] * 0.5:
            continue
        norm = np.linalg.norm(left) * np.linalg.norm(right)
        if norm == 0:
            continue
        score = float(left @ right / norm)
        if score > best_score:
            best_shift, best_score = shift, score
    return best_shift, best_score


def _difference_mask(
    detail: np.ndarray,
    baseline_rgb: np.ndarray,
    current_rgb: np.ndarray,
    page_cfg: Page,
) -> np.ndarray:
    """Flag pixels that differ structurally or in colour.

    SSIM alone runs on brightness, so a recolour that preserves luminance --
    blue to red, for instance -- barely registers. The colour distance covers
    that; SSIM covers layout and texture the colour distance would miss.
    """
    structural = detail < page_cfg.local_threshold
    distance = np.linalg.norm(
        baseline_rgb.astype(np.float32) - current_rgb.astype(np.float32), axis=2
    )
    mask = structural | (distance > page_cfg.colour_tolerance)
    if not mask.any():
        return mask
    mask = closing(mask, disk(CLOSE_RADIUS)).astype(bool)
    # max_size drops blobs of that size or smaller, so subtract one to keep
    # min_region meaning "the smallest blob still worth reporting".
    return remove_small_objects(mask, max_size=page_cfg.min_region - 1)


def _boxes(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    if not mask.any():
        return []
    found = sorted(regionprops(label(mask)), key=lambda region: region.area, reverse=True)
    return [tuple(int(value) for value in region.bbox) for region in found[:MAX_BOXES]]


def _render_diff(
    current: np.ndarray,
    mask: np.ndarray,
    boxes: list[tuple[int, int, int, int]],
    row_offset: int,
    out_path: Path,
) -> None:
    height, width = current.shape[:2]
    aligned = np.zeros((height, width), dtype=bool)
    usable = min(mask.shape[0], height - row_offset)
    if usable > 0:
        aligned[row_offset : row_offset + usable, : mask.shape[1]] = mask[:usable]

    base = current.astype(np.float32)
    grey = np.repeat((base @ LUMA)[..., None], 3, axis=2)
    faded = grey * 0.5 + 127.0
    hot = base * 0.35 + TINT * 0.65
    canvas = np.where(aligned[..., None], hot, faded)

    image = Image.fromarray(canvas.clip(0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(image)
    for min_row, min_col, max_row, max_col in boxes:
        draw.rectangle(
            [min_col - 3, min_row + row_offset - 3, max_col + 3, max_row + row_offset + 3],
            outline=BOX_COLOUR,
            width=3,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)


def compare_pair(
    page_cfg: Page,
    viewport: Viewport,
    baseline_path: Path,
    current_path: Path,
    diff_path: Path,
) -> Comparison:
    result = Comparison(
        page=page_cfg.name,
        viewport=viewport.name,
        url=page_cfg.url,
        status=PASS,
        baseline=str(baseline_path),
        current=str(current_path),
    )

    baseline = _load(baseline_path)
    current = _load(current_path)

    if baseline.shape[1] != current.shape[1]:
        result.status = ERROR
        result.score = 0.0
        result.note = (
            f"width changed from {baseline.shape[1]}px to {current.shape[1]}px, so the "
            "images cannot be compared; a horizontal scrollbar usually causes this"
        )
        return result

    result.height_delta = int(current.shape[0] - baseline.shape[0])
    height = max(baseline.shape[0], current.shape[0])
    if height < MIN_SIDE or baseline.shape[1] < MIN_SIDE:
        result.status = ERROR
        result.score = 0.0
        result.note = f"image is smaller than the {MIN_SIDE}px comparison window"
        return result

    baseline_rgb = _pad_below(baseline, height)
    current_rgb = _pad_below(current, height)
    baseline_grey = _luma(baseline_rgb)
    current_grey = _luma(current_rgb)

    score, detail = _ssim(baseline_grey, current_grey)
    row_offset = 0

    if score < page_cfg.threshold and page_cfg.max_shift:
        candidate, _ = detect_vertical_shift(baseline_grey, current_grey, page_cfg.max_shift)
        if candidate:
            left, right = _overlap(baseline_grey, current_grey, candidate)
            if left.shape[0] >= MIN_SIDE:
                aligned_score, aligned_detail = _ssim(left, right)
                if aligned_score - score >= ALIGN_GAIN:
                    result.shift = candidate
                    score, detail = aligned_score, aligned_detail
                    baseline_rgb, current_rgb = _overlap(baseline_rgb, current_rgb, candidate)
                    row_offset = max(candidate, 0)

    mask = _difference_mask(detail, baseline_rgb, current_rgb, page_cfg)
    result.score = score
    result.changed_fraction = float(mask.mean()) if mask.size else 0.0
    result.boxes = _boxes(mask)

    substantive = (
        result.changed_fraction > page_cfg.max_changed_fraction or score < page_cfg.threshold
    )
    moved = abs(result.shift) > page_cfg.shift_tolerance or result.height_delta != 0

    if substantive:
        result.status = CHANGED
    elif moved:
        result.status = SHIFTED
    else:
        result.status = PASS

    notes = []
    if result.boxes:
        count = len(result.boxes)
        notes.append(f"{count} changed region{'' if count == 1 else 's'}")
    if result.shift:
        direction = "down" if result.shift > 0 else "up"
        notes.append(f"content moved {abs(result.shift)}px {direction}")
    if result.height_delta:
        grew = "taller" if result.height_delta > 0 else "shorter"
        notes.append(f"page is {abs(result.height_delta)}px {grew}")
    result.note = "; ".join(notes)

    if result.status != PASS:
        _render_diff(current, mask, result.boxes, row_offset, diff_path)
        result.diff = str(diff_path)

    return result


def compare(config: Config, only: tuple[str, ...] = ()) -> list[Comparison]:
    """Compare the latest snapshots against the stored baselines."""
    # Start from a clean report so stale diffs and assets from a previous run
    # (or a different config) never leak into the published output.
    if config.report_dir.exists():
        shutil.rmtree(config.report_dir, ignore_errors=True)
    diff_dir = config.report_dir / "diffs"
    results: list[Comparison] = []

    for page_cfg in config.find(only):
        for viewport in page_cfg.viewports:
            slug = page_cfg.slug(viewport)
            baseline_path = config.baseline_dir / f"{slug}.png"
            current_path = config.snapshot_dir / f"{slug}.png"

            if not current_path.exists():
                results.append(
                    Comparison(
                        page=page_cfg.name,
                        viewport=viewport.name,
                        url=page_cfg.url,
                        status=ERROR,
                        score=0.0,
                        note="no snapshot found; run capture first",
                    )
                )
                continue

            if not baseline_path.exists():
                results.append(
                    Comparison(
                        page=page_cfg.name,
                        viewport=viewport.name,
                        url=page_cfg.url,
                        status=NEW,
                        current=str(current_path),
                        note="no baseline yet; run approve to accept this as the reference",
                    )
                )
                continue

            try:
                results.append(
                    compare_pair(
                        page_cfg,
                        viewport,
                        baseline_path,
                        current_path,
                        diff_dir / f"{slug}.png",
                    )
                )
            except (OSError, ValueError) as exc:
                log.error("could not compare %s: %s", slug, exc)
                results.append(
                    Comparison(
                        page=page_cfg.name,
                        viewport=viewport.name,
                        url=page_cfg.url,
                        status=ERROR,
                        score=0.0,
                        note=str(exc),
                    )
                )

    return sorted(results, key=lambda item: item.sort_key)
