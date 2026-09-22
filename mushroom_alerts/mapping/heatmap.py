"""Rolling ČHMÚ mushroom-growth persistence map for the whole Czechia.

Each pixel is the number of archived calendar days in the latest 21-day
window on which the official raster classified that pixel as level 4 or 5.
The colour domain is always 0..21, even while the archive is still filling;
``available_days`` states the actual coverage and prevents over-reading a
young archive.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from ..fetch_chmi_map import ARTIFACT_KIND, BBOX, PALETTE, SOURCE, pixel_for
from ..store import Store
from . import render as render_lib

WINDOW_DAYS = 21
PANEL_WIDTH = 1200
PANEL_HEIGHT = 750
MAP_BOX = (55, 112, 1145, 745)
VSETINSKO = (49.3594, 18.0990)

COLOR_STOPS: tuple[tuple[int, tuple[int, int, int]], ...] = (
    (0, (178, 58, 50)),
    (7, (222, 158, 46)),
    (14, (134, 203, 102)),
    (21, (33, 155, 81)),
)


@dataclass(frozen=True, slots=True)
class HeatmapPanel:
    image: Image.Image
    start_date: date
    latest_date: date
    available_days: int
    window_days: int
    warnings: tuple[str, ...] = ()


def persistence_color(days: int) -> tuple[int, int, int]:
    """Fixed red-to-green colour for 0..21 favourable days."""
    value = max(0, min(WINDOW_DAYS, int(days)))
    for (lo, left), (hi, right) in zip(COLOR_STOPS, COLOR_STOPS[1:]):
        if value <= hi:
            ratio = (value - lo) / (hi - lo)
            return tuple(round(a + (b - a) * ratio) for a, b in zip(left, right))
    return COLOR_STOPS[-1][1]


def _indexed_masks(blob: bytes) -> tuple[Image.Image, Image.Image]:
    """Return 0/1 favourable mask and 0/255 country mask.

    ČHMÚ publishes a palette PNG.  We deliberately classify by RGB rather
    than palette index, because encoders may reorder the palette each day.
    Unknown opaque colours make that snapshot unusable instead of silently
    biasing the accumulation.
    """
    with Image.open(io.BytesIO(blob)) as source:
        source.load()
        if source.mode != "P":
            raise ValueError(f"expected indexed PNG, got {source.mode}")
        palette = source.getpalette()
        if not palette:
            raise ValueError("indexed PNG has no palette")
        transparency = source.info.get("transparency")
        alpha_table = [255] * 256
        if isinstance(transparency, int):
            alpha_table[transparency] = 0
        elif isinstance(transparency, bytes):
            for index, alpha in enumerate(transparency[:256]):
                alpha_table[index] = alpha

        used = {index for _count, index in (source.getcolors(maxcolors=256) or [])}
        favourable = [0] * 256
        valid = [0] * 256
        for index in used:
            offset = index * 3
            rgb = tuple(palette[offset : offset + 3])
            alpha = alpha_table[index]
            if alpha == 0:
                continue
            level = PALETTE.get(rgb)
            if level is None:
                raise ValueError(f"unknown opaque palette colour {rgb}")
            valid[index] = 255
            favourable[index] = 1 if level >= 4 else 0
        return source.point(favourable, mode="L"), source.point(valid, mode="L")


def _colourize(counts: Image.Image, country: Image.Image) -> Image.Image:
    channels = []
    for component in range(3):
        lut = [persistence_color(value)[component] for value in range(256)]
        channels.append(counts.point(lut, mode="L"))
    return Image.merge("RGBA", (*channels, country))


def _draw_panel(
    raster: Image.Image,
    *,
    start: date,
    latest: date,
    available: int,
    window_days: int,
) -> Image.Image:
    page = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), render_lib.PAGE_BG)
    draw = ImageDraw.Draw(page)
    title_font = render_lib._font(34, True)
    detail_font = render_lib._font(24, False)
    small_font = render_lib._font(19, False)

    draw.text((55, 12), "Устойчивость благоприятных условий", font=title_font,
              fill=render_lib.INK)
    period = f"{start:%d.%m}–{latest:%d.%m.%Y} · архив {available}/{window_days} дней"
    draw.text((55, 57), period, font=detail_font, fill=render_lib.MUTED)

    x0, y0, x1, y1 = MAP_BOX
    target = raster.copy()
    target.thumbnail((x1 - x0, y1 - y0), Image.Resampling.BOX)
    px = x0 + (x1 - x0 - target.width) // 2
    py = y0 + (y1 - y0 - target.height) // 2
    page.paste(target, (px, py), target)

    alpha = target.getchannel("A")
    edge = ImageChops.subtract(
        alpha.filter(ImageFilter.MaxFilter(5)), alpha.filter(ImageFilter.MinFilter(5))
    )
    outline = Image.new("RGB", target.size, (72, 75, 72))
    page.paste(outline, (px, py), edge)

    # Geographic reference: the watched Vsetínsko area on the national map.
    sx, sy = pixel_for(VSETINSKO[0], VSETINSKO[1], raster.size)
    vx = px + round(sx * target.width / raster.width)
    vy = py + round(sy * target.height / raster.height)
    draw.ellipse((vx - 8, vy - 8, vx + 8, vy + 8), fill=(255, 255, 255),
                 outline=(25, 27, 26), width=3)
    draw.text((vx - 12, vy - 34), "Vsetínsko", font=small_font,
              fill=(25, 27, 26), stroke_width=3, stroke_fill=(255, 255, 255),
              anchor="ms")

    # Fixed-domain legend.  Missing archive days never stretch the colours.
    legend_x, legend_y, legend_w, legend_h = 735, 69, 385, 15
    for offset in range(legend_w):
        value = round(offset * window_days / max(1, legend_w - 1))
        draw.line((legend_x + offset, legend_y, legend_x + offset,
                   legend_y + legend_h), fill=persistence_color(value))
    draw.rectangle((legend_x, legend_y, legend_x + legend_w, legend_y + legend_h),
                   outline=(90, 92, 90), width=1)
    for value in (0, 7, 14, 21):
        lx = legend_x + round(value * legend_w / window_days)
        draw.text((lx, legend_y + 18), str(value), font=small_font,
                  fill=render_lib.MUTED, anchor="ma")
    draw.text((1120, 20), "дней с уровнем ČHMÚ 4–5", font=small_font,
              fill=render_lib.MUTED, anchor="ra")
    draw.text((1137, 724), "Данные: ČHMÚ · CC BY 4.0", font=small_font,
              fill=(70, 72, 70), anchor="ra",
              stroke_width=2, stroke_fill=(255, 255, 255))
    return page


def build_heatmap_panel(
    store: Store, *, window_days: int = WINDOW_DAYS
) -> HeatmapPanel | None:
    """Build the national panel from the latest rolling archive window."""
    status = store.artifact_archive_status(SOURCE, ARTIFACT_KIND,
                                           window_days=window_days)
    latest_text = status["latest_date"]
    if not latest_text:
        return None
    latest = date.fromisoformat(latest_text)
    start = latest - timedelta(days=window_days - 1)
    artifacts = store.artifacts(SOURCE, ARTIFACT_KIND, since=start, until=latest)

    counts: Image.Image | None = None
    country: Image.Image | None = None
    reference_size: tuple[int, int] | None = None
    usable = 0
    warnings: list[str] = []
    for artifact in artifacts:
        meta: dict[str, Any] = artifact.meta or {}
        if meta.get("projection") not in (None, "EPSG:3857"):
            warnings.append(f"heatmap: skipped {artifact.date}: projection changed")
            continue
        if meta.get("bbox") not in (None, list(BBOX), tuple(BBOX)):
            warnings.append(f"heatmap: skipped {artifact.date}: bbox changed")
            continue
        try:
            favourable, valid = _indexed_masks(artifact.data)
        except (OSError, ValueError) as exc:
            warnings.append(f"heatmap: skipped {artifact.date}: {exc}")
            continue
        if reference_size is None:
            reference_size = favourable.size
            counts = Image.new("L", reference_size, 0)
        elif favourable.size != reference_size:
            favourable = favourable.resize(reference_size, Image.Resampling.NEAREST)
            valid = valid.resize(reference_size, Image.Resampling.NEAREST)
            warnings.append(f"heatmap: resampled {artifact.date}: raster size changed")
        counts = ImageChops.add(counts, favourable)  # type: ignore[arg-type]
        country = valid
        usable += 1

    if counts is None or country is None or usable == 0:
        return None
    raster = _colourize(counts, country)
    panel = _draw_panel(
        raster,
        start=start,
        latest=latest,
        available=usable,
        window_days=window_days,
    )
    return HeatmapPanel(
        image=panel,
        start_date=start,
        latest_date=latest,
        available_days=usable,
        window_days=window_days,
        warnings=tuple(warnings),
    )
