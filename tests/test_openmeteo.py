"""``fetch_openmeteo`` against the fixtures recorded live on 2026-09-07."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from mushroom_alerts import api30
from mushroom_alerts import fetch_openmeteo as m
from mushroom_alerts.base import Location

from .conftest import BYSTRICE, FakeHttp, VALMEZ

TODAY = date(2026, 9, 7)


# ----------------------------------------------------------------------
# request shape
# ----------------------------------------------------------------------
def test_params_match_plan_1():
    params = m.params_for(VALMEZ)
    assert params == {
        "latitude": 49.4718,
        "longitude": 17.9711,
        "daily": "precipitation_sum,temperature_2m_mean,temperature_2m_min,temperature_2m_max",
        "past_days": 3,
        "forecast_days": 16,
        "timezone": "Europe/Prague",
        "models": "best_match",
    }


def test_one_request_per_location(http, locations):
    m.fetch(locations, http=http, today=TODAY)
    assert http.calls == [m.URL, m.URL]  # 2/day against a 10 000/day limit


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------
def test_fetch_live_fixture(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    assert result.ok and result.error is None
    assert result.source == "openmeteo"
    # 19 days x 4 metrics x 2 locations
    assert len(result.readings) == 19 * 4 * 2
    assert {r.metric for r in result.readings} == {
        "precip_mm",
        "t_mean",
        "t_min",
        "t_max",
    }

    days = sorted({r.date for r in result.for_location("valmez")})
    assert days[0] == TODAY - timedelta(days=3)
    assert days[-1] == TODAY + timedelta(days=15)
    assert len(days) == 19


def test_values_match_the_recorded_response(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    got = {
        (r.location, r.date, r.metric): r.value
        for r in result.readings
    }
    assert got[("valmez", date(2026, 9, 10), "precip_mm")] == pytest.approx(9.8)
    assert got[("valmez", date(2026, 9, 10), "t_mean")] == pytest.approx(14.7)
    assert got[("valmez", date(2026, 9, 10), "t_min")] == pytest.approx(12.7)
    assert got[("valmez", date(2026, 9, 10), "t_max")] == pytest.approx(17.3)
    assert got[("valasska-bystrice", date(2026, 9, 10), "precip_mm")] == pytest.approx(12.3)
    # the day before the run: model analysis, not a gauge
    assert got[("valmez", date(2026, 9, 5), "precip_mm")] == pytest.approx(2.8)


def test_forecast_and_provisional_split(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    valmez = result.for_location("valmez")

    past = [r for r in valmez if r.date <= TODAY]
    future = [r for r in valmez if r.date > TODAY]
    assert len(past) == 4 * 4  # 09-04..09-07
    assert len(future) == 15 * 4

    for r in past:
        assert not r.is_forecast
        assert r.issued == ""
        assert r.meta["provisional"] is True
        assert "issued" not in r.meta
    for r in future:
        assert r.is_forecast
        assert r.issued == TODAY.isoformat()
        assert r.meta["horizon_days"] == (r.date - TODAY).days
        assert "provisional" not in r.meta


def test_meta_carries_grid_and_elevation(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    valmez = result.for_location("valmez")[0]
    assert valmez.meta["model"] == "best_match"
    assert valmez.meta["grid"] == [49.48, 17.98]
    assert valmez.meta["elevation"] == 309.0
    assert valmez.meta["timezone"] == "Europe/Prague"
    bystrice = result.for_location("valasska-bystrice")[0]
    assert bystrice.meta["elevation"] == 459.0  # PLAN §2a: 458 m


def test_reading_keys_are_unique(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    keys = [r.key() for r in result.readings]
    assert len(set(keys)) == len(keys)


# ----------------------------------------------------------------------
# parse_daily edge cases
# ----------------------------------------------------------------------
def test_parse_daily_rejects_junk():
    for payload in ([], "nope", {}, {"daily": {}}, {"daily": {"time": []}}):
        with pytest.raises(ValueError):
            m.parse_daily(payload)


def test_parse_daily_reports_an_api_error():
    with pytest.raises(ValueError, match="invalid String value"):
        m.parse_daily({"error": True, "reason": "invalid String value nonsense"})


def test_parse_daily_needs_precipitation():
    with pytest.raises(ValueError, match="precipitation_sum"):
        m.parse_daily(
            {"daily": {"time": ["2026-09-07"], "temperature_2m_mean": [15.0]}}
        )


def test_parse_daily_rejects_a_bad_date():
    with pytest.raises(ValueError, match="daily.time"):
        m.parse_daily({"daily": {"time": ["yesterday"], "precipitation_sum": [0.0]}})


def test_parse_daily_tolerates_a_missing_temperature_column():
    days, columns = m.parse_daily(
        {"daily": {"time": ["2026-09-07"], "precipitation_sum": [1.0]}}
    )
    assert days == [date(2026, 9, 7)]
    assert set(columns) == {"precipitation_sum"}


def test_nulls_are_skipped_not_zeroed():
    class Stub:
        calls: list[str] = []

        def get_json(self, url, params=None):
            return {
                "latitude": 49.5,
                "longitude": 18.0,
                "elevation": 300.0,
                "timezone": "Europe/Prague",
                "daily": {
                    "time": ["2026-09-06", "2026-09-07"],
                    "precipitation_sum": [None, 1.5],
                    "temperature_2m_mean": [12.0, None],
                    "temperature_2m_min": [None, None],
                    "temperature_2m_max": [None, None],
                },
            }

    result = m.fetch([VALMEZ], http=Stub(), today=TODAY)
    assert result.ok
    got = {(r.date, r.metric): r.value for r in result.readings}
    assert got == {
        (date(2026, 9, 6), "t_mean"): 12.0,
        (date(2026, 9, 7), "precip_mm"): 1.5,
    }


# ----------------------------------------------------------------------
# soft failure -- base.py contract / PLAN §6
# ----------------------------------------------------------------------
def test_soft_failure_on_http_error(locations):
    result = m.fetch(locations, http=FakeHttp(fail=True), today=TODAY)
    assert result.ok is False
    assert result.readings == []
    assert "ConnectionError" in (result.error or "")


def test_one_bad_location_does_not_kill_the_others(http):
    nowhere = Location(name="Nowhere", lat=0.0, lon=0.0, slug="nowhere")
    result = m.fetch([VALMEZ, nowhere], http=http, today=TODAY)
    assert result.ok is True  # partial success, per base.py
    assert result.error and "nowhere" in result.error
    assert {r.location for r in result.readings} == {"valmez"}


def test_fetch_never_raises():
    class Exploding:
        def get_json(self, url, params=None):
            raise RuntimeError("boom")

    result = m.fetch([VALMEZ], http=Exploding(), today=TODAY)
    assert result.ok is False and "boom" in (result.error or "")


def test_fetch_with_no_locations():
    result = m.fetch([], http=FakeHttp(), today=TODAY)
    assert result.ok is False


# ----------------------------------------------------------------------
# helpers used by rules.py
# ----------------------------------------------------------------------
def test_series_helper(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    precip = m.series(result.readings, "valmez")
    assert len(precip) == 19
    assert precip[date(2026, 9, 10)] == pytest.approx(9.8)
    assert precip[date(2026, 9, 19)] == pytest.approx(9.0)
    assert m.series(result.readings, "valmez", "t_mean")[TODAY] == pytest.approx(15.4)
    assert m.series(result.readings, "unknown-slug") == {}


def test_temperatures_helper(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    temps = m.temperatures(result.readings, "valmez")
    assert temps[date(2026, 9, 11)] == {"t_mean": 12.7, "t_min": 11.5, "t_max": 14.0}
    assert api30.temp_ok(**{k: v for k, v in temps[date(2026, 9, 11)].items() if k != "t_max"})


def test_issued_days_helper(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    assert m.issued_days(result.readings) == {
        TODAY + timedelta(days=i) for i in range(1, 16)
    }


# ----------------------------------------------------------------------
# the whole point: an API30 curve into the future
# ----------------------------------------------------------------------
def test_curve_splices_onto_a_station_series(http, locations):
    """A wet past plus the recorded forecast crosses 40 mm on a known day."""
    result = m.fetch(locations, http=http, today=TODAY)
    forecast = m.series(result.readings, "valmez")
    # 30 dry station days behind us, so only the forecast drives the curve.
    observed = {TODAY - timedelta(days=j): 0.0 for j in range(1, 41)}
    curve = api30.forecast_api30(observed, forecast, today=TODAY, horizon=16)
    assert len(curve) == 17
    assert curve[0][1] == pytest.approx(0.0)
    # 09-11 sees yesterday's 9.8 mm damped once and 09-09's 1.4 mm twice
    assert dict(curve)[date(2026, 9, 11)] == pytest.approx(
        9.8 * 0.93 + 1.4 * 0.93**2
    )
    assert api30.crossing(curve, 40.0, today=TODAY) is None  # never gets there
    assert api30.crossing(curve, 5.0, today=TODAY) == date(2026, 9, 11)
