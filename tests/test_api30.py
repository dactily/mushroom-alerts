"""``api30`` arithmetic, plus a regression test of the formula itself.

The regression test replays ``tests/fixtures/chmi_station_valmez_sra_api30.json``
-- the SRA and API30 elements of station Valašské Meziříčí for
2025-09-01 .. 2026-09-06, extracted from ČHMÚ open data on 2026-09-07 --
and checks that :func:`api30.api30` reproduces what ČHMÚ published.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from mushroom_alerts import api30 as m

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 7)


# ----------------------------------------------------------------------
# module shape
# ----------------------------------------------------------------------
def test_module_is_not_a_fetcher():
    """``discover_fetchers`` imports ``api30``; it must not look like one."""
    assert not hasattr(m, "SOURCE")
    assert not hasattr(m, "fetch")


# ----------------------------------------------------------------------
# arithmetic
# ----------------------------------------------------------------------
def _single_rain(day: date, mm: float = 10.0, span: int = 60) -> dict[date, float]:
    """A dry series with exactly one rainy day."""
    start = day - timedelta(days=span)
    series = {start + timedelta(days=i): 0.0 for i in range(2 * span + 1)}
    series[day] = mm
    return series


def test_single_rain_decays_by_the_fitted_constant():
    rain = date(2026, 8, 1)
    series = _single_rain(rain)
    # Day of the rain itself: the window is 1..30 days *before* t, so the
    # rain has not entered it yet.
    assert m.api30(series, rain) == pytest.approx(0.0)
    # Then 0.93, 0.93**2, ... -- lag j weighs DECAY**j (LAG_OFFSET = 0).
    assert m.api30(series, rain + timedelta(days=1)) == pytest.approx(9.3)
    assert m.api30(series, rain + timedelta(days=2)) == pytest.approx(8.649)
    assert m.api30(series, rain + timedelta(days=3)) == pytest.approx(8.04357)
    # 30 days after the rain it is still (just) inside the window...
    assert m.api30(series, rain + timedelta(days=30)) == pytest.approx(10 * 0.93**30)
    # ...and on day 31 it has dropped out entirely.
    assert m.api30(series, rain + timedelta(days=31)) == pytest.approx(0.0)


def test_plan_variant_decays_as_the_plan_says():
    """PLAN §5's ``0.92**(j-1)``: 10, 9.2, 8.464 -- kept configurable."""
    rain = date(2026, 8, 1)
    series = _single_rain(rain)
    got = [
        m.api30(
            series,
            rain + timedelta(days=n),
            decay=m.PLAN_DECAY,
            lag_offset=m.PLAN_LAG_OFFSET,
        )
        for n in (1, 2, 3)
    ]
    assert got == pytest.approx([10.0, 9.2, 8.464])


def test_weights_helper():
    w = m.weights()
    assert len(w) == m.WINDOW
    assert w[0] == pytest.approx(0.93)
    assert w[-1] == pytest.approx(0.93**30)


def test_sums_several_rains():
    day = date(2026, 8, 31)
    series = {day - timedelta(days=j): 0.0 for j in range(1, 31)}
    series[day - timedelta(days=1)] = 5.0
    series[day - timedelta(days=10)] = 20.0
    expected = 5.0 * 0.93 + 20.0 * 0.93**10
    assert m.api30(series, day) == pytest.approx(expected)


# ----------------------------------------------------------------------
# gaps
# ----------------------------------------------------------------------
def _full_window(day: date, value: float = 1.0) -> dict[date, float]:
    return {day - timedelta(days=j): value for j in range(1, m.WINDOW + 1)}


def test_a_few_gaps_are_treated_as_zero_and_counted():
    day = date(2026, 8, 31)
    series = _full_window(day)
    for j in (3, 7, 11):
        del series[day - timedelta(days=j)]
    detail = m.api30_detail(series, day)
    assert detail is not None
    assert detail.gaps == 3
    assert detail.missing == tuple(
        sorted(day - timedelta(days=j) for j in (3, 7, 11))
    )
    expected = sum(0.93**j for j in range(1, 31) if j not in (3, 7, 11))
    assert detail.value == pytest.approx(expected)


def test_too_many_gaps_gives_none():
    day = date(2026, 8, 31)
    series = _full_window(day)
    for j in (3, 7, 11, 13):
        del series[day - timedelta(days=j)]
    assert m.api30(series, day) is None
    assert m.api30_detail(series, day) is None
    # ...unless the caller says it is fine
    assert m.api30(series, day, max_gaps=4) is not None


def test_none_values_count_as_gaps():
    day = date(2026, 8, 31)
    series: dict[date, float | None] = dict(_full_window(day))
    for j in (2, 4, 6, 8):
        series[day - timedelta(days=j)] = None
    assert m.api30(series, day) is None


def test_empty_series_is_none():
    assert m.api30({}, date(2026, 8, 31)) is None


def test_forecast_days_are_counted():
    day = date(2026, 8, 31)
    series = _full_window(day)
    marked = [day - timedelta(days=j) for j in (1, 2, 3)]
    detail = m.api30_detail(series, day, forecast_days=marked)
    assert detail is not None and detail.forecast_days == 3


# ----------------------------------------------------------------------
# extend_series
# ----------------------------------------------------------------------
def test_extend_series_prefers_the_station():
    d1, d2, d3 = date(2026, 9, 4), date(2026, 9, 5), date(2026, 9, 6)
    observed = {d1: 2.3, d2: 0.2}
    forecast = {d1: 0.0, d2: 2.8, d3: 9.8}
    merged = m.extend_series(observed, forecast)
    assert merged == {d1: 2.3, d2: 0.2, d3: 9.8}


def test_extend_series_lets_a_forecast_fill_a_station_hole():
    d1, d2 = date(2026, 9, 5), date(2026, 9, 6)
    merged = m.extend_series({d1: None, d2: 1.0}, {d1: 4.0, d2: 9.0})
    assert merged == {d1: 4.0, d2: 1.0}


def test_extend_series_ignores_junk():
    d = date(2026, 9, 5)
    assert m.extend_series({d: "wet"}, {d: 3.0}) == {d: 3.0}


# ----------------------------------------------------------------------
# forecast_api30 / crossing
# ----------------------------------------------------------------------
def test_forecast_api30_spans_today_to_horizon():
    observed = {TODAY - timedelta(days=j): 1.0 for j in range(1, 61)}
    forecast = {TODAY + timedelta(days=j): 0.0 for j in range(0, 17)}
    curve = m.forecast_api30(observed, forecast, today=TODAY, horizon=16)
    assert len(curve) == 17
    assert curve[0][0] == TODAY
    assert curve[-1][0] == TODAY + timedelta(days=16)
    assert curve == sorted(curve)
    # a uniform 1 mm/day past decays away once the forecast is dry
    assert curve[0][1] > curve[-1][1]


def test_forecast_api30_skips_days_it_cannot_compute():
    curve = m.forecast_api30({}, {}, today=TODAY, horizon=5)
    assert curve == []


def test_crossing_finds_the_first_upward_crossing():
    curve = [(TODAY + timedelta(days=i), v) for i, v in enumerate([10, 20, 39, 45, 50])]
    assert m.crossing(curve, 40.0, today=TODAY) == TODAY + timedelta(days=3)


def test_crossing_on_day_one():
    curve = [(TODAY + timedelta(days=i), v) for i, v in enumerate([39.9, 40.0, 12.0])]
    assert m.crossing(curve, 40.0, today=TODAY) == TODAY + timedelta(days=1)


def test_crossing_none_when_already_above_today():
    curve = [(TODAY + timedelta(days=i), v) for i, v in enumerate([41, 45, 50])]
    assert m.crossing(curve, 40.0, today=TODAY) is None


def test_crossing_none_when_it_never_gets_there():
    curve = [(TODAY + timedelta(days=i), v) for i, v in enumerate([10, 20, 30, 39.9])]
    assert m.crossing(curve, 40.0, today=TODAY) is None


def test_crossing_none_without_today():
    curve = [(TODAY + timedelta(days=i), 50.0) for i in range(1, 4)]
    assert m.crossing(curve, 40.0, today=TODAY) is None


def test_crossing_ignores_a_dip_after_the_first_crossing():
    curve = [
        (TODAY + timedelta(days=i), v) for i, v in enumerate([10, 45, 20, 60])
    ]
    assert m.crossing(curve, 40.0, today=TODAY) == TODAY + timedelta(days=1)


def test_crossing_uses_the_default_threshold():
    curve = [(TODAY + timedelta(days=i), v) for i, v in enumerate([10, 26])]
    assert m.crossing(curve, today=TODAY) == TODAY + timedelta(days=1)
    # PLAN's 40 mm was refuted by the station (20.3 mm on a 3/5 day); 25 mm
    # is provisional and overridable from the environment.
    assert m.DEFAULT_THRESHOLD_MM == 25.0


def test_threshold_from_the_environment(monkeypatch):
    monkeypatch.delenv(m.THRESHOLD_ENV, raising=False)
    assert m.threshold_mm() == m.DEFAULT_THRESHOLD_MM
    assert m.threshold_mm(default=12.5) == 12.5
    monkeypatch.setenv(m.THRESHOLD_ENV, "31.5")
    assert m.threshold_mm() == 31.5
    assert m.threshold_mm(default=12.5) == 31.5
    for junk in ("", "  ", "wet", "0", "-3"):
        monkeypatch.setenv(m.THRESHOLD_ENV, junk)
        assert m.threshold_mm() == m.DEFAULT_THRESHOLD_MM


# ----------------------------------------------------------------------
# to_readings
# ----------------------------------------------------------------------
def test_to_readings_marks_only_future_days_as_forecast():
    curve = [(TODAY + timedelta(days=i), 10.0 + i) for i in range(0, 4)]
    readings = m.to_readings(curve, "valmez", today=TODAY)
    assert [r.source for r in readings] == [m.FORECAST_SOURCE] * 4
    assert {r.metric for r in readings} == {"api30_mm"}
    assert {r.location for r in readings} == {"valmez"}

    today_reading = readings[0]
    assert today_reading.date == TODAY
    assert today_reading.issued == ""
    assert not today_reading.is_forecast
    assert today_reading.meta["provisional"] is True

    for i, r in enumerate(readings[1:], start=1):
        assert r.is_forecast
        assert r.issued == TODAY.isoformat()
        assert r.meta["horizon_days"] == i
        assert "provisional" not in r.meta


def test_to_readings_keys_are_unique():
    curve = [(TODAY + timedelta(days=i), float(i)) for i in range(0, 5)]
    readings = m.to_readings(curve, "valmez", today=TODAY)
    assert len({r.key() for r in readings}) == len(readings)


def test_to_readings_meta_is_json_serialisable():
    readings = m.to_readings([(TODAY, 12.0)], "valmez", today=TODAY)
    assert json.loads(readings[0].meta_json())["window"] == m.WINDOW


# ----------------------------------------------------------------------
# temperature gate (PLAN §3 trigger 4)
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "t_mean, t_min, expected",
    [
        (15.0, 10.0, True),
        (8.0, 3.0, True),  # inclusive lower bound on the mean
        (22.0, 3.0, True),  # inclusive upper bound on the mean
        (7.9, 10.0, False),
        (22.1, 10.0, False),
        (15.0, 2.0, False),  # strictly above 2 °C
        (15.0, 2.1, True),
        (15.0, -1.0, False),
        (None, 10.0, False),
        (15.0, None, False),
        ("mild", 10.0, False),
    ],
)
def test_temp_ok(t_mean, t_min, expected):
    assert m.temp_ok(t_mean, t_min) is expected


# ----------------------------------------------------------------------
# the formula itself, against what ČHMÚ published
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def station() -> tuple[dict[date, float], dict[date, float]]:
    raw = json.loads(
        (FIXTURES / "chmi_station_valmez_sra_api30.json").read_text(encoding="utf-8")
    )
    sra = {date.fromisoformat(k): v for k, v in raw["sra"].items()}
    api = {date.fromisoformat(k): v for k, v in raw["api30"].items()}
    return sra, api


def _errors(sra, api, *, since: date | None = None, **kwargs) -> list[float]:
    """Absolute error per day.  ``since`` pins the day set so that variants
    with different window lengths are compared on identical days."""
    out = []
    for day, published in sorted(api.items()):
        if since is not None and day < since:
            continue
        mine = m.api30(sra, day, max_gaps=0, **kwargs)
        if mine is not None:
            out.append(abs(published - mine))
    return out


def test_reproduces_the_published_api30(station):
    """PLAN §5 claims 0.87 mm mean error; the real formula is exact."""
    sra, api = station
    errors = _errors(*station)
    assert len(errors) > 300
    mean = sum(errors) / len(errors)
    assert mean < 0.05, f"mean abs error {mean:.4f} mm"
    # ČHMÚ publishes one decimal, so 0.05 mm *is* the rounding floor.
    assert max(errors) <= 0.06, f"max abs error {max(errors):.4f} mm"


def test_the_plan_variant_is_measurably_worse(station):
    sra, api = station
    plan = _errors(sra, api, decay=m.PLAN_DECAY, lag_offset=m.PLAN_LAG_OFFSET)
    ours = _errors(sra, api)
    assert sum(plan) / len(plan) > 0.5
    assert sum(plan) / len(plan) > 10 * (sum(ours) / len(ours))


def test_the_fitted_constants_are_the_optimum(station):
    """Scanning either knob away from (0.93, 30) makes the fit worse."""
    sra, api = station
    since = min(sra) + timedelta(days=32)  # every variant computable here

    def mean_error(**kwargs) -> float:
        errors = _errors(sra, api, since=since, **kwargs)
        return sum(errors) / len(errors)

    best = mean_error()
    for decay in (0.925, 0.928, 0.932, 0.935):
        assert mean_error(decay=decay) > best
    for window in (28, 29, 31, 32):
        assert mean_error(window=window) > best
    # day t itself must stay out of the window
    assert mean_error(lag_offset=1) > best
