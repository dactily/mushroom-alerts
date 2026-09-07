from __future__ import annotations

from datetime import date

import pytest

from mushroom_alerts import fetch_houbymapa as m

from .conftest import BYSTRICE, FakeHttp, VALMEZ

TODAY = date(2026, 9, 7)


def test_nearest_cell_matches_plan(houbymapa_payload):
    cells = m.cells_from_payload(houbymapa_payload)
    cell, dist = m.nearest_cell(cells, VALMEZ.lat, VALMEZ.lon)
    assert (cell["lat"], cell["lng"]) == (49.55, 17.89)  # PLAN §2a
    assert dist == pytest.approx(10.5, abs=0.5)

    cell, dist = m.nearest_cell(cells, BYSTRICE.lat, BYSTRICE.lon)
    assert (cell["lat"], cell["lng"]) == (49.35, 18.09)  # PLAN §2a
    assert dist == pytest.approx(7.4, abs=0.5)


def test_nearest_cell_picks_the_closest_of_a_synthetic_grid():
    cells = [
        {"lat": 49.35, "lng": 17.89, "l": 4, "s": 0.63},
        {"lat": 49.55, "lng": 17.89, "l": 3, "s": 0.47},
        {"lat": 49.55, "lng": 18.09, "l": 3, "s": 0.5},
    ]
    cell, dist = m.nearest_cell(cells, 49.54, 17.90)
    assert (cell["lat"], cell["lng"]) == (49.55, 17.89)
    assert dist < 2


def test_nearest_cell_empty():
    with pytest.raises(ValueError):
        m.nearest_cell([], 49.0, 18.0)


def test_updated_date_from_unix_timestamp(houbymapa_payload):
    # 1788726639 -> 2026-09-06 22:30 Europe/Prague, per PLAN §1
    assert m.updated_date(houbymapa_payload, TODAY) == date(2026, 9, 6)


def test_updated_date_falls_back_to_today():
    assert m.updated_date({}, TODAY) == TODAY
    assert m.updated_date({"updated": "rubbish"}, TODAY) == TODAY
    assert m.updated_date({"updated": "2026-09-01T22:30:00Z"}, TODAY) == date(2026, 9, 1)


def test_fetch_live_fixture(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    assert result.ok and result.error is None
    got = {(r.location, r.metric): r for r in result.readings}
    # PLAN §2a snapshot: Valmez cell 3/5 s=0.47, Bystřice cell 4/5 s=0.63
    assert got[("valmez", "level")].value == 3.0
    assert got[("valmez", "score")].value == pytest.approx(0.47)
    assert got[("valasska-bystrice", "level")].value == 4.0
    assert got[("valasska-bystrice", "score")].value == pytest.approx(0.63)

    r = got[("valasska-bystrice", "level")]
    assert r.source == "houbymapa"
    assert r.date == date(2026, 9, 6)
    assert r.meta["cell"] == [49.35, 18.09]
    assert r.meta["model"] == "v3.0"
    assert "historicky úrodné" in (r.text or "")
    assert "stale" not in r.meta


def test_fetch_marks_stale(http, locations):
    result = m.fetch(locations, http=http, today=date(2026, 9, 30))
    assert all(r.meta["stale"] for r in result.readings)


def test_fetch_never_raises(locations):
    result = m.fetch(locations, http=FakeHttp(fail=True), today=TODAY)
    assert result.ok is False and result.readings == []
    assert "ConnectionError" in (result.error or "")


def test_fetch_rejects_payload_without_cells(locations):
    class Empty:
        def get_json(self, url, params=None):
            return {"updated": 1788726639, "cells": []}

    result = m.fetch(locations, http=Empty(), today=TODAY)
    assert result.ok is False and "cells" in (result.error or "")


def test_fetch_skips_locations_far_from_the_grid():
    from mushroom_alerts.base import Location

    class Grid:
        def get_json(self, url, params=None):
            return {"updated": 1788726639, "cells": [{"lat": 49.55, "lng": 17.89, "l": 3, "s": 0.47}]}

    far = Location(name="Lisboa", lat=38.7, lon=-9.1, slug="lisboa")
    result = m.fetch([far], http=Grid(), today=TODAY)
    assert result.ok is False and "km away" in (result.error or "")


def test_params_for(houbymapa_payload):
    cells = m.cells_from_payload(houbymapa_payload)
    params = m.params_for(BYSTRICE, cells)
    assert params["houbymapa_cell"] == [49.35, 18.09]
    assert params["houbymapa_distance_km"] < 10
