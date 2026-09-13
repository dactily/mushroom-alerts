#!/usr/bin/env python3
"""Build the committed basemap of the forecast area from OpenStreetMap.

**This is a one-off tool, run by hand.**  Nothing on the daily path may
import it: the daily render opens the PNG this produces and draws markers
on it, with no network and no tiles -- see
``mushroom_alerts/mapping/basemap.py`` and ``assets/basemap/README.md``.

    .venv/bin/python tools/build_basemap.py

It asks Overpass for four layers (forest, water, roads, towns) inside a box
a little larger than the map, draws them with Pillow at 2x and downsamples,
and writes ``assets/basemap/vsetinsko.png`` plus a ``.json`` sidecar that
records the bounding box, the queries and the layer counts.

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

# -- colours (a light, quiet map: the markers on top must be the loud bit) --

C_BACKGROUND = (246, 244, 238)  # #F6F4EE warm off-white
C_FOREST = (201, 218, 187)  # #C9DABB muted green
C_WATER = (177, 208, 227)  # #B1D0E3 soft blue
#: Roads have to read on the off-white background *and* on the forest
#: green, which sit at almost the same luminance -- hence the darker greys.
C_ROAD_MINOR = (199, 192, 181)  # #C7C0B5 secondary
C_ROAD_MAJOR = (183, 174, 160)  # #B7AEA0 primary
C_ROAD_TRUNK = (166, 156, 140)  # #A69C8C motorway, trunk
C_LABEL = (74, 70, 64)  # #4A4640
C_LABEL_HALO = (250, 249, 246)
C_ATTRIBUTION = (138, 133, 125)

#: Line widths in 2x pixels (so "3" is 1.5 px on the finished map).
W_STREAM = 2
W_RIVER = 3
W_ROAD = {"secondary": 2, "primary": 3, "trunk": 4, "motorway": 4}

LABEL_SIZE = 26  # 2x pixels -> 13 px on the finished map
LABEL_HALO = 4
LABEL_GAP = 9
DOT_RADIUS = 5
ATTRIBUTION_SIZE = 20

#: Labelled for orientation, always, even if they crowd something else.
PRIORITY_PLACES = ("Valašské Meziříčí", "Vsetín", "Rožnov pod Radhoštěm")


def _bbox_clause() -> str:
    return ",".join(f"{v}" for v in FETCH_BBOX)


def _query(body: str) -> str:
    return f"[out:json][timeout:{OVERPASS_TIMEOUT}];\n{body.strip()}\nout geom;\n"


BBOX = _bbox_clause()


def build_queries(*, streams: bool) -> dict[str, str]:
    """The four Overpass queries, verbatim into the sidecar and the README.

    ``out geom;`` makes every way and every relation member carry its own
    coordinates, so one request per layer is all we need -- no node lookups,
    no recursion.
    """
    water = [
        f'  way["natural"="water"]({BBOX});',
        f'  relation["natural"="water"]({BBOX});',
        f'  way["waterway"="river"]({BBOX});',
    ]
    if streams:
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
        "water": _query("(\n" + "\n".join(water) + "\n);"),
        "roads": _query(f"""way["highway"~"^(motorway|trunk|primary|secondary)$"]({BBOX});"""),
        "places": _query(f"""node["place"~"^(city|town)$"]({BBOX});"""),
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
    if closed and (tags.get("landuse") == "forest" or tags.get("natural") in {"wood", "water"}):
        classes.add("area")
    waterway = tags.get("waterway")
    if not closed and waterway in {"river", "stream"}:
        classes.add(waterway)
    highway = tags.get("highway")
    if highway in W_ROAD:
        classes.add(highway)
    return classes


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


def draw_areas(
    canvas: Image.Image,
    size: tuple[int, int],
    project: Projector,
    payload: dict,
    colour: tuple[int, int, int],
    *,
    lines: list[tuple[Ring, int]] | None = None,
) -> tuple[int, dict[str, int]]:
    outer, inner, counts = areas(payload)

    def paint(draw: ImageDraw.ImageDraw) -> None:
        for ring in outer:
            draw.polygon(project.line(ring), fill=255)
        for ring in inner:  # holes, after every fill
            draw.polygon(project.line(ring), fill=0)
        for way, width in lines or []:
            draw.line(project.line(way), fill=255, width=width, joint="curve")

    _paint(canvas, size, colour, paint)
    counts["holes"] = len(inner)
    counts["rings"] = len(outer)
    return len(outer) + len(inner) + len(lines or []), counts


def draw_roads(canvas: Image.Image, project: Projector, payload: dict) -> tuple[int, dict]:
    """Thin grey lines, quiet classes first so the trunks stay on top."""
    draw = ImageDraw.Draw(canvas)
    by_class: dict[str, list[Ring]] = {k: [] for k in W_ROAD}
    for element in elements(payload):
        if element.get("type") != "way":
            continue
        for name in _way_class(element) & set(W_ROAD):
            geometry = _geometry(element)
            if len(geometry) >= 2:
                by_class[name].append(geometry)
    colours = {
        "secondary": C_ROAD_MINOR,
        "primary": C_ROAD_MAJOR,
        "trunk": C_ROAD_TRUNK,
        "motorway": C_ROAD_TRUNK,
    }
    drawn = 0
    for name in ("secondary", "primary", "trunk", "motorway"):
        for way in by_class[name]:
            draw.line(project.line(way), fill=colours[name], width=W_ROAD[name], joint="curve")
            drawn += 1
    return drawn, {k: len(v) for k, v in sorted(by_class.items())}


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    kind: str
    population: int


def places(payload: dict, bbox: tuple[float, float, float, float]) -> list[Place]:
    """Towns inside the drawn box, the three orientation ones first."""
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
            return (0, PRIORITY_PLACES.index(place.name), "")
        except ValueError:
            return (1, -place.population, place.name)

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
    out rather than smeared over the map.
    """
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(str(FONT_PATH), LABEL_SIZE)
    taken = list(reserved)
    margin = 6 * SUPERSAMPLE
    edge = 14 * SUPERSAMPLE  # a dot glued to the border reads as a mistake
    labelled: list[str] = []
    for place in found:
        x, y = project(place.lat, place.lon)
        if not (edge <= x <= size[0] - edge and edge <= y <= size[1] - edge):
            continue
        dot = (x - DOT_RADIUS, y - DOT_RADIUS, x + DOT_RADIUS, y + DOT_RADIUS)
        if any(_overlaps(dot, box) for box in taken):
            continue
        for anchor, point in (
            ("lm", (x + LABEL_GAP, y)),
            ("rm", (x - LABEL_GAP, y)),
            ("ma", (x, y + LABEL_GAP)),
            ("md", (x, y - LABEL_GAP)),
        ):
            box = draw.textbbox(point, place.name, font=font, anchor=anchor)
            box = (box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3)
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

    queries = build_queries(streams=args.streams)

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

    waterways: list[tuple[Ring, int]] = []
    for element in elements(payloads["water"]):
        if element.get("type") != "way":
            continue
        classes = _way_class(element)
        width = W_RIVER if "river" in classes else (W_STREAM if "stream" in classes else 0)
        if width:
            geometry = _geometry(element)
            if len(geometry) >= 2:
                waterways.append((geometry, width))
    water = LayerStat("water", _hex(C_WATER), len(payloads["water"].get("elements", [])))
    water.drawn, water.detail = draw_areas(
        canvas, size, project, payloads["water"], C_WATER, lines=waterways
    )
    water.detail["waterways"] = len(waterways)
    stats.append(water)

    roads = LayerStat("roads", _hex(C_ROAD_MAJOR), len(payloads["roads"].get("elements", [])))
    roads.drawn, roads.detail = draw_roads(canvas, project, payloads["roads"])
    stats.append(roads)

    credit_box = draw_attribution(canvas, size)
    found = places(payloads["places"], bbox)
    labels = LayerStat("places", _hex(C_LABEL), len(payloads["places"].get("elements", [])))
    labels.drawn, labelled = draw_places(canvas, size, project, found, [credit_box])
    labels.detail = {"inside_bbox": len(found)}
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
        "--streams",
        action="store_true",
        help="also draw waterway=stream (noisy at this scale; off by default)",
    )
    parser.add_argument(
        "--colors",
        type=int,
        default=128,
        help="quantise the PNG to this many palette colours (0 = keep RGB)",
    )
    parser.add_argument("--quiet", action="store_true")
    return build(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
