from __future__ import annotations

import json
from datetime import date

import pytest

from mushroom_alerts import base
from mushroom_alerts.base import (
    FetchResult,
    Location,
    Reading,
    haversine_km,
    soft_fetch,
)

TODAY = date(2026, 9, 7)


def test_sources_are_the_documented_five():
    assert set(base.SOURCES) == {
        "chmi_map",
        "houbymapa",
        "chmi_station",
        "openmeteo",
        "api30_forecast",
    }


def test_exit_codes():
    assert (base.EXIT_SILENT, base.EXIT_ERROR, base.EXIT_SIGNAL) == (0, 1, 10)


def test_reading_key_and_forecast_flag():
    obs = Reading("chmi_map", "valmez", TODAY, "level", 3.0)
    assert obs.issued == "" and obs.is_forecast is False
    assert obs.key() == ("chmi_map", "valmez", "2026-09-07", "level", "")

    fc = Reading(
        "api30_forecast", "valmez", date(2026, 9, 11), "api30_mm", 41.0,
        meta={"issued": "2026-09-07"},
    )
    assert fc.is_forecast and fc.issued == "2026-09-07"
    assert fc.key()[-1] == "2026-09-07"


def test_meta_json_is_deterministic():
    a = Reading("x", "y", TODAY, "m", 1.0, meta={"b": 2, "a": 1})
    b = Reading("x", "y", TODAY, "m", 1.0, meta={"a": 1, "b": 2})
    assert a.meta_json() == b.meta_json() == '{"a": 1, "b": 2}'
    assert Reading("x", "y", TODAY, "m", 1.0).meta_json() is None


def test_meta_json_survives_non_serialisable_values():
    r = Reading("x", "y", TODAY, "m", 1.0, meta={"when": TODAY})
    assert json.loads(r.meta_json()) == {"when": "2026-09-07"}


def test_fetch_result_helpers():
    r1 = Reading("chmi_map", "valmez", TODAY, "level", 3.0)
    r2 = Reading("chmi_map", "bystrice", TODAY, "level", 4.0)
    ok = FetchResult.success("chmi_map", [r1, r2])
    assert ok.ok and ok.error is None and ok.fetched_at.tzinfo is not None
    assert ok.for_location("valmez") == [r1]

    bad = FetchResult.failure("chmi_map", "502")
    assert bad.ok is False and bad.readings == [] and bad.error == "502"


def test_soft_fetch_swallows_exceptions():
    def boom(locations, *, http, today):
        raise RuntimeError("upstream on fire")

    result = soft_fetch("chmi_map", boom, [], http=None, today=TODAY)
    assert result.ok is False
    assert "RuntimeError: upstream on fire" in result.error


def test_soft_fetch_rejects_a_wrong_return_type():
    result = soft_fetch("chmi_map", lambda l, *, http, today: {"level": 3}, [], http=None, today=TODAY)
    assert result.ok is False and "not FetchResult" in result.error


def test_soft_fetch_passes_arguments_through():
    seen = {}

    def fetch(locations, *, http, today):
        seen.update(locations=locations, http=http, today=today)
        return FetchResult.success("chmi_map", [])

    loc = [Location("V", 49.0, 18.0, "v")]
    soft_fetch("chmi_map", fetch, loc, http="H", today=TODAY)
    assert seen == {"locations": loc, "http": "H", "today": TODAY}


def test_haversine_km():
    # Valašské Meziříčí -> Valašská Bystřice, ~11 km (PLAN §2a)
    assert haversine_km(49.4718, 17.9711, 49.416, 18.106) == pytest.approx(11.4, abs=0.5)
    assert haversine_km(49.0, 18.0, 49.0, 18.0) == 0.0
    # one degree of latitude is ~111.2 km anywhere
    assert haversine_km(49.0, 18.0, 50.0, 18.0) == pytest.approx(111.2, abs=0.5)


def test_location_as_dict():
    assert Location("V", 49.0, 18.0, "v").as_dict() == {
        "name": "V", "lat": 49.0, "lon": 18.0, "slug": "v"
    }


def test_utcnow_is_aware():
    assert base.utcnow().tzinfo is not None
