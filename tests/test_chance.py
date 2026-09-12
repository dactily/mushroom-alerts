"""``chance``: the comparable per-location percentage (PLAN §9b).

The number is not calibrated, so what can be pinned down is the arithmetic:
every multiplier in isolation, the rounding, the bounds, the station cap,
and the rule that the maps count for today only.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from mushroom_alerts import chance as chance_lib
from mushroom_alerts import policy
from mushroom_alerts.base import DataQuality

TODAY = date(2026, 9, 12)

#: API30 in the 15-25 mm band (x0.8) with a satisfied temperature gate, no
#: frost and no maps: a base case every test varies one factor of.
NEUTRAL = dict(
    phase="primary_window",
    api30_mm=20.0,
    api30_quality=DataQuality.FRESH,
    t_mean=15.0,
    t_min=9.0,
    frost_present=False,
    chmi_level=None,
    houbymapa_score=None,
    station_available=True,
)


def chance(**kw) -> int:
    return chance_lib.chance_for_day(**{**NEUTRAL, **kw})


def raw(**kw) -> float:
    return chance_lib.assess_day_chance(**{**NEUTRAL, **kw}).raw_percent


# ----------------------------------------------------------------------
# the factors, one at a time
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "phase, base",
    [
        ("primary_window", 0.60),
        ("residual_window", 0.35),
        ("waiting", 0.15),
        ("expired", 0.05),
        ("no_episode", 0.05),
        ("nonsense", 0.05),  # an unknown phase must not be optimistic
    ],
)
def test_the_phase_sets_the_base(phase, base):
    assert raw(phase=phase) == pytest.approx(base * 0.8 * 100)


@pytest.mark.parametrize(
    "api30, factor",
    [
        (0.0, 0.5),
        (14.9, 0.5),
        (15.0, 0.8),  # the band edge belongs to the wetter band
        (24.9, 0.8),
        (25.0, 1.15),
        (39.9, 1.15),
        (40.0, 1.25),
        (120.0, 1.25),
    ],
)
def test_moisture_follows_the_api30_bands(api30, factor):
    assert raw(api30_mm=api30) == pytest.approx(0.60 * factor * 100)


def test_an_unusable_api30_is_neutral_not_dry():
    """No number is no evidence; the station cap does the honest part."""
    assert raw(api30_mm=None) == pytest.approx(60.0)
    assert raw(api30_quality=DataQuality.STALE) == pytest.approx(60.0)
    assert raw(api30_quality="missing") == pytest.approx(60.0)
    # a partial day still carries its measurement
    assert raw(api30_quality=DataQuality.PARTIAL) == pytest.approx(48.0)


@pytest.mark.parametrize(
    "t_mean, t_min",
    [(15.0, 9.0), (8.0, 2.1), (22.0, 3.0)],
)
def test_a_satisfied_temperature_gate_costs_nothing(t_mean, t_min):
    assert raw(t_mean=t_mean, t_min=t_min) == pytest.approx(48.0)


@pytest.mark.parametrize(
    "t_mean, t_min",
    [(7.9, 9.0), (22.1, 9.0), (15.0, 2.0), (None, 9.0), (15.0, None)],
)
def test_a_failed_or_unknown_temperature_gate_costs_a_multiplier(t_mean, t_min):
    assert raw(t_mean=t_mean, t_min=t_min) == pytest.approx(48.0 * 0.6)


def test_frost_costs_a_multiplier():
    assert raw(frost_present=True) == pytest.approx(48.0 * 0.4)


@pytest.mark.parametrize(
    "score, factor", [(0.0, 0.85), (0.5, 1.0), (0.63, 1.039), (1.0, 1.15)]
)
def test_houbymapa_scales_by_its_score(score, factor):
    assert raw(houbymapa_score=score) == pytest.approx(48.0 * factor)


@pytest.mark.parametrize(
    "level, factor", [(1.0, 0.8), (3.0, 0.9), (4.0, 0.95), (5.0, 1.0)]
)
def test_the_chmi_level_scales_around_its_pivot(level, factor):
    assert raw(chmi_level=level) == pytest.approx(48.0 * factor)


def test_the_maps_multiply_together():
    assert raw(chmi_level=5.0, houbymapa_score=1.0) == pytest.approx(48.0 * 1.15)


# ----------------------------------------------------------------------
# rounding, bounds, the cap
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "api30, expected",
    [(20.0, 50), (0.0, 30), (30.0, 70), (45.0, 75)],
)
def test_the_result_is_rounded_to_five(api30, expected):
    """No false precision: 48 % is reported as 50 %, 47.4 % as 45 %."""
    assert chance(api30_mm=api30) == expected


def test_rounding_is_half_up_not_bankers():
    # 0.35 * 0.5 = 17.5 -> 20, and 0.15 * 0.5 = 7.5 -> 10
    assert chance(phase="residual_window", api30_mm=0.0) == 20
    assert chance(phase="waiting", api30_mm=0.0) == 10


def test_the_result_is_clamped_to_the_reported_range():
    floor = chance(phase="no_episode", api30_mm=0.0, frost_present=True)
    assert floor == policy.CHANCE_MIN
    ceiling = chance(
        api30_mm=100.0, chmi_level=5.0, houbymapa_score=1.0, t_mean=15.0
    )
    assert ceiling <= policy.CHANCE_MAX
    # 0.60 * 1.25 * 1.15 = 86 % -- the ceiling only bites above 95 %
    assert ceiling == 85


def test_a_dead_station_caps_the_number():
    wet = dict(api30_mm=45.0, chmi_level=5.0, houbymapa_score=1.0)
    assert chance(**wet) == 85  # 0.60 x 1.25 x 1.15 x 1.0 = 86.25 %

    capped = chance_lib.assess_day_chance(
        **{**NEUTRAL, **wet, "station_available": False}
    )
    assert capped.value == policy.CHANCE_NO_STATION_CAP
    assert capped.capped is True
    assert capped.raw_percent > policy.CHANCE_NO_STATION_CAP


def test_the_cap_does_not_lift_a_low_number():
    low = chance_lib.assess_day_chance(
        **{**NEUTRAL, "phase": "waiting", "api30_mm": 0.0, "station_available": False}
    )
    assert low.value == 10 and low.capped is False


# ----------------------------------------------------------------------
# the horizon
# ----------------------------------------------------------------------
def horizon(**kw):
    days = [TODAY + timedelta(days=n) for n in range(0, 4)]
    return chance_lib.assess_chance_horizon(
        TODAY,
        {day: "primary_window" for day in days},
        days,
        api30={day: 20.0 for day in days},
        api30_quality={day: DataQuality.FRESH for day in days},
        t_mean={day: 15.0 for day in days},
        t_min={day: 9.0 for day in days},
        frost_present=False,
        chmi_level=kw.get("chmi_level"),
        houbymapa_score=kw.get("houbymapa_score"),
        station_available=kw.get("station_available", True),
    )


def test_the_maps_count_for_today_only():
    """ČHMÚ and HoubyMapa publish no forecast (the verdict agrees)."""
    outlook = horizon(chmi_level=5.0, houbymapa_score=1.0)
    assert outlook.current.value == 55  # 48 % x 1.15
    for item in outlook.outlook[1:]:
        assert item.value == 50
        assert [name for name, _ in item.factors] == ["phase", "moisture"]


def test_the_peak_is_the_best_day_earliest_first():
    outlook = chance_lib.assess_chance_horizon(
        TODAY,
        {
            TODAY: "waiting",
            TODAY + timedelta(days=1): "primary_window",
            TODAY + timedelta(days=2): "primary_window",
        },
        [TODAY + timedelta(days=n) for n in range(0, 3)],
        api30={TODAY + timedelta(days=n): 20.0 for n in range(0, 3)},
        api30_quality={
            TODAY + timedelta(days=n): DataQuality.FRESH for n in range(0, 3)
        },
        t_mean={TODAY + timedelta(days=n): 15.0 for n in range(0, 3)},
        t_min={TODAY + timedelta(days=n): 9.0 for n in range(0, 3)},
        frost_present=False,
        chmi_level=None,
        houbymapa_score=None,
        station_available=True,
    )
    payload = outlook.as_dict()
    assert payload["today"] == 10  # waiting: 0.15 x 0.8 = 12 % -> 10 %
    assert payload["peak"] == (TODAY + timedelta(days=1), 50)
    assert payload["curve"][TODAY + timedelta(days=2)] == 50
    assert payload["capped"] is False


def test_a_dead_station_caps_the_whole_curve():
    payload = horizon(station_available=False, chmi_level=5.0, houbymapa_score=1.0)
    assert payload.as_dict()["capped"] is True
    assert set(payload.as_dict()["curve"].values()) == {policy.CHANCE_NO_STATION_CAP}


def test_the_horizon_always_evaluates_today_even_without_a_curve():
    outlook = chance_lib.assess_chance_horizon(
        TODAY,
        {},
        [],
        api30={},
        api30_quality={},
        t_mean={},
        t_min={},
        frost_present=False,
        chmi_level=None,
        houbymapa_score=None,
        station_available=False,
    )
    # no episode, no moisture, no temperature: 5 % x 1.0 x 0.6 = 3 % -> 5 %
    assert outlook.current.value == policy.CHANCE_MIN
    assert outlook.as_dict()["curve"] == {TODAY: policy.CHANCE_MIN}
