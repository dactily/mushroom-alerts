"""The picture that travels with the block: one map, one list, one scale.

Why a picture at all
--------------------
The Telegram message is a list of "location — chance, %" in the order of
``locations.yaml``, and that order is the user's own: nearest forest first,
then the ones with an easy road.  What the list cannot say is *where* those
places are relative to each other, and that is exactly the question the
reader asks when two neighbouring valleys differ by twenty points.  So the
same numbers are also drawn on a map of the area, and the map travels as a
native Telegram photo next to the text.

The text stays the authority.  A picture compresses, a percentage on a
1200 px map can collide with its neighbour, and a phone renders the caption
before the image.  Therefore:

* **nothing is computed here.**  :func:`build_layout` reads the very
  ``summary`` dict that :func:`mushroom_alerts.report.render` prints, and
  joins it with ``locations.yaml`` by slug for the coordinates.  Every
  number and every date on the image is the same object the block printed,
  not a second calculation of it;
* the list under the map carries **every** location in config order, so a
  label the collision solver had to drop from the map is never lost;
* a location the basemap does not cover is named on the image and reported
  to the caller -- never silently skipped.

Layout
------
One fixed page, 1200x1600, four bands (all constants in one block below)::

    0 ....  140   header:  «Грибной прогноз» + the date of the block
    140 .. 1140   map:     the 1200x1000 basemap with the markers on it
    1140 . 1530   list:    every location, two columns, config order
    1530 . 1600   footer:  colour scale, distance scale, OSM attribution

Markers
-------
A filled circle at the projected coordinates, coloured by the chance and
outlined in near-black so it survives a dark green forest underneath.  Next
to it the percentage in bold, haloed in white, and -- only where it fits --
the short name underneath.  Placement is deterministic: locations are placed
in config order, each tries the candidate offsets right/left/above/below in
that order, first free one wins; then the percentage alone in the same four;
then a nudged position with a leader line back to the dot.
:data:`LABEL_OFFSETS` lets a human override one slug without touching
``locations.yaml``.

Colour
------
One continuous scale, :func:`chance_color`, from muted red at 0 % through
amber at 50 % to green at 100 %.  The marker, the swatch in the list and the
bar in the footer all call it, so they cannot drift apart.  A location with
no number is neutral grey and prints «—» -- never 0 %, which would be a
statement about the forest rather than about the data.
"""

from __future__ import annotations

import colorsys
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont

from .. import report as report_lib
from ..base import Location

try:  # the basemap is built on its own branch; this module must import anyway
    from .basemap import (  # noqa: F401 - re-exported on purpose
        DEFAULT_BASEMAP,
        FONT_BOLD_PATH,
        FONT_PATH,
        OutsideBasemap,
        load_basemap,
    )
except ImportError:  # pragma: no cover - exercised by whichever branch is alone
    DEFAULT_BASEMAP = None
    FONT_PATH = None
    FONT_BOLD_PATH = None

    class OutsideBasemap(ValueError):
        """A point that is not on the basemap (stand-in definition).

        The real one lives in :mod:`mushroom_alerts.mapping.basemap` and
        carries the bounding box in its message.  Callers catch the name
        exported from *this* module, so the two can never disagree.
        """

    def load_basemap(path: Path | None = None):
        raise RuntimeError(
            "mushroom_alerts.mapping.basemap is unavailable: no basemap to render on"
        )


__all__ = [
    "WIDTH",
    "HEIGHT",
    "LABEL_OFFSETS",
    "PRUNE_DAYS",
    "FILE_PATTERN",
    "OutsideBasemap",
    "Run",
    "Line",
    "Marker",
    "Row",
    "Layout",
    "MapFile",
    "chance_color",
    "build_layout",
    "draw_layout",
    "render_image",
    "render_map",
    "map_name",
    "prune_maps",
]


# ----------------------------------------------------------------------
# layout constants -- the whole geometry of the page, in one place
# ----------------------------------------------------------------------
WIDTH = 1200
HEIGHT = 1600

HEADER_TOP, HEADER_BOTTOM = 0, 140
MAP_TOP, MAP_BOTTOM = 140, 1140
LIST_TOP, LIST_BOTTOM = 1140, 1530
FOOTER_TOP, FOOTER_BOTTOM = 1530, 1600

MAP_HEIGHT = MAP_BOTTOM - MAP_TOP

#: Header: the title, then the date the block speaks about.
TITLE = "Грибной прогноз"
WEEKEND_PREFIX = "Выходные"
TITLE_SIZE = 58
TITLE_BASELINE = 74
SUBTITLE_SIZE = 40
SUBTITLE_BASELINE = 124

#: Markers.  The percentage is the thing that must read on a phone, so it
#: is the biggest text on the page after the title.
MARKER_RADIUS = 17
MARKER_OUTLINE = 3
CHANCE_SIZE = 52
NAME_SIZE = 40
LABEL_GAP = 16  # dot edge -> text box; wider than 2*LABEL_PAD, so the
#                 four candidates clear the dot they belong to
LABEL_LINE_GAP = 2
LABEL_PAD = 7  # collision padding around a placed box
LABEL_HALO = 4  # white outline that keeps text off the forest
MAP_PAD = 12  # a label never touches the edge of the viewport
LEADER_MIN_PX = 72  # farther than this, the label gets a leader line
NUDGE_STEP = 34
NUDGE_RINGS = 14
#: Tried in this order, so a nudged label drifts sideways before it drifts
#: up, and the result is the same on every run.
NUDGE_ANGLES = (0, 30, 330, 60, 300, 90, 270, 120, 240, 150, 210, 180)

#: List: every location, two columns, config order, never re-sorted.
LIST_MARGIN = 28
LIST_GUTTER = 24
LIST_PAD_TOP = 12
LIST_COLUMNS = 2
LIST_ROW_MAX = 56
LIST_FONT_MAX = 38
LIST_FONT_MIN = 18
#: A row starts at this share of its height and shrinks, never truncates,
#: until the longest name and the widest percentage both fit.
LIST_FONT_SHARE = 0.74
LIST_NAME_GAP = 10
SWATCH_SIZE = 26
SWATCH_GAP = 14

#: Footer: the colour scale, the distance scale, the attribution.
FOOTER_LABEL_SIZE = 20
SCALE_BOX = (LIST_MARGIN, 1541, 328, 1559)
SCALE_LABEL_BASELINE = 1582
DISTANCE_X = 400
DISTANCE_Y = 1550
DISTANCE_TICK = 7
DISTANCE_MAX_PX = 400
DISTANCE_TARGET_PX = 150
DISTANCE_STEPS_KM = (1, 2, 5, 10, 20, 50, 100)
ATTRIBUTION = "© OpenStreetMap contributors"
ATTRIBUTION_URL = "openstreetmap.org/copyright"
ATTRIBUTION_RIGHT = WIDTH - LIST_MARGIN
ATTRIBUTION_BASELINES = (1552, 1580)
ATTRIBUTION_WIDTH = 380

#: The note that names locations the basemap does not cover.
NOTE_SIZE = 28
NOTE_PAD = 10

#: Colours.  The page is paper, the list is white, the ink is near-black.
PAGE_BG = (247, 246, 242)
PANEL_BG = (255, 255, 255)
INK = (28, 30, 32)
MUTED = (110, 114, 118)
HAIRLINE = (216, 214, 208)
HALO_COLOR = (255, 255, 255)
MARKER_EDGE = (26, 28, 30)
LEADER_COLOR = (60, 62, 64)

#: The chance scale: muted red -> amber -> green, interpolated.  Nothing
#: else in the package may invent a colour for a percentage.
COLOR_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.0, (178, 58, 50)),
    (50.0, (222, 158, 46)),
    (100.0, (52, 140, 62)),
)
#: No number is not a low number: it gets its own neutral grey.
COLOR_NONE = (138, 140, 144)

#: A dot may be as bright as it likes; text has to survive white paper and
#: a phone screen.  Darkening the fill by a factor turned every mid-scale
#: colour into the same brown, so text keeps the *hue* and is pinned to one
#: lightness and a saturation floor instead: dark red, amber, vivid green.
INK_LIGHTNESS = 0.36
INK_SATURATION = 0.62
#: Below this saturation a colour is grey and must stay grey.
INK_GREY = 0.15
INK_GREY_FACTOR = 0.55

#: «—», the same em dash the text block prints for a missing number.
NO_VALUE = "—"

#: Hand-tuned label offsets, by slug, in pixels from the marker centre to
#: the centre of its text box: ``{"kohutka": (60, -70)}`` pushes that one
#: label up and to the right.  Empty by default and meant to stay that way:
#: it exists so a crowded pair can be separated by hand without touching
#: ``locations.yaml``, which is the user's file and not a drawing hint.
#: An override skips the candidate search and is obeyed as given -- it is a
#: human decision -- but the box still reserves its space, so the labels
#: placed after it move out of the way.
LABEL_OFFSETS: dict[str, tuple[int, int]] = {}

#: Delivery: one file per release, pruned after a month.
FILE_PREFIX = "mushroom"
FILE_PATTERN = re.compile(
    r"^mushroom-(?P<mode>daily|weekend)-(?P<date>\d{4}-\d{2}-\d{2})-[0-9a-f]{8}\.png$"
)
PRUNE_DAYS = 31


# ----------------------------------------------------------------------
# fonts
# ----------------------------------------------------------------------
#: Used only when the basemap module ships no font (its DejaVu is vendored
#: with the assets).  Both lists need Czech *and* Cyrillic coverage.
FALLBACK_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)
FALLBACK_FONTS_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)


def _font_candidates(bold: bool) -> list[Path]:
    shipped = FONT_BOLD_PATH if bold else FONT_PATH
    names = [shipped] if shipped else []
    names += [Path(name) for name in (FALLBACK_FONTS_BOLD if bold else FALLBACK_FONTS)]
    return [Path(name) for name in names if name and Path(name).exists()]


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> Any:
    """A font of this size, from the assets if they are there."""
    for path in _font_candidates(bold):
        try:
            return ImageFont.truetype(str(path), size)
        except (OSError, ValueError):  # pragma: no cover - unreadable font file
            continue
    try:  # pragma: no cover - only when no TTF at all is installed
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 9.2
        return ImageFont.load_default()


def _metrics(size: int, bold: bool) -> tuple[int, int]:
    """``(ascent, line height)`` -- one height for every run of a line."""
    font = _font(size, bold)
    getmetrics = getattr(font, "getmetrics", None)
    if getmetrics is None:  # pragma: no cover - bitmap fallback font
        box = font.getbbox("Ag")
        return int(box[3]), int(box[3] - box[1]) or size
    ascent, descent = getmetrics()
    return int(ascent), int(ascent + descent)


def _width(text: str, size: int, bold: bool) -> int:
    return int(math.ceil(_font(size, bold).getlength(text)))


def _fit_size(text: str, size: int, bold: bool, max_width: int, floor: int = 12) -> int:
    """The largest size at or below ``size`` whose text still fits."""
    while size > floor and _width(text, size, bold) > max_width:
        size -= 1
    return size


def _ellipsize(text: str, size: int, bold: bool, max_width: int) -> str:
    if _width(text, size, bold) <= max_width:
        return text
    cut = text
    while cut and _width(cut + "…", size, bold) > max_width:
        cut = cut[:-1]
    return (cut + "…") if cut else ""


# ----------------------------------------------------------------------
# colour
# ----------------------------------------------------------------------
def chance_color(value: int | float | None) -> tuple[int, int, int]:
    """The colour of a percentage: 0 % muted red, 50 % amber, 100 % green.

    Continuous between the stops, so two locations five points apart look
    five points apart.  ``None`` is not a low number and gets the neutral
    grey of :data:`COLOR_NONE`.
    """
    if value is None:
        return COLOR_NONE
    percent = max(0.0, min(100.0, float(value)))
    for (x0, low), (x1, high) in zip(COLOR_STOPS, COLOR_STOPS[1:]):
        if percent <= x1:
            span = x1 - x0
            t = 0.0 if span <= 0 else (percent - x0) / span
            return tuple(int(round(a + (b - a) * t)) for a, b in zip(low, high))  # type: ignore[return-value]
    return COLOR_STOPS[-1][1]


def _ink(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """The same hue, dark enough to be text on white paper.

    Red stays red and green stays green -- which a flat multiply did not
    manage: it pushed both amber and green into the same brown, and the
    list stopped saying anything the swatches had not said already.
    """
    hue, lightness, saturation = colorsys.rgb_to_hls(*(c / 255 for c in color))
    if saturation < INK_GREY:  # grey is a statement of its own; keep it
        return tuple(int(round(c * INK_GREY_FACTOR)) for c in color)  # type: ignore[return-value]
    red, green, blue = colorsys.hls_to_rgb(
        hue, INK_LIGHTNESS, max(saturation, INK_SATURATION)
    )
    return (int(round(red * 255)), int(round(green * 255)), int(round(blue * 255)))


# ----------------------------------------------------------------------
# the layout model -- what the test reads instead of the pixels
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Run:
    """One stretch of text in one colour."""

    text: str
    color: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class Line:
    """One placed line of runs: left edge, baseline, size."""

    runs: tuple[Run, ...]
    size: int
    bold: bool
    x: int
    baseline: int
    halo: int = 0

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass(frozen=True, slots=True)
class Marker:
    """One location on the map: the dot, its label, and how it got there."""

    slug: str
    short_name: str
    x: int
    y: int
    color: tuple[int, int, int]
    values: tuple[int | None, ...]
    lines: tuple[Line, ...]
    box: tuple[int, int, int, int]
    name_shown: bool
    placement: str
    leader: tuple[int, int, int, int] | None = None

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines)


@dataclass(frozen=True, slots=True)
class Row:
    """One line of the authoritative list under the map."""

    slug: str
    short_name: str
    values: tuple[int | None, ...]
    color: tuple[int, int, int]
    swatch: tuple[int, int, int, int]
    lines: tuple[Line, ...]

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines)


@dataclass(frozen=True, slots=True)
class Layout:
    """Everything the drawing step needs, and nothing a pixel can hide."""

    mode: str
    title: str
    subtitle: str
    header: tuple[Line, ...]
    markers: tuple[Marker, ...]
    rows: tuple[Row, ...]
    footer: tuple[Line, ...]
    notes: tuple[Line, ...]
    note_box: tuple[int, int, int, int] | None
    scale_box: tuple[int, int, int, int]
    distance_box: tuple[int, int, int, int]
    distance_km: int
    km_per_pixel: float
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MapFile:
    """A written map: where it landed and what was wrong with it."""

    path: Path
    layout: Layout
    warnings: tuple[str, ...] = ()


# ----------------------------------------------------------------------
# reading the summary -- the numbers, never recomputed
# ----------------------------------------------------------------------
def _percent(value: int | None) -> str:
    """``45%``, or «—».  The same value the block printed as ``45 %``."""
    return NO_VALUE if value is None else f"{value}%"


def _values(item: Mapping[str, Any], mode: str) -> tuple[int | None, ...]:
    """The numbers this location shows: one a day, two at the weekend."""
    if mode == report_lib.WEEKEND:
        return tuple(day["chance"] for day in item["weekend"])
    return (item["chance"],)


def _best(values: Sequence[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return max(known) if known else None


def _labels(item: Mapping[str, Any], mode: str) -> tuple[str, ...]:
    """``()`` daily, ``("Сб", "Вс")`` at the weekend -- from the summary."""
    if mode == report_lib.WEEKEND:
        return tuple(str(day["label"]).capitalize() for day in item["weekend"])
    return ()


def _list_runs(item: Mapping[str, Any], mode: str) -> tuple[Run, ...]:
    """``45%``, or ``Сб 45% · Вс 50%`` with each day on its own colour."""
    values = _values(item, mode)
    if mode != report_lib.WEEKEND:
        return (Run(_percent(values[0]), _ink(chance_color(values[0]))),)
    runs: list[Run] = []
    for index, (label, value) in enumerate(zip(_labels(item, mode), values)):
        if index:
            runs.append(Run(" · ", MUTED))
        runs.append(Run(f"{label} {_percent(value)}", _ink(chance_color(value))))
    return tuple(runs)


def _marker_runs(item: Mapping[str, Any], mode: str) -> tuple[Run, ...]:
    """The map is short of room: ``45%`` daily, ``45·50%`` at the weekend."""
    values = _values(item, mode)
    if all(value is None for value in values):
        # «—·—%» says nothing twice; one dash is the whole statement.
        return (Run(NO_VALUE, _ink(COLOR_NONE)),)
    if mode != report_lib.WEEKEND:
        return (Run(_percent(values[0]), _ink(chance_color(values[0]))),)
    runs: list[Run] = []
    for index, value in enumerate(values):
        if index:
            runs.append(Run("·", MUTED))
        text = NO_VALUE if value is None else str(value)
        runs.append(Run(text, _ink(chance_color(value))))
    runs.append(Run("%", _ink(chance_color(_best(values)))))
    return tuple(runs)


def subtitle_of(summary: Mapping[str, Any]) -> str:
    """``13.09``, or ``Выходные 13–14.09`` -- the block's own date string."""
    label = report_lib.date_label(summary)
    if str(summary["mode"]) == report_lib.WEEKEND:
        return f"{WEEKEND_PREFIX} {label}"
    return label


# ----------------------------------------------------------------------
# text helpers shared by every band
# ----------------------------------------------------------------------
def _runs_width(runs: Sequence[Run], size: int, bold: bool) -> int:
    return sum(_width(run.text, size, bold) for run in runs)


def _line(
    runs: Sequence[Run],
    *,
    size: int,
    bold: bool,
    x: int,
    baseline: int,
    halo: int = 0,
    align: str = "left",
) -> Line:
    if align != "left":
        width = _runs_width(runs, size, bold)
        x = x - width if align == "right" else x - width // 2
    return Line(tuple(runs), size, bold, int(x), int(baseline), halo)


def _text_line(
    text: str, *, color: tuple[int, int, int] = INK, **kwargs: Any
) -> Line:
    return _line([Run(text, color)], **kwargs)


# ----------------------------------------------------------------------
# marker placement
# ----------------------------------------------------------------------
def _overlap_area(
    box: tuple[int, int, int, int], taken: Iterable[tuple[int, int, int, int]]
) -> int:
    """How much of ``box`` is already spoken for.  Zero means it is free.

    The area, not a yes/no, so that the hopeless case -- a location boxed in
    by its neighbours and the edge of the map -- can still pick the least
    damaging place instead of the first one it tried.
    """
    x0, y0, x1, y1 = box
    total = 0
    for a0, b0, a1, b1 in taken:
        width = min(x1, a1) - max(x0, a0)
        height = min(y1, b1) - max(y0, b0)
        if width > 0 and height > 0:
            total += width * height
    return total


def _inside_map(box: tuple[int, int, int, int]) -> bool:
    x0, y0, x1, y1 = box
    return (
        x0 >= MAP_PAD
        and y0 >= MAP_TOP + MAP_PAD
        and x1 <= WIDTH - MAP_PAD
        and y1 <= MAP_BOTTOM - MAP_PAD
    )


def _candidates(
    cx: int, cy: int, width: int, height: int
) -> list[tuple[str, tuple[int, int]]]:
    """Right, left, above, below -- in that order, so it is reproducible."""
    reach = MARKER_RADIUS + LABEL_GAP
    return [
        ("right", (cx + reach, cy - height // 2)),
        ("left", (cx - reach - width, cy - height // 2)),
        ("below", (cx - width // 2, cy + reach)),
        ("above", (cx - width // 2, cy - reach - height)),
    ]


def _nudged(cx: int, cy: int, width: int, height: int) -> list[tuple[int, int]]:
    """Wider and wider rings around the dot, for the crowded cases."""
    out: list[tuple[int, int]] = []
    for ring in range(1, NUDGE_RINGS + 1):
        distance = MARKER_RADIUS + LABEL_GAP + ring * NUDGE_STEP
        for degrees in NUDGE_ANGLES:
            angle = math.radians(degrees)
            px = cx + distance * math.cos(angle)
            py = cy - distance * math.sin(angle)
            out.append((int(px - width / 2), int(py - height / 2)))
    return out


def _block_lines(
    chance: Sequence[Run], name: str | None, x: int, y: int
) -> tuple[tuple[Line, ...], tuple[int, int, int, int]]:
    """Place the percentage (and maybe the name) with the top-left at x, y."""
    ascent, height = _metrics(CHANCE_SIZE, True)
    chance_width = _runs_width(chance, CHANCE_SIZE, True)
    total_width, total_height = chance_width, height
    lines = [
        _line(chance, size=CHANCE_SIZE, bold=True, x=x, baseline=y + ascent, halo=LABEL_HALO)
    ]
    if name:
        name_ascent, name_height = _metrics(NAME_SIZE, True)
        name_width = _width(name, NAME_SIZE, True)
        total_width = max(chance_width, name_width)
        total_height = height + LABEL_LINE_GAP + name_height
        lines.append(
            _text_line(
                name,
                size=NAME_SIZE,
                bold=True,
                x=x + (total_width - name_width) // 2,
                baseline=y + height + LABEL_LINE_GAP + name_ascent,
                halo=LABEL_HALO,
                color=INK,
            )
        )
        # centre the percentage over the (wider) name
        lines[0] = _line(
            chance,
            size=CHANCE_SIZE,
            bold=True,
            x=x + (total_width - chance_width) // 2,
            baseline=y + ascent,
            halo=LABEL_HALO,
        )
    return tuple(lines), (x, y, x + total_width, y + total_height)


def _block_size(chance: Sequence[Run], name: str | None) -> tuple[int, int]:
    _, box = _block_lines(chance, name, 0, 0)
    return box[2] - box[0], box[3] - box[1]


def _leader(box: tuple[int, int, int, int], cx: int, cy: int) -> tuple[int, int, int, int] | None:
    """A thin line from the dot to the box, when the two drifted apart."""
    x0, y0, x1, y1 = box
    tx = min(max(cx, x0), x1)
    ty = min(max(cy, y0), y1)
    if math.hypot(tx - cx, ty - cy) <= LEADER_MIN_PX:
        return None
    return (cx, cy, int(tx), int(ty))


def _place(
    *,
    slug: str,
    cx: int,
    cy: int,
    chance: tuple[Run, ...],
    name: str,
    taken: list[tuple[int, int, int, int]],
) -> tuple[tuple[Line, ...], tuple[int, int, int, int], bool, str]:
    """Where this label goes.  Deterministic: the first free candidate wins.

    Full block (percentage + name) in the four directions, then the
    percentage alone in the same four, then wider and wider rings around the
    dot.  A location hemmed in by its neighbours and the edge of the map
    still has to be drawn -- the last resort is the least crowded position
    of all the ones tried, because a percentage half over a neighbour is
    still better than a location that vanished, and the list under the map
    carries the number regardless.
    """
    override = LABEL_OFFSETS.get(slug)
    if override is not None:
        width, height = _block_size(chance, name)
        dx, dy = override
        lines, box = _block_lines(
            chance, name, cx + dx - width // 2, cy + dy - height // 2
        )
        return lines, box, True, "override"

    fallback: tuple[int, tuple[Line, ...], tuple[int, int, int, int]] | None = None

    def free(label: str | None, x: int, y: int) -> tuple[tuple[Line, ...], tuple[int, int, int, int]] | None:
        nonlocal fallback
        lines, box = _block_lines(chance, label, x, y)
        padded = (box[0] - LABEL_PAD, box[1] - LABEL_PAD, box[2] + LABEL_PAD, box[3] + LABEL_PAD)
        if not _inside_map(padded):
            return None  # off the picture is never an answer
        area = _overlap_area(padded, taken)
        if area == 0:
            return lines, box
        if fallback is None or area < fallback[0]:
            fallback = (area, lines, box)
        return None

    for with_name in (True, False):
        label = name if with_name else None
        width, height = _block_size(chance, label)
        for placement, (x, y) in _candidates(cx, cy, width, height):
            found = free(label, x, y)
            if found is not None:
                return found[0], found[1], with_name, placement

    width, height = _block_size(chance, None)
    for x, y in _nudged(cx, cy, width, height):
        found = free(None, x, y)
        if found is not None:
            return found[0], found[1], False, "nudged"

    if fallback is not None:
        return fallback[1], fallback[2], False, "crowded"
    x, y = _candidates(cx, cy, width, height)[0][1]
    lines, box = _block_lines(chance, None, x, y)
    return lines, box, False, "forced"


# ----------------------------------------------------------------------
# building the layout
# ----------------------------------------------------------------------
def _projection(basemap: Any) -> tuple[float, float]:
    """Scale from basemap pixels to page pixels (usually 1:1)."""
    return WIDTH / float(basemap.width), MAP_HEIGHT / float(basemap.height)


def _markers(
    summary: Mapping[str, Any],
    locations: Sequence[Location],
    basemap: Any,
) -> tuple[tuple[Marker, ...], list[str], list[str]]:
    """Every location that is on the map, in config order.

    Returns the markers, the caller-facing warnings, and the short names the
    note on the image has to own up to.
    """
    mode = str(summary["mode"])
    by_slug = {loc.slug: loc for loc in locations}
    scale_x, scale_y = _projection(basemap)

    points: list[tuple[Mapping[str, Any], Location, int, int]] = []
    warnings: list[str] = []
    off_map: list[str] = []
    for item in summary["locations"]:
        slug = str(item["slug"])
        short = str(item["short_name"])
        location = by_slug.get(slug)
        if location is None:
            warnings.append(f"{slug}: no coordinates in locations.yaml, not drawn")
            off_map.append(short)
            continue
        try:
            px, py = basemap.to_pixel(location.lat, location.lon)
        except OutsideBasemap:
            # Spelled out here rather than forwarded from the exception: the
            # diagnostic has to name the point and the file to fix, and it
            # must read the same whoever raised it.
            warnings.append(
                f"{slug} ({short}): {location.lat:.5f}, {location.lon:.5f} is outside "
                f"the {getattr(basemap, 'name', 'basemap')!r} basemap, not drawn"
            )
            off_map.append(short)
            continue
        points.append((item, location, int(round(px * scale_x)), int(round(MAP_TOP + py * scale_y))))

    # Every dot is reserved before the first label is placed: a label may
    # cover the map, never a marker -- not another location's, and not the
    # one it belongs to, which is why LABEL_GAP clears LABEL_PAD twice over.
    taken: list[tuple[int, int, int, int]] = [
        (
            x - MARKER_RADIUS - LABEL_PAD,
            y - MARKER_RADIUS - LABEL_PAD,
            x + MARKER_RADIUS + LABEL_PAD,
            y + MARKER_RADIUS + LABEL_PAD,
        )
        for _, _, x, y in points
    ]

    markers: list[Marker] = []
    for item, location, x, y in points:  # config order, so it is reproducible
        values = _values(item, mode)
        lines, box, name_shown, placement = _place(
            slug=str(item["slug"]),
            cx=x,
            cy=y,
            chance=_marker_runs(item, mode),
            name=str(item["short_name"]),
            taken=taken,
        )
        taken.append((box[0] - LABEL_PAD, box[1] - LABEL_PAD, box[2] + LABEL_PAD, box[3] + LABEL_PAD))
        markers.append(
            Marker(
                slug=str(item["slug"]),
                short_name=str(item["short_name"]),
                x=x,
                y=y,
                color=chance_color(_best(values)),
                values=values,
                lines=lines,
                box=box,
                name_shown=name_shown,
                placement=placement,
                leader=_leader(box, x, y),
            )
        )
    return tuple(markers), warnings, off_map


def _rows(summary: Mapping[str, Any]) -> tuple[Row, ...]:
    """The authoritative list: every location, config order, two columns."""
    mode = str(summary["mode"])
    items = list(summary["locations"])
    if not items:
        return ()
    per_column = math.ceil(len(items) / LIST_COLUMNS)
    available = LIST_BOTTOM - LIST_TOP - 2 * LIST_PAD_TOP
    row_height = min(LIST_ROW_MAX, available // per_column)
    column_width = (WIDTH - 2 * LIST_MARGIN - LIST_GUTTER * (LIST_COLUMNS - 1)) // LIST_COLUMNS

    # The list is the authoritative reading of the block, so a name is never
    # cut to keep the type big: the whole column steps down a point at a
    # time until the longest name and the widest percentage both fit.  Two
    # weekend values need roughly a third more room than one daily value.
    names = [str(item["short_name"]) for item in items]
    all_runs = [_list_runs(item, mode) for item in items]
    size = max(LIST_FONT_MIN, min(LIST_FONT_MAX, int(row_height * LIST_FONT_SHARE)))
    while size > LIST_FONT_MIN:
        widest = max(
            SWATCH_SIZE
            + SWATCH_GAP
            + _width(name, size, False)
            + LIST_NAME_GAP
            + _runs_width(runs, size, True)
            for name, runs in zip(names, all_runs)
        )
        if widest <= column_width:
            break
        size -= 1
    ascent, line_height = _metrics(size, False)

    rows: list[Row] = []
    for index, item in enumerate(items):
        column, row = divmod(index, per_column)
        left = LIST_MARGIN + column * (column_width + LIST_GUTTER)
        top = LIST_TOP + LIST_PAD_TOP + row * row_height
        baseline = top + (row_height - line_height) // 2 + ascent
        values = _values(item, mode)
        color = chance_color(_best(values))

        runs = all_runs[index]
        chance_width = _runs_width(runs, size, True)
        chance_x = left + column_width - chance_width
        name_x = left + SWATCH_SIZE + SWATCH_GAP
        name = _ellipsize(
            names[index], size, False, max(0, chance_x - LIST_NAME_GAP - name_x)
        )
        swatch_top = top + (row_height - SWATCH_SIZE) // 2
        rows.append(
            Row(
                slug=str(item["slug"]),
                short_name=str(item["short_name"]),
                values=values,
                color=color,
                swatch=(left, swatch_top, left + SWATCH_SIZE, swatch_top + SWATCH_SIZE),
                lines=(
                    _text_line(name, size=size, bold=False, x=name_x, baseline=baseline),
                    _line(runs, size=size, bold=True, x=chance_x, baseline=baseline),
                ),
            )
        )
    return tuple(rows)


def _distance_scale(km_per_pixel: float) -> tuple[int, int]:
    """A round number of kilometres and its width in pixels."""
    if km_per_pixel <= 0:  # pragma: no cover - a basemap with no geometry
        return 0, 0
    best = DISTANCE_STEPS_KM[0]
    for km in DISTANCE_STEPS_KM:
        pixels = km / km_per_pixel
        if pixels > DISTANCE_MAX_PX:
            break
        best = km
        if pixels >= DISTANCE_TARGET_PX:
            break
    return best, int(round(best / km_per_pixel))


def _attribution_lines(basemap: Any) -> tuple[str, str]:
    """What the footer must say about where the picture comes from."""
    text = str(getattr(basemap, "attribution", "") or "").strip()
    if "openstreetmap" not in text.lower():
        text = f"{text} · {ATTRIBUTION}".strip(" ·").strip() or ATTRIBUTION
    return text, ATTRIBUTION_URL


def _footer(basemap: Any, km_per_pixel: float) -> tuple[tuple[Line, ...], tuple[int, int, int, int], int]:
    distance_km, distance_px = _distance_scale(km_per_pixel)
    lines: list[Line] = [
        _text_line(
            "0 %",
            size=FOOTER_LABEL_SIZE,
            bold=False,
            x=SCALE_BOX[0],
            baseline=SCALE_LABEL_BASELINE,
            color=MUTED,
        ),
        _text_line(
            "50 %",
            size=FOOTER_LABEL_SIZE,
            bold=False,
            x=(SCALE_BOX[0] + SCALE_BOX[2]) // 2,
            baseline=SCALE_LABEL_BASELINE,
            color=MUTED,
            align="center",
        ),
        _text_line(
            "100 %",
            size=FOOTER_LABEL_SIZE,
            bold=False,
            x=SCALE_BOX[2],
            baseline=SCALE_LABEL_BASELINE,
            color=MUTED,
            align="right",
        ),
        _text_line(
            f"{distance_km} км",
            size=FOOTER_LABEL_SIZE,
            bold=False,
            x=DISTANCE_X,
            baseline=SCALE_LABEL_BASELINE,
            color=MUTED,
        ),
    ]
    for text, baseline in zip(_attribution_lines(basemap), ATTRIBUTION_BASELINES):
        size = _fit_size(text, FOOTER_LABEL_SIZE, False, ATTRIBUTION_WIDTH)
        lines.append(
            _text_line(
                text,
                size=size,
                bold=False,
                x=ATTRIBUTION_RIGHT,
                baseline=baseline,
                color=MUTED,
                align="right",
            )
        )
    distance_box = (
        DISTANCE_X,
        DISTANCE_Y - DISTANCE_TICK,
        DISTANCE_X + distance_px,
        DISTANCE_Y + DISTANCE_TICK,
    )
    return tuple(lines), distance_box, distance_km


def _note(off_map: Sequence[str]) -> tuple[tuple[Line, ...], tuple[int, int, int, int] | None]:
    """«вне карты: Bumbálka» -- the picture owns up to what it cannot show."""
    if not off_map:
        return (), None
    text = "вне карты: " + ", ".join(off_map)
    text = _ellipsize(text, NOTE_SIZE, False, WIDTH - 2 * (MAP_PAD + NOTE_PAD) - 20)
    ascent, height = _metrics(NOTE_SIZE, False)
    x = MAP_PAD + NOTE_PAD
    top = MAP_BOTTOM - MAP_PAD - height - 2 * NOTE_PAD
    box = (
        MAP_PAD,
        top,
        MAP_PAD + _width(text, NOTE_SIZE, False) + 2 * NOTE_PAD,
        top + height + 2 * NOTE_PAD,
    )
    line = _text_line(
        text, size=NOTE_SIZE, bold=False, x=x, baseline=top + NOTE_PAD + ascent, color=INK
    )
    return (line,), box


def build_layout(
    summary: Mapping[str, Any],
    *,
    locations: Sequence[Location],
    basemap: Any,
) -> Layout:
    """Everything the page shows, from the summary the block was built from.

    The only join is by slug, for the coordinates: percentages, dates and
    names are taken from ``summary`` as they are.
    """
    mode = str(summary["mode"])
    if mode not in report_lib.MODES:
        raise ValueError(f"unknown report mode: {mode}")

    subtitle = subtitle_of(summary)
    header = (
        _text_line(
            TITLE,
            size=TITLE_SIZE,
            bold=True,
            x=WIDTH // 2,
            baseline=TITLE_BASELINE,
            align="center",
        ),
        _text_line(
            subtitle,
            size=SUBTITLE_SIZE,
            bold=False,
            x=WIDTH // 2,
            baseline=SUBTITLE_BASELINE,
            align="center",
            color=MUTED,
        ),
    )
    markers, warnings, off_map = _markers(summary, locations, basemap)
    notes, note_box = _note(off_map)
    scale_x, _ = _projection(basemap)
    km_per_pixel = float(basemap.km_per_pixel()) / scale_x
    footer, distance_box, distance_km = _footer(basemap, km_per_pixel)
    return Layout(
        mode=mode,
        title=TITLE,
        subtitle=subtitle,
        header=header,
        markers=markers,
        rows=_rows(summary),
        footer=footer,
        notes=notes,
        note_box=note_box,
        scale_box=SCALE_BOX,
        distance_box=distance_box,
        distance_km=distance_km,
        km_per_pixel=km_per_pixel,
        warnings=tuple(warnings),
    )


# ----------------------------------------------------------------------
# drawing
# ----------------------------------------------------------------------
def _draw_lines(draw: ImageDraw.ImageDraw, lines: Iterable[Line]) -> None:
    """Halo first for the whole line, then the glyphs -- otherwise the halo
    of one run eats the edge of the one before it."""
    lines = list(lines)
    for line in lines:
        if not line.halo:
            continue
        font = _font(line.size, line.bold)
        x = line.x
        for run in line.runs:
            draw.text(
                (x, line.baseline),
                run.text,
                font=font,
                fill=HALO_COLOR,
                anchor="ls",
                stroke_width=line.halo,
                stroke_fill=HALO_COLOR,
            )
            x += font.getlength(run.text)
    for line in lines:
        font = _font(line.size, line.bold)
        x = line.x
        for run in line.runs:
            draw.text((x, line.baseline), run.text, font=font, fill=run.color, anchor="ls")
            x += font.getlength(run.text)


def _draw_gradient(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    span = max(1, x1 - x0 - 1)
    for offset in range(x1 - x0):
        draw.line(
            [(x0 + offset, y0), (x0 + offset, y1)],
            fill=chance_color(offset / span * 100.0),
        )
    draw.rectangle(box, outline=HAIRLINE)


def draw_layout(layout: Layout, *, basemap: Any) -> Image.Image:
    """Paint the layout on to the basemap.  No decisions are taken here."""
    page = Image.new("RGB", (WIDTH, HEIGHT), PAGE_BG)
    base = basemap.open_image()
    if base.size != (WIDTH, MAP_HEIGHT):
        base = base.resize((WIDTH, MAP_HEIGHT), Image.LANCZOS)
    page.paste(base, (0, MAP_TOP))
    draw = ImageDraw.Draw(page)

    # bands
    draw.rectangle((0, LIST_TOP, WIDTH, LIST_BOTTOM), fill=PANEL_BG)
    draw.line((0, MAP_TOP, WIDTH, MAP_TOP), fill=HAIRLINE)
    draw.line((0, LIST_TOP, WIDTH, LIST_TOP), fill=HAIRLINE)
    draw.line((0, LIST_BOTTOM, WIDTH, LIST_BOTTOM), fill=HAIRLINE)

    _draw_lines(draw, layout.header)

    for marker in layout.markers:
        if marker.leader:
            draw.line(marker.leader, fill=LEADER_COLOR, width=2)
    for marker in layout.markers:
        draw.ellipse(
            (
                marker.x - MARKER_RADIUS,
                marker.y - MARKER_RADIUS,
                marker.x + MARKER_RADIUS,
                marker.y + MARKER_RADIUS,
            ),
            fill=marker.color,
            outline=MARKER_EDGE,
            width=MARKER_OUTLINE,
        )
    for marker in layout.markers:
        _draw_lines(draw, marker.lines)

    if layout.note_box:
        draw.rectangle(layout.note_box, fill=PANEL_BG, outline=HAIRLINE)
        _draw_lines(draw, layout.notes)

    for row in layout.rows:
        draw.rounded_rectangle(row.swatch, radius=6, fill=row.color, outline=HAIRLINE)
        _draw_lines(draw, row.lines)

    _draw_gradient(draw, layout.scale_box)
    x0, y0, x1, y1 = layout.distance_box
    middle = (y0 + y1) // 2
    draw.line((x0, middle, x1, middle), fill=INK, width=3)
    draw.line((x0, y0, x0, y1), fill=INK, width=3)
    draw.line((x1, y0, x1, y1), fill=INK, width=3)
    _draw_lines(draw, layout.footer)
    return page


def render_image(
    summary: Mapping[str, Any],
    *,
    locations: Sequence[Location],
    basemap: Any,
) -> tuple[Image.Image, Layout]:
    """The finished page and the layout it was drawn from."""
    layout = build_layout(summary, locations=locations, basemap=basemap)
    return draw_layout(layout, basemap=basemap), layout


# ----------------------------------------------------------------------
# delivery: one file per release, written atomically, pruned after a month
# ----------------------------------------------------------------------
def map_name(mode: str, day: date, token: str | None = None) -> str:
    """``mushroom-daily-2026-09-13-1a2b3c4d.png``.

    The token is per release, not per day: a second run must not overwrite
    the picture a message already points at, and Telegram must never be
    handed a file whose bytes changed under it.
    """
    token = token or uuid.uuid4().hex[:8]
    return f"{FILE_PREFIX}-{mode}-{day.isoformat()}-{token}.png"


def _write_atomic(image: Image.Image, path: Path) -> None:
    """Temp file in the same directory, then ``os.replace``."""
    handle = tempfile.NamedTemporaryFile(
        dir=str(path.parent), prefix=f".{FILE_PREFIX}-", suffix=".png.tmp", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            image.save(handle, format="PNG")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def render_map(
    summary: Mapping[str, Any],
    *,
    locations: Sequence[Location],
    directory: Path | str,
    basemap: Any = None,
    token: str | None = None,
) -> MapFile:
    """Render the summary and write it into ``directory``.

    Raises on any failure -- the caller decides what a missing picture means
    for the message, and the answer is always "print the block anyway".
    """
    basemap = load_basemap() if basemap is None else basemap
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    image, layout = render_image(summary, locations=locations, basemap=basemap)
    path = folder / map_name(str(summary["mode"]), summary["date"], token)
    _write_atomic(image, path)
    return MapFile(path=path.resolve(), layout=layout, warnings=layout.warnings)


def prune_maps(
    directory: Path | str, *, today: date, max_age_days: int = PRUNE_DAYS
) -> list[Path]:
    """Delete the maps *this tool* wrote and nobody needs any more.

    Matched by the exact name pattern and dated by the day in the name, so
    nothing else in the directory is ever at risk -- the folder may well be
    someone's shared media drop.
    """
    folder = Path(directory)
    if not folder.is_dir():
        return []
    removed: list[Path] = []
    for path in sorted(folder.iterdir()):
        match = FILE_PATTERN.match(path.name)
        if not match or not path.is_file():
            continue
        try:
            written = date.fromisoformat(match.group("date"))
        except ValueError:  # pragma: no cover - the pattern already checked it
            continue
        if (today - written).days > max_age_days:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed
