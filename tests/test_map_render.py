"""``mapping.render``: the picture that must say exactly what the block says.

The text block is the authority (``mushroom_alerts/report.py``); the image
is the same numbers, arranged so the reader can see *where* they are.  Two
kinds of regression are therefore worth more than any pixel:

* the image inventing a number, a date or an order of its own -- every test
  here renders from a summary and compares against the block rendered from
  the *same* summary, never against a hand-written expectation;
* a location quietly disappearing.  A label that does not fit may lose its
  name; a point outside the basemap may lose its dot; neither may lose its
  line in the list under the map or its mention on the picture.

Nothing here reads pixels back: :func:`~mushroom_alerts.mapping.render.build_layout`
returns the placed text, boxes and colours, which is what the drawing step
consumes, so asserting on it is asserting on what will be drawn.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta

import pytest

from mushroom_alerts import report as report_lib
from mushroom_alerts.base import Location
from mushroom_alerts.locations import load_locations
from mushroom_alerts.mapping import render as render_lib

from tests.fake_basemap import FakeBasemap

#: The daily job runs every morning; 13.09.2026 is a Sunday, so its block
#: says ``13.09``.  The weekend job runs on the Friday before, and its block
#: speaks about ``12–13.09`` -- the two dates are different on purpose.
TODAY = date(2026, 9, 13)
FRIDAY = date(2026, 9, 11)
HORIZON = 16

SHIPPED = load_locations(
    __import__("pathlib").Path(__file__).resolve().parent.parent / "locations.yaml"
)


# ----------------------------------------------------------------------
# fixtures: the shape ``views``/``brief`` hand to ``report.summarize``
# ----------------------------------------------------------------------
def chance_block(today, value, *, curve=None, capped=False):
    days = {
        today + timedelta(days=n): (curve or {}).get(n, value) for n in range(HORIZON + 1)
    }
    known = {day: number for day, number in days.items() if number is not None}
    peak = min(known.items(), key=lambda pair: (-pair[1], pair[0])) if known else None
    return {"today": days[today], "curve": days, "peak": peak, "capped": capped}


def location_view(
    location: Location,
    *,
    chance: int | None,
    tomorrow: int | None = None,
    today: date = TODAY,
    offset: int = 0,
    capped: bool = False,
    houby: float = 0.63,
    chmi: float = 3.0,
    api30: float = 35.9,
):
    """One location as the read model produces it, reduced to what the
    report reads.  ``chance=None`` is a location with no usable API30;
    ``offset`` says which day of the curve the first number lands on -- 0
    for the daily run, 1 for a Friday run whose Saturday is tomorrow."""
    anchor = today - timedelta(days=1)
    curve = {offset: chance, offset + 1: chance if tomorrow is None else tomorrow}
    return {
        "slug": location.slug,
        "name": location.name,
        "short_name": location.short_name,
        "chmi": {"level": chmi},
        "houbymapa": {"level": 4.0, "score": houby},
        "station": {
            "api30_mm": 23.7,
            "sra": [{"window_days": 1, "mm": 0.0}, {"window_days": 7, "mm": 25.6}],
        },
        "forecast": {
            "days": [
                {"date": today + timedelta(days=n), "offset": n, "verdict": "средняя"}
                for n in range(HORIZON + 1)
            ],
            "today_mm": api30,
            "api30_quality": "fresh" if chance is not None else "stale",
            "next_rain": (today + timedelta(days=5), 27.4),
        },
        "biological": {
            "chance": chance_block(today, chance, curve=curve, capped=capped),
            "rain_episode": {
                "date": anchor,
                "growth_window": [anchor + timedelta(days=7), anchor + timedelta(days=12)],
                "residual_window_end": anchor + timedelta(days=21),
            },
            "guidance": {
                "verdict": "medium",
                "verdict_label": "средняя",
                "phase": "primary_window",
                "dominant_event_id": "rain-1",
                "candidate_high_date": None,
            },
        },
    }


#: Deliberately not sorted, and deliberately not the config order of the
#: numbers: two blind locations, a leader in the middle, a cap at the end.
DEMO_CHANCES: dict[str, tuple[int | None, int | None]] = {
    "valmez": (45, 50),
    "jablunka": (40, 45),
    "valasska-bystrice": (60, 65),
    "maruska": (35, 35),
    "benesky": (70, 75),
    "kohutka": (65, 60),
    "lidecko-lacnov": (30, 35),
    "bumbalka": (75, 70),
    "meduvka": (50, 55),
    "semetinske-lesy": (None, None),
    "rajnochovicke-lesy": (55, 50),
    "kamenarka": (25, 30),
    "pod-vartovnou": (20, 25),
}


def demo_brief(mode="daily", locations=None, chances=None, today=None):
    """The two numbers of ``chances`` are today/tomorrow for the daily run
    and Saturday/Sunday for the Friday one."""
    today = today or (FRIDAY if mode == report_lib.WEEKEND else TODAY)
    offset = 1 if mode == report_lib.WEEKEND else 0
    locations = SHIPPED if locations is None else locations
    chances = DEMO_CHANCES if chances is None else chances
    return {
        "locations": [
            location_view(
                loc,
                chance=chances.get(loc.slug, (40, 40))[0],
                tomorrow=chances.get(loc.slug, (40, 40))[1],
                today=today,
                offset=offset,
                capped=loc.slug == "kamenarka",
            )
            for loc in locations
        ],
        "notes": [],
        "failed_sources": [],
    }


def demo_summary(mode="daily", *, locations=None, chances=None, today=None):
    today = today or (FRIDAY if mode == report_lib.WEEKEND else TODAY)
    return report_lib.summarize(
        demo_brief(mode, locations, chances, today), mode=mode, today=today
    )


@pytest.fixture
def basemap():
    return FakeBasemap()


def layout_of(summary, basemap, locations=None):
    return render_lib.build_layout(
        summary, locations=SHIPPED if locations is None else locations, basemap=basemap
    )


# ----------------------------------------------------------------------
# reading the block back, so the image can be compared against it
# ----------------------------------------------------------------------
def block_values(text: str) -> dict[str, tuple[int | None, ...]]:
    """``{"Valmez": (45,)}`` -- the numbers the ШАНС list actually printed."""
    out: dict[str, tuple[int | None, ...]] = {}
    inside = False
    for line in text.splitlines():
        if line.startswith("ШАНС"):
            inside = True
            continue
        if inside and not line.startswith("  "):
            break
        if not inside:
            continue
        name, _, tail = line.strip().partition(" — ")
        values = []
        for part in tail.split(", "):
            found = re.search(r"(\d+) %", part)
            values.append(int(found.group(1)) if found else None)
        out[name] = tuple(values)
    return out


def boxes_of(layout) -> list[tuple[int, int, int, int]]:
    radius = render_lib.MARKER_RADIUS
    dots = [
        (m.x - radius, m.y - radius, m.x + radius, m.y + radius) for m in layout.markers
    ]
    return dots + [m.box for m in layout.markers]


def overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# ----------------------------------------------------------------------
# the numbers and the dates are the block's own
# ----------------------------------------------------------------------
def test_the_daily_image_prints_the_numbers_of_the_block(basemap):
    summary = demo_summary("daily")
    text = report_lib.render(summary, send=True, reason="тест")
    layout = layout_of(summary, basemap)

    printed = block_values(text)
    assert printed, text
    assert {row.short_name: row.values for row in layout.rows} == printed
    on_map = {marker.short_name: marker.values for marker in layout.markers}
    assert on_map == {name: value for name, value in printed.items() if name in on_map}


def test_the_weekend_image_prints_both_days_of_the_block(basemap):
    summary = demo_summary("weekend")
    text = report_lib.render(summary, send=True, reason="тест")
    layout = layout_of(summary, basemap)

    printed = block_values(text)
    assert all(len(values) == 2 for values in printed.values())
    assert {row.short_name: row.values for row in layout.rows} == printed


@pytest.mark.parametrize(
    "mode, expected", [("daily", "13.09"), ("weekend", "Выходные 12–13.09")]
)
def test_the_header_date_is_the_blocks_own(basemap, mode, expected):
    summary = demo_summary(mode)
    layout = layout_of(summary, basemap)
    assert layout.subtitle == expected
    header = report_lib.render(summary, send=True, reason="тест").splitlines()[2]
    assert report_lib.date_label(summary) in header
    assert layout.title == "Грибной прогноз"


def test_the_list_is_the_config_order_never_sorted(basemap):
    summary = demo_summary("daily")
    layout = layout_of(summary, basemap)
    assert [row.slug for row in layout.rows] == [loc.slug for loc in SHIPPED]
    assert [marker.slug for marker in layout.markers] == [loc.slug for loc in SHIPPED]
    # and the order is genuinely not the order of the numbers
    values = [row.values[0] for row in layout.rows if row.values[0] is not None]
    assert values != sorted(values, reverse=True)


def test_every_location_has_a_line_in_the_list(basemap):
    layout = layout_of(demo_summary("daily"), basemap)
    assert len(layout.rows) == len(SHIPPED)
    assert len({row.slug for row in layout.rows}) == len(SHIPPED)


# ----------------------------------------------------------------------
# colour
# ----------------------------------------------------------------------
@pytest.mark.parametrize("percent, expected", [(0, 0), (50, 1), (100, 2)])
def test_the_colour_scale_hits_its_stops(percent, expected):
    assert render_lib.chance_color(percent) == render_lib.COLOR_STOPS[expected][1]


def test_the_colour_scale_is_continuous_and_monotone():
    """Between the stops the colour must move, and always the same way:
    every step greener than the one before it."""
    greenness = [
        render_lib.chance_color(p)[1] - render_lib.chance_color(p)[0] for p in range(101)
    ]
    assert all(b > a for a, b in zip(greenness, greenness[1:]))
    midpoint = render_lib.chance_color(25)
    assert midpoint == (200, 108, 48)  # halfway between red and amber


def test_no_number_is_grey_and_never_zero_percent():
    assert render_lib.chance_color(None) == render_lib.COLOR_NONE
    assert render_lib.chance_color(None) != render_lib.chance_color(0)
    red, green, blue = render_lib.COLOR_NONE
    assert max(red, green, blue) - min(red, green, blue) < 12  # grey, not a hue


def test_a_location_without_a_number_prints_a_dash_in_grey(basemap):
    layout = layout_of(demo_summary("daily"), basemap)
    blind = next(row for row in layout.rows if row.slug == "semetinske-lesy")
    assert blind.values == (None,)
    assert blind.color == render_lib.COLOR_NONE
    assert blind.text.endswith("—")
    assert "0%" not in blind.text
    marker = next(m for m in layout.markers if m.slug == "semetinske-lesy")
    assert marker.color == render_lib.COLOR_NONE
    assert marker.text == "—"


@pytest.mark.parametrize("mode", ["daily", "weekend"])
def test_a_blind_location_prints_one_dash_on_the_map(basemap, mode):
    """«—·—%» would say nothing twice; one dash is the whole statement."""
    layout = layout_of(demo_summary(mode), basemap)
    marker = next(m for m in layout.markers if m.slug == "semetinske-lesy")
    assert marker.text == "—"


def test_the_ink_keeps_red_amber_and_green_apart():
    """Text has to survive white paper without losing what it encodes."""
    low, mid, high = (render_lib._ink(render_lib.chance_color(v)) for v in (0, 50, 100))
    assert low[0] > low[1] and low[0] > low[2]  # red stays red
    assert high[1] > high[0] and high[1] > high[2]  # green stays green
    assert mid[0] > mid[2] and mid[1] > mid[2]  # amber stays warm
    assert all(sum(color) / 3 < 150 for color in (low, mid, high))  # dark enough
    grey = render_lib._ink(render_lib.COLOR_NONE)
    assert max(grey) - min(grey) < 12  # and grey is still grey


def test_the_list_shrinks_rather_than_cutting_a_name(basemap):
    """The list is the authoritative reading: no «Rajnoc…» in it, ever."""
    weekend = layout_of(demo_summary("weekend"), basemap)
    printed = [row.lines[0].text for row in weekend.rows]
    assert printed == [loc.short_name for loc in SHIPPED]
    assert all("…" not in name for name in printed)
    # two values a row need more room, so that column sets in smaller type
    daily = layout_of(demo_summary("daily"), basemap)
    assert daily.rows[0].lines[0].size > weekend.rows[0].lines[0].size
    assert weekend.rows[0].lines[0].size >= render_lib.LIST_FONT_MIN


def test_the_weekend_colours_each_day_on_its_own(basemap):
    layout = layout_of(demo_summary("weekend"), basemap)
    row = next(row for row in layout.rows if row.slug == "valmez")
    assert row.values == (45, 50)
    coloured = [run for run in row.lines[1].runs if run.color != render_lib.MUTED]
    assert [run.text for run in coloured] == ["Сб 45%", "Вс 50%"]
    assert coloured[0].color != coloured[1].color


def test_the_marker_takes_the_better_weekend_day(basemap):
    layout = layout_of(demo_summary("weekend"), basemap)
    marker = next(m for m in layout.markers if m.slug == "kohutka")
    assert marker.values == (65, 60)
    assert marker.color == render_lib.chance_color(65)
    assert "65" in marker.text and "60" in marker.text


# ----------------------------------------------------------------------
# placing the labels
# ----------------------------------------------------------------------
def test_the_labels_never_collide_for_the_thirteen_real_locations(basemap):
    layout = layout_of(demo_summary("daily"), basemap)
    assert len(layout.markers) == len(SHIPPED)
    boxes = boxes_of(layout)
    for index, first in enumerate(boxes):
        for second in boxes[index + 1 :]:
            assert not overlap(first, second), (first, second)
    assert all(marker.placement not in ("forced", "crowded") for marker in layout.markers)
    assert sum(marker.name_shown for marker in layout.markers) >= 1


def test_the_weekend_labels_also_fit(basemap):
    layout = layout_of(demo_summary("weekend"), basemap)
    boxes = boxes_of(layout)
    for index, first in enumerate(boxes):
        for second in boxes[index + 1 :]:
            assert not overlap(first, second)


def test_the_placement_is_deterministic(basemap):
    first = layout_of(demo_summary("daily"), basemap)
    second = layout_of(demo_summary("daily"), basemap)
    assert first == second


def test_a_crowded_pair_keeps_both_numbers(basemap):
    """Two forests a kilometre apart: the second may lose its name, or move
    away on a leader line, but never its percentage and never its dot."""
    crowded = [
        Location(name="Alpha", lat=49.400, lon=18.000, slug="alpha", short="Alpha"),
        Location(name="Beta", lat=49.418, lon=18.004, slug="beta", short="Beta"),
    ]
    summary = demo_summary(
        "daily", locations=crowded, chances={"alpha": (45, 45), "beta": (50, 50)}
    )
    layout = layout_of(summary, basemap, locations=crowded)
    assert [marker.slug for marker in layout.markers] == ["alpha", "beta"]
    assert all("%" in marker.text for marker in layout.markers)
    boxes = boxes_of(layout)
    for index, first in enumerate(boxes):
        for second in boxes[index + 1 :]:
            assert not overlap(first, second)
    assert [row.values for row in layout.rows] == [(45,), (50,)]


def test_a_nudged_label_gets_a_leader_line_back_to_its_dot(basemap):
    """Twelve locations on one spot: whoever ends up far away is tied back."""
    crowd = [
        Location(
            name=f"L{n}", lat=49.40 + 0.002 * (n % 3), lon=18.00 + 0.002 * (n // 3),
            slug=f"l{n}", short=f"L{n}",
        )
        for n in range(12)
    ]
    summary = demo_summary(
        "daily", locations=crowd, chances={loc.slug: (40 + n, None) for n, loc in enumerate(crowd)}
    )
    layout = layout_of(summary, basemap, locations=crowd)
    nudged = [m for m in layout.markers if m.placement == "nudged"]
    assert nudged, [m.placement for m in layout.markers]
    assert any(m.leader is not None for m in nudged)
    for marker in layout.markers:
        if marker.leader:
            x0, y0, x1, y1 = marker.leader
            assert (x0, y0) == (marker.x, marker.y)
            assert marker.box[0] <= x1 <= marker.box[2]
            assert marker.box[1] <= y1 <= marker.box[3]


def test_a_hopeless_crowd_still_draws_every_location(basemap):
    """Twenty forests on one spot cannot all be labelled without touching.
    They must still all be drawn, inside the picture, with their numbers."""
    crowd = [
        Location(name=f"L{n}", lat=49.40, lon=18.00, slug=f"l{n}", short=f"L{n}")
        for n in range(20)
    ]
    summary = demo_summary(
        "daily", locations=crowd, chances={loc.slug: (40, None) for loc in crowd}
    )
    layout = layout_of(summary, basemap, locations=crowd)

    assert len(layout.markers) == 20 == len(layout.rows)
    assert all("40%" in marker.text for marker in layout.markers)
    for marker in layout.markers:
        x0, y0, x1, y1 = marker.box
        assert x0 >= 0 and x1 <= render_lib.WIDTH
        assert y0 >= render_lib.MAP_TOP and y1 <= render_lib.MAP_BOTTOM
    assert layout == layout_of(demo_summary(
        "daily", locations=crowd, chances={loc.slug: (40, None) for loc in crowd}
    ), basemap, locations=crowd)


def test_a_per_slug_offset_moves_that_label_and_no_other(basemap, monkeypatch):
    before = layout_of(demo_summary("daily"), basemap)
    monkeypatch.setitem(render_lib.LABEL_OFFSETS, "kohutka", (140, -160))
    after = layout_of(demo_summary("daily"), basemap)

    moved = next(m for m in after.markers if m.slug == "kohutka")
    was = next(m for m in before.markers if m.slug == "kohutka")
    assert moved.placement == "override"
    assert moved.box != was.box
    assert moved.box[0] > was.box[0] and moved.box[1] < was.box[1]
    assert moved.name_shown is True
    # the dots themselves never move
    assert [(m.x, m.y) for m in after.markers] == [(m.x, m.y) for m in before.markers]


def test_the_default_override_table_is_empty():
    assert render_lib.LABEL_OFFSETS == {}


def test_a_label_never_leaves_the_map_viewport(basemap):
    layout = layout_of(demo_summary("daily"), basemap)
    for marker in layout.markers:
        x0, y0, x1, y1 = marker.box
        assert x0 >= 0 and x1 <= render_lib.WIDTH
        assert y0 >= render_lib.MAP_TOP and y1 <= render_lib.MAP_BOTTOM


# ----------------------------------------------------------------------
# a location the basemap does not cover
# ----------------------------------------------------------------------
def test_a_location_outside_the_basemap_is_named_not_dropped(basemap):
    far = list(SHIPPED) + [
        Location(name="Praha", lat=50.0755, lon=14.4378, slug="praha", short="Praha")
    ]
    summary = demo_summary("daily", locations=far, chances=DEMO_CHANCES)
    layout = layout_of(summary, basemap, locations=far)

    assert "praha" not in {marker.slug for marker in layout.markers}
    assert "praha" in {row.slug for row in layout.rows}  # the list keeps it
    assert any("Praha" in line.text for line in layout.notes)
    assert layout.note_box is not None
    assert len(layout.warnings) == 1
    warning = layout.warnings[0]
    assert "praha" in warning and "outside" in warning
    assert "50.07550" in warning and "14.43780" in warning


def test_a_location_with_no_coordinates_is_named_not_dropped(basemap):
    summary = demo_summary("daily")
    known = [loc for loc in SHIPPED if loc.slug != "kohutka"]
    layout = layout_of(summary, basemap, locations=known)
    assert "kohutka" not in {marker.slug for marker in layout.markers}
    assert "kohutka" in {row.slug for row in layout.rows}
    assert any("Kohútka" in line.text for line in layout.notes)
    assert layout.warnings and "no coordinates" in layout.warnings[0]


# ----------------------------------------------------------------------
# the footer
# ----------------------------------------------------------------------
def test_the_distance_bar_is_derived_from_km_per_pixel(basemap):
    layout = layout_of(demo_summary("daily"), basemap)
    pixels = layout.distance_box[2] - layout.distance_box[0]
    assert pixels == pytest.approx(layout.distance_km / basemap.km_per_pixel(), abs=1)
    assert 90 <= pixels <= render_lib.DISTANCE_MAX_PX
    assert layout.distance_km in render_lib.DISTANCE_STEPS_KM
    assert any(f"{layout.distance_km} км" == line.text for line in layout.footer)


def test_a_coarser_basemap_gets_a_longer_bar():
    """Same code, four times the ground per pixel -> a rounder number."""
    wide = FakeBasemap(min_lon=14.0, max_lon=18.5, min_lat=47.5, max_lat=49.6)
    layout = render_lib.build_layout(
        demo_summary("daily"), locations=SHIPPED, basemap=wide
    )
    assert layout.km_per_pixel > 0.2
    assert layout.distance_km >= 20


def test_the_footer_credits_openstreetmap(basemap):
    layout = layout_of(demo_summary("daily"), basemap)
    footer = " | ".join(line.text for line in layout.footer)
    assert "© OpenStreetMap contributors" in footer
    assert "openstreetmap.org/copyright" in footer
    assert "0 %" in footer and "50 %" in footer and "100 %" in footer


def test_a_basemap_with_another_source_keeps_the_osm_credit():
    layout = render_lib.build_layout(
        demo_summary("daily"),
        locations=SHIPPED,
        basemap=FakeBasemap(attribution="Relief: SRTM"),
    )
    footer = " | ".join(line.text for line in layout.footer)
    assert "Relief: SRTM" in footer and "© OpenStreetMap contributors" in footer


# ----------------------------------------------------------------------
# the page itself
# ----------------------------------------------------------------------
def test_the_bands_tile_the_page():
    assert render_lib.HEADER_BOTTOM == render_lib.MAP_TOP == 140
    assert render_lib.MAP_BOTTOM == render_lib.LIST_TOP == 1140
    assert render_lib.LIST_BOTTOM == render_lib.FOOTER_TOP == 1530
    assert render_lib.FOOTER_BOTTOM == render_lib.HEIGHT == 1600
    assert render_lib.MAP_HEIGHT == 1000
    assert 48 <= render_lib.CHANCE_SIZE <= 56
    assert render_lib.NAME_SIZE >= 36


def test_the_image_is_the_page_drawn_on_the_basemap(basemap):
    image, layout = render_lib.render_image(
        demo_summary("daily"), locations=SHIPPED, basemap=basemap
    )
    assert image.size == (render_lib.WIDTH, render_lib.HEIGHT)
    assert image.getpixel((5, render_lib.MAP_TOP + 5)) == basemap.fill
    assert image.getpixel((5, 5)) == render_lib.PAGE_BG
    assert image.getpixel((5, render_lib.LIST_TOP + 200)) == render_lib.PANEL_BG
    marker = layout.markers[0]
    assert image.getpixel((marker.x, marker.y)) == marker.color


def test_an_empty_configuration_still_renders(basemap):
    summary = report_lib.summarize(
        {"locations": [], "notes": [], "failed_sources": []}, mode="daily", today=TODAY
    )
    image, layout = render_lib.render_image(summary, locations=[], basemap=basemap)
    assert layout.rows == () and layout.markers == ()
    assert image.size == (render_lib.WIDTH, render_lib.HEIGHT)


def test_an_unknown_mode_is_refused(basemap):
    with pytest.raises(ValueError):
        render_lib.build_layout({"mode": "hourly"}, locations=[], basemap=basemap)


# ----------------------------------------------------------------------
# file names and pruning
# ----------------------------------------------------------------------
def test_the_file_name_carries_mode_date_and_a_unique_id():
    first = render_lib.map_name("daily", TODAY)
    second = render_lib.map_name("daily", TODAY)
    assert first != second
    assert first.startswith("mushroom-daily-2026-09-13-") and first.endswith(".png")
    assert render_lib.FILE_PATTERN.match(first)
    assert render_lib.FILE_PATTERN.match(render_lib.map_name("weekend", TODAY))
    assert not render_lib.FILE_PATTERN.match("mushroom-daily-2026-09-13-latest.png")
    assert not render_lib.FILE_PATTERN.match("latest.png")


def test_pruning_removes_only_our_own_old_files(tmp_path):
    old = TODAY - timedelta(days=40)
    keep_age = TODAY - timedelta(days=31)
    names = {
        f"mushroom-daily-{old}-aaaaaaaa.png": False,
        f"mushroom-weekend-{old}-bbbbbbbb.png": False,
        f"mushroom-daily-{keep_age}-cccccccc.png": True,  # exactly 31 days
        f"mushroom-daily-{TODAY}-dddddddd.png": True,
        f"mushroom-daily-{old}-zzzz.png": True,  # not our token
        f"holiday-{old}.png": True,
        "notes.txt": True,
        "mushroom-daily.png": True,
    }
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "subdir").mkdir()

    removed = render_lib.prune_maps(tmp_path, today=TODAY)

    assert {path.name for path in removed} == {
        name for name, kept in names.items() if not kept
    }
    for name, kept in names.items():
        assert (tmp_path / name).exists() is kept
    assert (tmp_path / "subdir").is_dir()


def test_pruning_an_absent_directory_is_not_an_error(tmp_path):
    assert render_lib.prune_maps(tmp_path / "nope", today=TODAY) == []


def test_the_map_is_written_atomically_and_completely(tmp_path, basemap):
    written = render_lib.render_map(
        demo_summary("daily"), locations=SHIPPED, directory=tmp_path, basemap=basemap
    )
    assert written.path.is_absolute() and written.path.exists()
    assert written.path.parent == tmp_path.resolve()
    assert [p.name for p in tmp_path.iterdir()] == [written.path.name]  # no leftovers

    from PIL import Image

    with Image.open(written.path) as image:
        assert image.size == (render_lib.WIDTH, render_lib.HEIGHT)
        assert image.format == "PNG"


def test_two_renders_are_two_files(tmp_path, basemap):
    summary = demo_summary("daily")
    first = render_lib.render_map(
        summary, locations=SHIPPED, directory=tmp_path, basemap=basemap
    )
    second = render_lib.render_map(
        summary, locations=SHIPPED, directory=tmp_path, basemap=basemap
    )
    assert first.path != second.path
    assert first.path.exists() and second.path.exists()


def test_the_directory_is_created_on_demand(tmp_path, basemap):
    target = tmp_path / "maps" / "2026"
    written = render_lib.render_map(
        demo_summary("weekend"), locations=SHIPPED, directory=target, basemap=basemap
    )
    assert written.path.parent == target.resolve()
    assert written.warnings == ()
