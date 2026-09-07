from __future__ import annotations

import math
from datetime import date

import pytest
from PIL import Image

from mushroom_alerts import fetch_chmi_map as m
from mushroom_alerts.base import haversine_km

from .conftest import BYSTRICE, VALMEZ

SIZE = (2755, 1600)
TODAY = date(2026, 9, 7)


# ----------------------------------------------------------------------
# pixel math
# ----------------------------------------------------------------------
def test_pixel_for_bbox_corners():
    min_lon, min_lat, max_lon, max_lat = m.BBOX
    w, h = SIZE
    assert m.pixel_for(max_lat, min_lon, SIZE) == (0, 0)
    # bottom-right corner clamps to the last pixel, not one past the end
    assert m.pixel_for(min_lat, max_lon, SIZE) == (w - 1, h - 1)


def test_pixel_for_longitude_is_linear():
    min_lon, _, max_lon, _ = m.BBOX
    mid_lon = (min_lon + max_lon) / 2
    x, _ = m.pixel_for(50.0, mid_lon, SIZE)
    assert x == pytest.approx(SIZE[0] / 2, abs=1)


def test_pixel_for_is_web_mercator_not_plate_carree():
    """The raster is EPSG:3857; plate carrée would be ~10 px off here."""
    min_lon, min_lat, max_lon, max_lat = m.BBOX
    lat = 49.4718
    _, y = m.pixel_for(lat, 17.9711, SIZE)
    plate = (max_lat - lat) / (max_lat - min_lat) * SIZE[1]
    merc = (m._merc_y(max_lat) - m._merc_y(lat)) / (m._merc_y(max_lat) - m._merc_y(min_lat)) * SIZE[1]
    assert abs(y - merc) <= 1
    assert abs(y - plate) > 5


def test_pixel_for_scales_with_image_size():
    x1, y1 = m.pixel_for(49.4718, 17.9711, SIZE)
    x2, y2 = m.pixel_for(49.4718, 17.9711, (SIZE[0] // 5, SIZE[1] // 5))
    assert x2 == pytest.approx(x1 / 5, abs=1)
    assert y2 == pytest.approx(y1 / 5, abs=1)


def test_pixel_for_clamps_outside_bbox():
    assert m.pixel_for(0.0, 0.0, SIZE) == (0, SIZE[1] - 1)


# ----------------------------------------------------------------------
# palette majority
# ----------------------------------------------------------------------
def _synthetic(colours: list[tuple[int, int, int, int]]) -> Image.Image:
    """5x5 image, one colour per pixel, centred on the point we sample."""
    img = Image.new("RGBA", SIZE, (255, 255, 255, 0))
    x, y = m.pixel_for(49.4718, 17.9711, SIZE)
    px = img.load()
    i = 0
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            px[x + dx, y + dy] = colours[i % len(colours)]
            i += 1
    return img


def test_level_at_uniform_colour():
    for rgb, level in m.PALETTE.items():
        img = _synthetic([(*rgb, 255)])
        got, _, votes = m.level_at(img, 49.4718, 17.9711)
        assert got == level
        assert votes == 25


def test_level_at_majority_wins():
    green = (134, 203, 102, 255)  # level 4
    yellow = (255, 255, 191, 255)  # level 3
    # 13 green / 12 yellow by alternating over 25 pixels
    img = _synthetic([green, yellow])
    got, _, votes = m.level_at(img, 49.4718, 17.9711)
    assert got == 4
    assert votes == 13


def test_level_at_ignores_transparent_and_unknown_colours():
    yellow = (255, 255, 191, 255)
    noise = (1, 2, 3, 255)  # not a palette colour -> no data, no vote
    transparent = (255, 255, 191, 0)  # palette colour but alpha 0
    img = _synthetic([noise, transparent, noise, transparent, yellow])
    got, _, votes = m.level_at(img, 49.4718, 17.9711)
    assert got == 3
    assert votes == 5


def test_level_at_no_data_returns_none():
    img = Image.new("RGBA", SIZE, (255, 255, 255, 0))
    got, px, votes = m.level_at(img, 49.4718, 17.9711)
    assert got is None and votes == 0 and len(px) == 2


# ----------------------------------------------------------------------
# time label
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "label,expected",
    [
        ("ne 6. 9.", date(2026, 9, 6)),
        ("po 7.9.", date(2026, 9, 7)),
        ("st 1. 1.", date(2026, 1, 1)),  # never resolved into the future
        ("út 8. 9.", date(2026, 9, 8)),  # tomorrow is still plausible
        ("6. 9. 2025", date(2025, 9, 6)),
        ("", None),
        (None, None),
        ("nesmysl", None),
    ],
)
def test_parse_time_label(label, expected):
    assert m.parse_time_label(label, TODAY) == expected


def test_parse_time_label_rolls_the_year():
    # a "20. 12." label seen in early January belongs to the previous year
    assert m.parse_time_label("so 20. 12.", date(2027, 1, 3)) == date(2026, 12, 20)


# ----------------------------------------------------------------------
# fetch, against the recorded 2026-09-07 payload
# ----------------------------------------------------------------------
def test_fetch_live_fixture(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    assert result.ok and result.error is None
    by_slug = {r.location: r for r in result.readings}
    assert by_slug["valmez"].value == 3.0  # PLAN §1 snapshot: Valmez 3/5
    assert by_slug["valasska-bystrice"].value == 3.0
    r = by_slug["valmez"]
    assert r.source == "chmi_map" and r.metric == "level"
    assert r.text == "ne 6. 9." and r.date == date(2026, 9, 6)
    assert r.meta["px"] == [2308, 967]
    assert r.meta["label"] == "střední"
    assert "stale" not in r.meta


def test_fetch_marks_stale_when_the_map_stops_updating(http, locations):
    result = m.fetch(locations, http=http, today=date(2026, 9, 20))
    assert all(r.meta["stale"] for r in result.readings)


def test_fetch_never_raises(locations):
    from .conftest import FakeHttp

    result = m.fetch(locations, http=FakeHttp(fail=True), today=TODAY)
    assert result.ok is False and "ConnectionError" in (result.error or "")
    assert result.readings == []


def test_fetch_survives_garbage(locations):
    class Broken:
        def get_json(self, url, params=None):
            return {"timeLabel": "ne 6. 9.", "img": "not-a-png"}

    result = m.fetch(locations, http=Broken(), today=TODAY)
    assert result.ok is False and result.readings == []


def test_decode_image_size(chmi_payload):
    img = m.decode_image(chmi_payload)
    assert img.size == SIZE and img.mode == "RGBA"


def test_palette_ordering_agrees_with_houbymapa(chmi_payload, houbymapa_payload):
    """Independent check of the red->green level ordering (see module docs).

    Sampling the ČHMÚ raster at every HoubyMapa cell centre must correlate
    *positively* with HoubyMapa's own numeric level.  With the ordering
    reversed the correlation would be equally strongly negative.
    """
    img = m.decode_image(chmi_payload)
    pairs = []
    for cell in houbymapa_payload["cells"]:
        level, _, _ = m.level_at(img, float(cell["lat"]), float(cell["lng"]))
        if level is not None:
            pairs.append((level, float(cell["l"])))
    assert len(pairs) > 200
    n = len(pairs)
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    cov = sum((a - mx) * (b - my) for a, b in pairs)
    sx = math.sqrt(sum((a - mx) ** 2 for a, _ in pairs))
    sy = math.sqrt(sum((b - my) ** 2 for _, b in pairs))
    assert cov / (sx * sy) > 0.4
    within_one = sum(1 for a, b in pairs if abs(a - b) <= 1) / n
    assert within_one > 0.7


def test_raster_extent_matches_czechia(chmi_payload):
    """The opaque mask must line up with the real border under this
    projection -- the check that caught the plate-carrée assumption."""
    img = m.decode_image(chmi_payload)
    left, _, right, _ = img.getchannel("A").getbbox()
    # westernmost point of Czechia: 12.0906 E, 50.2521 N
    wx, wy = m.pixel_for(50.2521, 12.0906, img.size)
    assert abs(wx - left) <= 2
    # easternmost point: 18.8592 E, 49.5511 N
    ex, ey = m.pixel_for(49.5511, 18.8592, img.size)
    assert abs(ex - right) <= 3

    def opaque_rows(x: int) -> list[int]:
        return [y for y in range(img.size[1]) if img.getpixel((x, y))[3] > 0]

    # ... and those extreme columns must be opaque at the predicted latitude,
    # which is what distinguishes Mercator (off by <=1 px) from plate carrée
    # (off by 9-11 px here).
    for x, y in ((left, wy), (right - 1, ey)):
        rows = opaque_rows(x)
        assert rows, f"column {x} is empty"
        assert min(rows) - 2 <= y <= max(rows) + 2


def test_params_for_matches_pixel_for():
    params = m.params_for(VALMEZ, SIZE)
    assert params["chmi_px"] == list(m.pixel_for(VALMEZ.lat, VALMEZ.lon, SIZE))
    assert params["chmi_px_size"] == [2755, 1600]


def test_the_two_locations_land_in_different_pixels():
    a = m.pixel_for(VALMEZ.lat, VALMEZ.lon, SIZE)
    b = m.pixel_for(BYSTRICE.lat, BYSTRICE.lon, SIZE)
    assert a != b
    assert haversine_km(VALMEZ.lat, VALMEZ.lon, BYSTRICE.lat, BYSTRICE.lon) > 8
