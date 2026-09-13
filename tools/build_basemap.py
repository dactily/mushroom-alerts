#!/usr/bin/env python3
"""Build the committed basemap of the forecast area from OpenStreetMap.

**This is a one-off tool, run by hand.**  Nothing on the daily path may
import it: the daily render opens the PNG this produces and draws markers
on it, with no network and no tiles -- see
``mushroom_alerts/mapping/basemap.py`` and ``assets/basemap/README.md``.

    .venv/bin/python tools/build_basemap.py

It asks Overpass for five layers (forest, settlements, water, roads, places)
inside a box a little larger than the map, draws them with Pillow at 2x and
downsamples, and writes ``assets/basemap/vsetinsko.png`` plus a ``.json``
sidecar that records the bounding box, the queries and the layer counts.

The build is deterministic: with the same cached Overpass answers it writes
a byte-identical PNG, so a rerun that changes nothing shows an empty diff.
``--cache-dir`` (default ``~/.cache/mushroom-alerts/basemap``) keeps those
answers; ``--refresh`` refetches them.

Data: © OpenStreetMap contributors, ODbL -- https://www.openstreetmap.org/copyright
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # run me without installing the package
    sys.path.insert(0, str(REPO_ROOT))

from mushroom_alerts.http import USER_AGENT  # noqa: E402
from mushroom_alerts.mapping.basemap import (  # noqa: E402
    ATTRIBUTION,
    FONT_PATH,
    PROJECTION,
    inverse_mercator_y,
    mercator_x,
    mercator_y,
)

# -- the map -------------------------------------------------------------

NAME = "vsetinsko"
WIDTH, HEIGHT = 1200, 1000  # the map viewport; the Telegram image is taller

#: The box is pinned by its centre and its longitude span; the latitudes
#: fall out of the aspect ratio (see :func:`compute_bbox`).  Centre and span
#: were chosen so all 13 watched locations sit at least 2 km inside.
CENTRE_LON, LON_SPAN = 18.0990, 0.70
CENTRE_LAT = 49.3594

#: Fetch a little wider than we draw, so nothing is cut mid-polygon.
FETCH_BBOX = (49.16, 17.74, 49.56, 18.46)  # south, west, north, east

#: Draw at 2x and downsample -- Pillow has no anti-aliasing of its own.
SUPERSAMPLE = 2

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT = 180  # the server-side budget, seconds
HTTP_TIMEOUT = (30, 300)  # connect, read
#: The public instance hands out two slots per IP and answers 429 when they
#: are busy, so wait before the one retry -- and between layers.
RETRY_SLEEP = 30.0
POLITE_SLEEP = 5.0
SOURCE = f"OpenStreetMap via Overpass API ({OVERPASS_URL})"

# -- colours -------------------------------------------------------------
#
# The markers on top are 34 px dots with 52 px bold text and a white halo,
# so they stay the loud bit whatever the map does -- which means the map may
# carry real colour, and has to: the first version was forest #C9DABB on
# #F6F4EE, two tones 30 luminance apart, and at the 390 px Telegram preview
# width it was one pale green smear with no rivers and no roads in it.
#
# The rule now is *the map is a background, not a whisper*: forest sits near
# luminance 165 (0.299R+0.587G+0.114B), open land stays near-white, and
# every line on top of them -- water, roads, labels -- is saturated or cased
# enough to survive being shrunk to a third of its size.

C_BACKGROUND = (246, 244, 238)  # #F6F4EE warm off-white -- open land
C_FOREST = (150, 180, 132)  # #96B484 luminance 166 (was #C9DABB, 209)
C_RESIDENTIAL = (213, 195, 165)  # #D5C3A5 towns and villages, warm grey-tan
C_INDUSTRIAL = (203, 195, 182)  # #CBC3B6 the same, a shade greyer

#: Water is the region's skeleton -- the Bečva and the Vsetínská Bečva run
#: the valleys every road and every village follows -- so it is the most
#: saturated thing on the map.
C_WATER = (146, 193, 220)  # #92C1DC lake and pond fill
C_RIVER = (72, 138, 182)  # #488AB6
C_WATER_EDGE = C_RIVER  # a lake's rim is the colour of the river feeding it
C_STREAM = (133, 180, 212)  # #85B4D4 thinner and paler than a river

#: Roads are drawn casing-then-fill: a dark thin outline under a lighter
#: core.  That is what makes a 3 px road read over forest at 390 px wide --
#: a flat grey line of the same width simply disappears into it.
C_ROAD = {
    "secondary": ((152, 144, 131), (250, 249, 244)),  # #989083 / #FAF9F4
    "primary": ((176, 138, 86), (245, 213, 167)),  # #B08A56 / #F5D5A7
    "trunk": ((150, 97, 49), (233, 168, 106)),  # #966131 / #E9A86A
    "motorway": ((150, 97, 49), (233, 168, 106)),
}
C_LABEL = (55, 52, 47)  # #37342F
C_LABEL_HALO = (252, 251, 248)
C_ATTRIBUTION = (122, 117, 109)

#: Line widths in 2x pixels (so "3" is 1.5 px on the finished map).
W_STREAM = 3
W_RIVER = 6
W_LAKE_EDGE = 4
#: ``(casing, fill)`` per road class -- the casing shows as half the
#: difference on each side, so a trunk is 8 px of orange in 13 px of brown
#: (4 and 6.5 px once the 2x canvas is downsampled).
W_ROAD = {
    "secondary": (5, 3),
    "primary": (9, 5),
    "trunk": (13, 8),
    "motorway": (13, 8),
}
#: Quiet classes first, so the trunks end up on top.
ROAD_ORDER = ("secondary", "primary", "trunk", "motorway")

#: A brook shorter than this is lint at 40 m per pixel.  Measured over the
#: whole watercourse of one name, not per way -- see :func:`long_streams`.
STREAM_MIN_KM = 1.5
#: Mean radius, for ground lengths -- *not* the sphere Mercator pretends
#: the Earth is (:data:`~mushroom_alerts.mapping.basemap.EARTH_RADIUS_M`).
EARTH_RADIUS_KM = 6371.0088

#: Labels: towns big, villages small, both dark grey in a white halo.
LABEL_SIZE = {"city": 30, "town": 30, "village": 23}  # 2x px
DOT_RADIUS = {"city": 6, "town": 6, "village": 4}
LABEL_HALO = 5
LABEL_GAP = 10
LABEL_PAD = (8, 6)  # collision padding around a placed name, 2x px
#: Enough to orient by, few enough to still see the map under them.
MAX_LABELS = 16
ATTRIBUTION_SIZE = 20

#: Labelled for orientation, always, even if they crowd something else.
PRIORITY_PLACES = ("Valašské Meziříčí", "Vsetín", "Rožnov pod Radhoštěm")
#: City before town before village; within a rank, the bigger population.
PLACE_RANK = {"city": 0, "town": 1, "village": 2}


def _bbox_clause() -> str:
    return ",".join(f"{v}" for v in FETCH_BBOX)


def _query(body: str) -> str:
    return f"[out:json][timeout:{OVERPASS_TIMEOUT}];\n{body.strip()}\nout geom;\n"


BBOX = _bbox_clause()


def build_queries(*, streams: bool) -> dict[str, str]:
    """The five Overpass queries, verbatim into the sidecar and the README.

    ``out geom;`` makes every way and every relation member carry its own
    coordinates, so one request per layer is all we need -- no node lookups,
    no recursion.  The dict order is the fetch order, and the cache key is
    the query text, so editing one of these refetches only that layer.
    """
    water = [
        f'  way["natural"="water"]({BBOX});',
        f'  relation["natural"="water"]({BBOX});',
        f'  way["waterway"="river"]({BBOX});',
    ]
    if streams:
        # Everything comes back; :func:`long_streams` throws away the ditches.
        water.append(f'  way["waterway"="stream"]({BBOX});')
    return {
        "forest": _query(
            f"""(
  way["landuse"="forest"]({BBOX});
  way["natural"="wood"]({BBOX});
  relation["landuse"="forest"]({BBOX});
  relation["natural"="wood"]({BBOX});
);"""
        ),
        "settlements": _query(
            f"""(
  way["landuse"~"^(residential|industrial)$"]({BBOX});
  relation["landuse"~"^(residential|industrial)$"]({BBOX});
);"""
        ),
        "water": _query("(\n" + "\n".join(water) + "\n);"),
        "roads": _query(f"""way["highway"~"^(motorway|trunk|primary|secondary)$"]({BBOX});"""),
        "places": _query(f"""node["place"~"^(city|town|village)$"]({BBOX});"""),
    }


# -- geometry ------------------------------------------------------------


def compute_bbox() -> tuple[float, float, float, float]:
    """``(min_lon, min_lat, max_lon, max_lat)`` of the map.

    Web Mercator is conformal, so an image is undistorted exactly when its
    pixel aspect ratio equals the ratio of the projected spans.  We fix the
    longitudes and solve for the latitudes::

        (y_max - y_min) / (x_max - x_min) == HEIGHT / WIDTH

    keeping ``CENTRE_LAT`` in the middle of the *image* (Mercator y), not
    halfway between the two edge latitudes.
    """
    min_lon = CENTRE_LON - LON_SPAN / 2
    max_lon = CENTRE_LON + LON_SPAN / 2
    span_x = mercator_x(max_lon) - mercator_x(min_lon)
    span_y = span_x * HEIGHT / WIDTH
    centre_y = mercator_y(CENTRE_LAT)
    return (
        min_lon,
        inverse_mercator_y(centre_y - span_y / 2),
        max_lon,
        inverse_mercator_y(centre_y + span_y / 2),
    )


class Projector:
    """lat/lon -> 2x canvas pixels, without the bounds check.

    :meth:`mushroom_alerts.mapping.basemap.Basemap.to_pixel` refuses points
    off the map, which is right for a marker and wrong for a forest that
    runs over the edge, so the builder projects with the same maths and no
    opinion.
    """

    def __init__(self, bbox: tuple[float, float, float, float], scale: int) -> None:
        min_lon, min_lat, max_lon, max_lat = bbox
        self._x0 = mercator_x(min_lon)
        self._y1 = mercator_y(max_lat)
        self._sx = WIDTH * scale / (mercator_x(max_lon) - self._x0)
        self._sy = HEIGHT * scale / (self._y1 - mercator_y(min_lat))

    def __call__(self, lat: float, lon: float) -> tuple[float, float]:
        return (
            (mercator_x(lon) - self._x0) * self._sx,
            (self._y1 - mercator_y(lat)) * self._sy,
        )

    def line(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [self(lat, lon) for lat, lon in points]


# -- Overpass ------------------------------------------------------------


def fetch(
    name: str, query: str, *, cache_dir: Path | None, refresh: bool, log=print
) -> dict:
    """One Overpass answer, from the cache when we have it.

    The cache key is the query itself, so editing a query invalidates only
    that layer.
    """
    cache_file = None
    if cache_dir is not None:
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
        cache_file = cache_dir / f"{name}-{digest}.json"
        if cache_file.exists() and not refresh:
            log(f"  {name}: cached ({cache_file.stat().st_size / 1e6:.1f} MB)")
            return json.loads(cache_file.read_text(encoding="utf-8"))

    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    last: Exception | None = None
    time.sleep(POLITE_SLEEP)
    for attempt in (1, 2):  # one retry, then give up
        started = time.monotonic()
        try:
            response = requests.post(
                OVERPASS_URL, data={"data": query}, headers=headers, timeout=HTTP_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - a one-off tool: report and stop
            last = exc
            log(f"  {name}: attempt {attempt} failed ({exc})")
            if attempt == 1:
                time.sleep(RETRY_SLEEP)
            continue
        log(
            f"  {name}: {len(response.content) / 1e6:.1f} MB, "
            f"{len(payload.get('elements', []))} elements, "
            f"{time.monotonic() - started:.1f} s"
        )
        if cache_file is not None:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
        return payload
    raise SystemExit(f"Overpass failed for {name!r}: {last}")


def elements(payload: dict) -> list[dict]:
    """The answer's elements in a fixed order, so the render is reproducible."""
    return sorted(
        (e for e in payload.get("elements", []) if isinstance(e, dict)),
        key=lambda e: (str(e.get("type", "")), int(e.get("id", 0))),
    )


Ring = list[tuple[float, float]]


def _geometry(element: dict) -> Ring:
    return [
        (float(p["lat"]), float(p["lon"]))
        for p in element.get("geometry") or []
        if isinstance(p, dict) and "lat" in p and "lon" in p
    ]


def _stitch(segments: list[Ring]) -> list[Ring]:
    """Glue a relation's member ways into rings by matching endpoints.

    A multipolygon's outer ring is often split across several ways; drawing
    each of them as its own polygon would slice chords across the forest.
    Overpass repeats the shared node's coordinates exactly, so plain
    equality is enough to find the neighbours.
    """
    rings: list[Ring] = []
    pending = [list(seg) for seg in segments if len(seg) >= 2]
    while pending:
        ring = pending.pop(0)
        joined = True
        while joined and ring[0] != ring[-1]:
            joined = False
            for i, seg in enumerate(pending):
                if seg[0] == ring[-1]:
                    ring.extend(seg[1:])
                elif seg[-1] == ring[-1]:
                    ring.extend(reversed(seg[:-1]))
                elif seg[-1] == ring[0]:
                    ring[:0] = seg[:-1]
                elif seg[0] == ring[0]:
                    ring[:0] = list(reversed(seg[1:]))
                else:
                    continue
                pending.pop(i)
                joined = True
                break
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def areas(payload: dict) -> tuple[list[Ring], list[Ring], dict[str, int]]:
    """``(outer rings, inner rings, counts)`` for an area layer.

    Ways are rings on their own; relations are multipolygons whose members
    carry ``outer``/``inner`` roles (an empty role means outer).
    """
    outer: list[Ring] = []
    inner: list[Ring] = []
    counts = {"ways": 0, "relations": 0}
    for element in elements(payload):
        kind = element.get("type")
        if kind == "way":
            if "area" in _way_class(element):
                ring = _geometry(element)
                if len(ring) >= 3:
                    outer.append(ring)
                    counts["ways"] += 1
        elif kind == "relation":
            outer_parts: list[Ring] = []
            inner_parts: list[Ring] = []
            for member in element.get("members") or []:
                if member.get("type") != "way":
                    continue
                geometry = _geometry(member)
                if len(geometry) < 2:
                    continue
                (inner_parts if member.get("role") == "inner" else outer_parts).append(geometry)
            rings_out = _stitch(outer_parts)
            rings_in = _stitch(inner_parts)
            if rings_out:
                counts["relations"] += 1
            outer.extend(rings_out)
            inner.extend(rings_in)
    return outer, inner, counts


#: The tags that make a closed way a filled polygon rather than a line.
AREA_TAGS = {
    "landuse": {"forest", "residential", "industrial"},
    "natural": {"wood", "water"},
}


def _way_class(element: dict) -> set[str]:
    """What a way is for us: ``area``, ``river``, ``stream`` or a road class.

    A closed way with an area tag is a polygon; everything else with a
    ``waterway`` or ``highway`` tag is a line.  Closure is the reliable
    discriminator -- a lake tagged ``natural=water`` and a river tagged
    ``waterway=river`` can otherwise sit on the same way.
    """
    tags = element.get("tags") or {}
    geometry = element.get("geometry") or []
    closed = len(geometry) >= 4 and geometry[0] == geometry[-1]
    classes: set[str] = set()
    if closed and any(tags.get(key) in values for key, values in AREA_TAGS.items()):
        classes.add("area")
    waterway = tags.get("waterway")
    if not closed and waterway in {"river", "stream"}:
        classes.add(waterway)
    highway = tags.get("highway")
    if highway in W_ROAD:
        classes.add(highway)
    return classes


def subset(payload: dict, key: str, value: str) -> dict:
    """The elements of one payload carrying one tag.

    ``landuse=residential`` and ``landuse=industrial`` arrive in the same
    Overpass answer and are drawn in two different colours, so the layer is
    split here rather than fetched twice.
    """
    return {
        "elements": [
            element
            for element in payload.get("elements", [])
            if isinstance(element, dict) and (element.get("tags") or {}).get(key) == value
        ]
    }


def _length_km(line: Ring) -> float:
    """Ground length of a polyline, km.

    Equirectangular, which is exact enough for a 50 km box: the error over
    one OSM way is far below the 1.5 km threshold it feeds.
    """
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(line, line[1:]):
        dx = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
        dy = math.radians(lat2 - lat1)
        total += math.hypot(dx, dy) * EARTH_RADIUS_KM
    return total


def long_streams(ways: list[dict]) -> list[Ring]:
    """The streams worth drawing: everything under 1.5 km is thrown away.

    Measured over the whole watercourse *of one name*, not per way: OSM
    splits one brook into a dozen ways wherever a bridge or a landuse
    boundary crosses it, and a per-way filter would draw the long ones full
    of holes.  An unnamed way answers for itself.
    """
    total_by_name: dict[str, float] = {}
    for element in ways:
        name = str((element.get("tags") or {}).get("name") or "")
        if name:
            total_by_name[name] = total_by_name.get(name, 0.0) + _length_km(_geometry(element))
    kept: list[Ring] = []
    for element in ways:
        geometry = _geometry(element)
        name = str((element.get("tags") or {}).get("name") or "")
        length = total_by_name[name] if name else _length_km(geometry)
        if length >= STREAM_MIN_KM:
            kept.append(geometry)
    return kept


# -- drawing -------------------------------------------------------------


@dataclass
class LayerStat:
    """What ended up on the map, for the sidecar and for the console."""

    name: str
    colour: str
    elements: int = 0
    drawn: int = 0
    detail: dict[str, int] = field(default_factory=dict)

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "colour": self.colour,
            "elements": self.elements,
            "drawn": self.drawn,
            **{k: v for k, v in sorted(self.detail.items())},
        }


def _hex(colour: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % colour


def _paint(canvas: Image.Image, size: tuple[int, int], colour, painter) -> None:
    """Paint through a mask so overlapping shapes union cleanly and inner
    rings really are holes."""
    mask = Image.new("L", size, 0)
    painter(ImageDraw.Draw(mask))
    canvas.paste(colour, (0, 0), mask)


def _closed(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """A ring's points, with the first repeated at the end if it is not."""
    return points if points and points[0] == points[-1] else points + points[:1]


def draw_areas(
    canvas: Image.Image,
    size: tuple[int, int],
    project: Projector,
    payload: dict,
    colour: tuple[int, int, int],
    *,
    edge: tuple[int, int, int] | None = None,
    edge_width: int = 0,
) -> tuple[int, dict[str, int]]:
    """Fill an area layer, optionally with a darker rim around every ring.

    The rim is painted first and the fill covers it, so what is left is a
    hairline *outside* the shape -- which is what makes a pond a few pixels
    across still read as water rather than as a smudge.
    """
    outer, inner, counts = areas(payload)

    def paint_rim(draw: ImageDraw.ImageDraw) -> None:
        for ring in outer:
            draw.line(_closed(project.line(ring)), fill=255, width=edge_width, joint="curve")

    if edge is not None and edge_width:
        _paint(canvas, size, edge, paint_rim)

    def paint(draw: ImageDraw.ImageDraw) -> None:
        for ring in outer:
            draw.polygon(project.line(ring), fill=255)
        for ring in inner:  # holes, after every fill
            draw.polygon(project.line(ring), fill=0)

    _paint(canvas, size, colour, paint)
    counts["holes"] = len(inner)
    counts["rings"] = len(outer)
    return len(outer) + len(inner), counts


def draw_lines(
    canvas: Image.Image,
    project: Projector,
    ways: list[Ring],
    colour: tuple[int, int, int],
    width: int,
) -> int:
    """One flat run of polylines -- rivers, streams."""
    draw = ImageDraw.Draw(canvas)
    for way in ways:
        draw.line(project.line(way), fill=colour, width=width, joint="curve")
    return len(ways)


def draw_roads(canvas: Image.Image, project: Projector, payload: dict) -> tuple[int, dict]:
    """Casing under fill, so a road reads over the forest as well as over
    the open land.

    Every casing goes down before the first fill rather than each road being
    finished in turn: otherwise a trunk's dark casing would be stamped
    across the pale core of every secondary it crosses.  Within each of the
    two passes the quiet classes come first, so the trunks end up on top.
    """
    draw = ImageDraw.Draw(canvas)
    by_class: dict[str, list[list[tuple[float, float]]]] = {k: [] for k in W_ROAD}
    for element in elements(payload):
        if element.get("type") != "way":
            continue
        for name in _way_class(element) & set(W_ROAD):
            geometry = _geometry(element)
            if len(geometry) >= 2:
                by_class[name].append(project.line(geometry))
    for layer in (0, 1):  # 0 casing, 1 fill
        for name in ROAD_ORDER:
            colour, width = C_ROAD[name][layer], W_ROAD[name][layer]
            for way in by_class[name]:
                draw.line(way, fill=colour, width=width, joint="curve")
    return (
        sum(len(ways) for ways in by_class.values()),
        {k: len(v) for k, v in sorted(by_class.items())},
    )


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    kind: str
    population: int


def places(payload: dict, bbox: tuple[float, float, float, float]) -> list[Place]:
    """Settlements inside the drawn box, best label first.

    The three orientation towns lead; after them it is city before town
    before village and, within a rank, the bigger population -- so a cap on
    the number of labels drops the hamlets and keeps the places a reader has
    heard of.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    found: list[Place] = []
    for element in elements(payload):
        tags = element.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        lat, lon = element.get("lat"), element.get("lon")
        if not name or lat is None or lon is None:
            continue
        if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
            continue
        try:
            population = int(str(tags.get("population") or "0").replace(" ", ""))
        except ValueError:
            population = 0
        found.append(Place(name, float(lat), float(lon), str(tags.get("place")), population))

    def order(place: Place) -> tuple:
        try:
            return (0, PRIORITY_PLACES.index(place.name), 0, place.name)
        except ValueError:
            return (1, PLACE_RANK.get(place.kind, 9), -place.population, place.name)

    return sorted(found, key=order)


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def draw_places(
    canvas: Image.Image,
    size: tuple[int, int],
    project: Projector,
    found: list[Place],
    reserved: list[tuple[float, float, float, float]],
) -> tuple[int, list[str]]:
    """A dot plus a haloed name, skipped when there is no room for it.

    Orientation is the whole point of these labels, so a name that would
    collide with one already drawn -- or run off the edge -- is simply left
    out rather than smeared over the map, and the list stops at
    :data:`MAX_LABELS`: past that the names start hiding the valleys they
    were meant to help find.
    """
    draw = ImageDraw.Draw(canvas)
    fonts = {
        size_px: ImageFont.truetype(str(FONT_PATH), size_px)
        for size_px in set(LABEL_SIZE.values())
    }
    taken = list(reserved)
    margin = 6 * SUPERSAMPLE
    edge = 14 * SUPERSAMPLE  # a dot glued to the border reads as a mistake
    pad_x, pad_y = LABEL_PAD
    labelled: list[str] = []
    for place in found:
        if len(labelled) >= MAX_LABELS:
            break
        font = fonts[LABEL_SIZE.get(place.kind, LABEL_SIZE["village"])]
        radius = DOT_RADIUS.get(place.kind, DOT_RADIUS["village"])
        x, y = project(place.lat, place.lon)
        if not (edge <= x <= size[0] - edge and edge <= y <= size[1] - edge):
            continue
        dot = (x - radius, y - radius, x + radius, y + radius)
        if any(_overlaps(dot, box) for box in taken):
            continue
        for anchor, point in (
            ("lm", (x + LABEL_GAP, y)),
            ("rm", (x - LABEL_GAP, y)),
            ("ma", (x, y + LABEL_GAP)),
            ("md", (x, y - LABEL_GAP)),
        ):
            box = draw.textbbox(point, place.name, font=font, anchor=anchor)
            box = (box[0] - pad_x, box[1] - pad_y, box[2] + pad_x, box[3] + pad_y)
            if box[0] < margin or box[1] < margin:
                continue
            if box[2] > size[0] - margin or box[3] > size[1] - margin:
                continue
            if any(_overlaps(box, other) for other in taken):
                continue
            draw.ellipse(dot, fill=C_LABEL)
            draw.text(
                point,
                place.name,
                font=font,
                fill=C_LABEL,
                anchor=anchor,
                stroke_width=LABEL_HALO,
                stroke_fill=C_LABEL_HALO,
            )
            taken.append(box)
            taken.append(dot)
            labelled.append(place.name)
            break
    return len(labelled), labelled


def draw_attribution(canvas: Image.Image, size: tuple[int, int]) -> tuple[float, float, float, float]:
    """ODbL credit, baked into the picture so it can never be forgotten."""
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(str(FONT_PATH), ATTRIBUTION_SIZE)
    point = (size[0] - 8 * SUPERSAMPLE, size[1] - 6 * SUPERSAMPLE)
    draw.text(
        point,
        ATTRIBUTION,
        font=font,
        fill=C_ATTRIBUTION,
        anchor="rd",
        stroke_width=3,
        stroke_fill=C_LABEL_HALO,
    )
    box = draw.textbbox(point, ATTRIBUTION, font=font, anchor="rd")
    return (box[0] - 10, box[1] - 6, box[2] + 10, box[3] + 6)


# -- the build -----------------------------------------------------------


def build(args: argparse.Namespace) -> int:
    log = (lambda *a: None) if args.quiet else print
    bbox = compute_bbox()
    min_lon, min_lat, max_lon, max_lat = bbox
    log(f"basemap {NAME}: {WIDTH}x{HEIGHT} px, {PROJECTION}")
    log(f"  lon {min_lon!r} .. {max_lon!r}")
    log(f"  lat {min_lat!r} .. {max_lat!r}")

    queries = build_queries(streams=not args.no_streams)

    log("fetching:")
    cache_dir = None if args.no_cache else Path(args.cache_dir).expanduser()
    payloads = {
        name: fetch(name, query, cache_dir=cache_dir, refresh=args.refresh, log=log)
        for name, query in queries.items()
    }

    started = time.monotonic()
    size = (WIDTH * SUPERSAMPLE, HEIGHT * SUPERSAMPLE)
    project = Projector(bbox, SUPERSAMPLE)
    canvas = Image.new("RGB", size, C_BACKGROUND)
    stats: list[LayerStat] = []

    forest = LayerStat("forest", _hex(C_FOREST), len(payloads["forest"].get("elements", [])))
    forest.drawn, forest.detail = draw_areas(canvas, size, project, payloads["forest"], C_FOREST)
    stats.append(forest)

    # Towns over the forest, water over the towns: a river runs through one.
    built = LayerStat(
        "settlements", _hex(C_RESIDENTIAL), len(payloads["settlements"].get("elements", []))
    )
    for landuse, colour in (("residential", C_RESIDENTIAL), ("industrial", C_INDUSTRIAL)):
        drawn, detail = draw_areas(
            canvas, size, project, subset(payloads["settlements"], "landuse", landuse), colour
        )
        built.drawn += drawn
        built.detail[landuse] = detail["rings"]
    stats.append(built)

    rivers: list[Ring] = []
    stream_ways: list[dict] = []
    for element in elements(payloads["water"]):
        if element.get("type") != "way":
            continue
        classes = _way_class(element)
        if "river" in classes:
            geometry = _geometry(element)
            if len(geometry) >= 2:
                rivers.append(geometry)
        elif "stream" in classes:
            stream_ways.append(element)
    streams = long_streams(stream_ways)
    water = LayerStat("water", _hex(C_RIVER), len(payloads["water"].get("elements", [])))
    water.drawn, water.detail = draw_areas(
        canvas, size, project, payloads["water"], C_WATER, edge=C_WATER_EDGE, edge_width=W_LAKE_EDGE
    )
    water.drawn += draw_lines(canvas, project, streams, C_STREAM, W_STREAM)
    water.drawn += draw_lines(canvas, project, rivers, C_RIVER, W_RIVER)
    water.detail["rivers"] = len(rivers)
    water.detail["streams"] = len(streams)
    water.detail["streams_dropped"] = len(stream_ways) - len(streams)
    stats.append(water)

    roads = LayerStat("roads", _hex(C_ROAD["trunk"][1]), len(payloads["roads"].get("elements", [])))
    roads.drawn, roads.detail = draw_roads(canvas, project, payloads["roads"])
    stats.append(roads)

    credit_box = draw_attribution(canvas, size)
    found = places(payloads["places"], bbox)
    labels = LayerStat("places", _hex(C_LABEL), len(payloads["places"].get("elements", [])))
    labels.drawn, labelled = draw_places(canvas, size, project, found, [credit_box])
    labels.detail = {"inside_bbox": len(found), "max_labels": MAX_LABELS}
    stats.append(labels)
    log("labelled: " + ", ".join(labelled))

    image = canvas.resize((WIDTH, HEIGHT), Image.LANCZOS)
    if args.colors:
        image = image.quantize(colors=args.colors, method=Image.Quantize.MEDIANCUT)
    render_seconds = time.monotonic() - started

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / f"{NAME}.png"
    json_path = out_dir / f"{NAME}.json"
    image.save(png_path, format="PNG", optimize=True)

    sidecar = {
        "name": NAME,
        "width": WIDTH,
        "height": HEIGHT,
        "projection": PROJECTION,
        "bbox": {
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
        },
        "image": png_path.name,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCE,
        "attribution": ATTRIBUTION,
        "fetch_bbox": {
            "min_lat": FETCH_BBOX[0],
            "min_lon": FETCH_BBOX[1],
            "max_lat": FETCH_BBOX[2],
            "max_lon": FETCH_BBOX[3],
        },
        "supersample": SUPERSAMPLE,
        "palette_colors": args.colors or 0,
        "background": _hex(C_BACKGROUND),
        "labels": labelled,
        "layers": [s.as_json() for s in stats],
        "overpass_queries": [{"name": n, "query": q} for n, q in sorted(queries.items())],
    }
    json_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    log(f"render: {render_seconds:.1f} s")
    for stat in stats:
        log(f"  {stat.name:<8} {stat.elements:>5} elements -> {stat.drawn:>5} shapes {stat.detail}")
    log(f"wrote {png_path} ({png_path.stat().st_size / 1024:.0f} KB) and {json_path.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "assets" / "basemap"),
        help="where the PNG and the JSON go (default: assets/basemap)",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(Path.home() / ".cache" / "mushroom-alerts" / "basemap"),
        help="keep Overpass answers here so a rerun does not refetch",
    )
    parser.add_argument("--no-cache", action="store_true", help="do not read or write the cache")
    parser.add_argument("--refresh", action="store_true", help="refetch even when cached")
    parser.add_argument(
        "--no-streams",
        action="store_true",
        help=f"drop waterway=stream entirely (default: keep the ones at least "
        f"{STREAM_MIN_KM} km long)",
    )
    parser.add_argument(
        "--colors",
        type=int,
        default=256,
        help="quantise the PNG to this many palette colours (0 = keep RGB). "
        "256, not the 128 the flat first map could afford: measured against "
        "the RGB original, 128 washes the darkest label ink out by 109 levels "
        "and moves 2.7%% of the map by more than 16; 256 keeps that tail at "
        "0.7%% and costs 84 KB",
    )
    parser.add_argument("--quiet", action="store_true")
    return build(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
