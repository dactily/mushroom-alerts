"""The committed basemap and the projection on to it.

Everything here is offline and reads the artefacts that
``tools/build_basemap.py`` produced: ``assets/basemap/vsetinsko.json`` and
the PNG next to it.  The geometry assertions are the contract the marker
renderer leans on -- if one of them fails, the picture and the maths have
drifted apart and the map is quietly lying about where things are.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from mushroom_alerts.locations import load_locations
from mushroom_alerts.mapping import basemap as B
from mushroom_alerts.mapping.render import COLOR_NONE, COLOR_STOPS

REPO = Path(__file__).resolve().parent.parent
SHIPPED_LOCATIONS = REPO / "locations.yaml"

#: Every watched location must sit this far inside the picture, so the
#: marker (and its halo) has room and never straddles the border.
MIN_MARGIN_KM = 2.0

#: What the renderer paints on top of this map: the three stops of the
#: chance scale and the grey for "no number".  Imported rather than copied,
#: so a repainted scale cannot quietly stop reading against the background.
MARKER_FILLS = tuple(colour for _, colour in COLOR_STOPS) + (COLOR_NONE,)


@pytest.fixture(scope="module")
def bm() -> B.Basemap:
    return B.load_basemap()


def from_pixel(bm: B.Basemap, x: float, y: float) -> tuple[float, float]:
    """The inverse of :meth:`Basemap.to_pixel`, for the round-trip."""
    x0, x1 = B.mercator_x(bm.min_lon), B.mercator_x(bm.max_lon)
    y0, y1 = B.mercator_y(bm.min_lat), B.mercator_y(bm.max_lat)
    lon = math.degrees((x / bm.width * (x1 - x0) + x0) / B.EARTH_RADIUS_M)
    lat = B.inverse_mercator_y(y1 - y / bm.height * (y1 - y0))
    return lat, lon


def luminance(colour: tuple[int, int, int]) -> float:
    """ITU-R 601-2 luma -- the same weights ``Image.convert("L")`` uses."""
    return 0.299 * colour[0] + 0.587 * colour[1] + 0.114 * colour[2]


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance, km."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


# -- the artefacts -------------------------------------------------------


def test_the_shipped_basemap_loads(bm):
    assert bm.name == "vsetinsko"
    assert (bm.width, bm.height) == (1200, 1000)
    assert bm.attribution == "© OpenStreetMap contributors"
    assert bm.built_at and "OpenStreetMap" in bm.source
    assert bm.image_path.is_file()


def test_the_sidecar_matches_the_committed_png(bm):
    """A resized PNG with a stale sidecar would misplace every marker."""
    from PIL import Image

    with Image.open(bm.image_path) as im:
        assert im.size == (bm.width, bm.height)


def test_the_png_is_small_enough_to_commit(bm):
    assert bm.image_path.stat().st_size < 1_500_000


def test_the_sidecar_records_how_it_was_built():
    data = json.loads(B.DEFAULT_BASEMAP.read_text(encoding="utf-8"))
    layers = {"forest", "settlements", "water", "roads", "places"}
    assert data["projection"] == "EPSG:3857"
    assert layers == {l["name"] for l in data["layers"]}
    assert all(layer["drawn"] > 0 for layer in data["layers"])
    names = {q["name"] for q in data["overpass_queries"]}
    assert names == layers
    assert all("out geom;" in q["query"] for q in data["overpass_queries"])


def test_the_fonts_are_vendored_and_cover_czech_and_cyrillic():
    """The server's fonts are not ours to rely on; these two are."""
    from PIL import Image, ImageFont

    for path in (B.FONT_PATH, B.FONT_BOLD_PATH):
        assert path.is_file(), path
        font = ImageFont.truetype(str(path), 24)

        def glyph(char: str) -> bytes:
            mask = font.getmask(char, mode="L")
            return bytes(Image.frombytes("L", mask.size, bytes(mask)).tobytes())

        notdef = glyph("￿")  # the empty box a missing glyph draws
        for char in "řůíěšžýáéАБВГДЕабвгдеёіїєґ":
            assert glyph(char) != notdef, f"{path.name} has no {char!r}"


# -- projection ----------------------------------------------------------


def test_the_mercator_aspect_matches_the_pixel_aspect(bm):
    """Undistorted by construction: the projected box has the shape of the
    image, so a kilometre is the same number of pixels whichever way it
    runs."""
    span_x = B.mercator_x(bm.max_lon) - B.mercator_x(bm.min_lon)
    span_y = B.mercator_y(bm.max_lat) - B.mercator_y(bm.min_lat)
    assert span_y / span_x == pytest.approx(bm.height / bm.width, rel=1e-12)


@pytest.mark.parametrize(
    "corner,expected",
    [
        ("min_lat_min_lon", (0.0, 1000.0)),
        ("max_lat_max_lon", (1200.0, 0.0)),
        ("min_lat_max_lon", (1200.0, 1000.0)),
        ("max_lat_min_lon", (0.0, 0.0)),
        ("centre", (600.0, 500.0)),
    ],
)
def test_the_corners_and_the_centre_land_where_they_should(bm, corner, expected):
    lat, lon = {
        "min_lat_min_lon": (bm.min_lat, bm.min_lon),
        "max_lat_max_lon": (bm.max_lat, bm.max_lon),
        "min_lat_max_lon": (bm.min_lat, bm.max_lon),
        "max_lat_min_lon": (bm.max_lat, bm.min_lon),
        "centre": (bm.centre_lat, bm.centre_lon),
    }[corner]
    assert bm.to_pixel(lat, lon) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize(
    "lat,lon",
    [
        (49.16906695848169, 17.749),
        (49.54899925170674, 18.449),
        (49.3594, 18.0990),
        (49.4718, 17.9711),
        (49.2095, 18.4106),
    ],
)
def test_pixels_round_trip_back_to_the_same_point(bm, lat, lon):
    x, y = bm.to_pixel(lat, lon)
    back_lat, back_lon = from_pixel(bm, x, y)
    assert back_lat == pytest.approx(lat, abs=1e-9)
    assert back_lon == pytest.approx(lon, abs=1e-9)


def test_north_is_up_and_east_is_right(bm):
    centre_x, centre_y = bm.to_pixel(bm.centre_lat, bm.centre_lon)
    north_x, north_y = bm.to_pixel(bm.centre_lat + 0.05, bm.centre_lon)
    east_x, east_y = bm.to_pixel(bm.centre_lat, bm.centre_lon + 0.05)
    assert north_y < centre_y  # further north -> smaller y (origin top-left)
    assert north_x == pytest.approx(centre_x)
    assert east_x > centre_x
    assert east_y == pytest.approx(centre_y)


def test_latitude_rows_are_not_evenly_spaced(bm):
    """Mercator stretches northwards, so equal steps in latitude must give
    *growing* steps in y -- otherwise we projected with a linear scale and
    the picture and the markers disagree by a few pixels."""
    ys = [bm.to_pixel(bm.min_lat + 0.05 * i, bm.centre_lon)[1] for i in range(7)]
    gaps = [ys[i] - ys[i + 1] for i in range(len(ys) - 1)]
    assert all(gap > 0 for gap in gaps)
    assert all(gaps[i] < gaps[i + 1] for i in range(len(gaps) - 1))


def test_km_per_pixel_is_about_forty_metres(bm):
    """~50 km across 1200 px."""
    assert bm.km_per_pixel() == pytest.approx(0.0423, abs=0.001)
    assert bm.km_per_pixel() * bm.width == pytest.approx(50.8, abs=0.5)
    # Mercator's scale shrinks towards the equator, so a southern row covers
    # slightly more ground per pixel than a northern one.
    assert bm.km_per_pixel(bm.min_lat) > bm.km_per_pixel(bm.max_lat)


def test_the_scale_agrees_with_the_real_distance_between_two_locations(bm):
    """The projection is only worth anything if pixels measure ground."""
    locations = {l.slug: l for l in load_locations(SHIPPED_LOCATIONS)}
    a, b = locations["valmez"], locations["bumbalka"]
    ax, ay = bm.to_pixel(a.lat, a.lon)
    bx, by = bm.to_pixel(b.lat, b.lon)
    pixels = math.hypot(bx - ax, by - ay)
    real = km_between(a.lat, a.lon, b.lat, b.lon)
    assert pixels * bm.km_per_pixel() == pytest.approx(real, rel=0.01)


# -- the watched locations -----------------------------------------------


def test_every_watched_location_is_well_inside_the_picture(bm):
    locations = load_locations(SHIPPED_LOCATIONS)
    assert len(locations) == 13
    for loc in locations:
        assert bm.contains(loc.lat, loc.lon), loc.slug
        x, y = bm.to_pixel(loc.lat, loc.lon, label=loc.slug)
        assert 0 < x < bm.width and 0 < y < bm.height, loc.slug
        margins = {
            "west": km_between(loc.lat, loc.lon, loc.lat, bm.min_lon),
            "east": km_between(loc.lat, loc.lon, loc.lat, bm.max_lon),
            "south": km_between(loc.lat, loc.lon, bm.min_lat, loc.lon),
            "north": km_between(loc.lat, loc.lon, bm.max_lat, loc.lon),
        }
        worst = min(margins, key=margins.get)
        assert margins[worst] >= MIN_MARGIN_KM, f"{loc.slug}: {worst} {margins[worst]:.2f} km"


def test_a_point_outside_is_refused_never_clamped(bm):
    prague = (50.0755, 14.4378)
    assert not bm.contains(*prague)
    with pytest.raises(B.OutsideBasemap) as excinfo:
        bm.to_pixel(*prague, label="praha")
    message = str(excinfo.value)
    assert "praha" in message and "50.0755" in message and "14.4378" in message
    assert "49.169067" in message and "18.449000" in message
    assert excinfo.value.bbox == (bm.min_lon, bm.min_lat, bm.max_lon, bm.max_lat)
    assert isinstance(excinfo.value, ValueError)  # callers may catch either


@pytest.mark.parametrize(
    "lat,lon",
    [(49.16, 18.0), (49.56, 18.0), (49.3594, 17.70), (49.3594, 18.50)],
)
def test_just_outside_each_edge_raises(bm, lat, lon):
    with pytest.raises(B.OutsideBasemap):
        bm.to_pixel(lat, lon)


def test_the_edges_themselves_are_inside(bm):
    for lat, lon in (
        (bm.min_lat, bm.min_lon),
        (bm.max_lat, bm.max_lon),
        (bm.min_lat, bm.max_lon),
        (bm.max_lat, bm.min_lon),
    ):
        assert bm.contains(lat, lon)
        bm.to_pixel(lat, lon)


# -- the picture ---------------------------------------------------------


def test_open_image_hands_out_a_private_copy(bm):
    first = bm.open_image()
    assert first.mode == "RGB"
    assert first.size == (bm.width, bm.height)
    first.putpixel((0, 0), (255, 0, 255))
    second = bm.open_image()
    assert second.getpixel((0, 0)) != (255, 0, 255)
    assert second is not first


def test_the_map_stays_the_paper_under_the_markers(bm):
    """The markers are the loud part; a dark basemap would swallow them.

    Not "the map is nearly white" -- it carries forest, rivers, roads and
    towns on purpose, and the flat version of it that was nearly white said
    nothing at all at the 390 px width Telegram previews a photo at.  What
    still has to hold is what the renderer actually leans on: whatever the
    map is made of, it is the paper and the markers are the ink.  So the
    map's average tone sits above the *lightest* fill a marker can have
    (amber, 50 %), and next to none of it is as dark as the darkest one.
    """
    grey = bm.open_image().convert("L")
    histogram = grey.histogram()
    total = bm.width * bm.height
    average = sum(value * count for value, count in enumerate(histogram)) / total
    assert average > max(luminance(fill) for fill in MARKER_FILLS)
    darkest = min(luminance(fill) for fill in MARKER_FILLS)
    assert sum(histogram[: int(darkest) + 1]) / total < 0.01


# -- loading -------------------------------------------------------------


def test_load_basemap_finds_the_png_next_to_the_json(tmp_path, bm):
    sidecar = json.loads(B.DEFAULT_BASEMAP.read_text(encoding="utf-8"))
    sidecar.pop("image")
    path = tmp_path / "vsetinsko.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")
    assert B.load_basemap(path).image_path == tmp_path / "vsetinsko.png"


@pytest.mark.parametrize(
    "patch,message",
    [
        ({"projection": "EPSG:4326"}, "unsupported projection"),
        ({"bbox": {"min_lon": 18.5, "min_lat": 49.1, "max_lon": 17.7, "max_lat": 49.5}}, "empty"),
        ({"width": 0}, "bad image size"),
        ({"bbox": {"min_lon": "x"}}, "not a basemap sidecar"),
    ],
)
def test_a_broken_sidecar_says_so(tmp_path, patch, message):
    sidecar = json.loads(B.DEFAULT_BASEMAP.read_text(encoding="utf-8")) | patch
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        B.load_basemap(path)
