"""``chance``: the comparable per-location percentage (PLAN §9b).

The number is not calibrated, so what can be pinned down is the arithmetic:
every multiplier in isolation, the rounding, the bounds, the station cap --
and, since the model became continuous, the three properties the steps used
to break:

* a day is a point on a ramp, not a category, so the curve cannot jump from
  15 % to 75 % overnight;
* two days with the same conditions get the same number, which means the
  maps count on every day of the horizon, not only today;
* the wetter of two places always scores higher, with no band edge where
  8 mm of API30 count for nothing and 0.1 mm counts for a third.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from mushroom_alerts import chance as chance_lib
from mushroom_alerts import policy
from mushroom_alerts.base import DataQuality

TODAY = date(2026, 9, 12)

#: Nine days after the rain (the ramp's plateau, x0.60), API30 on the
#: 20 mm knot (x0.8), a satisfied temperature gate, no frost, no maps and
#: no lead time: 48 %, the base case every test varies one factor of.
NEUTRAL = dict(
    days_since_anchor=(9,),
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


def factors(**kw) -> dict[str, float]:
    return dict(chance_lib.assess_day_chance(**{**NEUTRAL, **kw}).factors)


def slope(knots) -> float:
    """The steepest segment of a ramp, per unit of x."""
    return max(
        abs(y1 - y0) / (x1 - x0) for (x0, y0), (x1, y1) in zip(knots, knots[1:])
    )


# ----------------------------------------------------------------------
# the shared interpolation helper
# ----------------------------------------------------------------------
KNOTS = ((0.0, 1.0), (2.0, 2.0), (6.0, 0.0))


@pytest.mark.parametrize(
    "x, expected",
    [
        (-10.0, 1.0),  # clamped to the first value, never extrapolated
        (0.0, 1.0),
        (1.0, 1.5),
        (2.0, 2.0),
        (4.0, 1.0),
        (6.0, 0.0),
        (99.0, 0.0),  # clamped to the last value
    ],
)
def test_the_ramp_interpolates_between_knots_and_clamps_outside(x, expected):
    assert chance_lib._ramp(KNOTS, x) == pytest.approx(expected)


@pytest.mark.parametrize(
    "knots",
    [
        policy.CHANCE_PHASE_RAMP,
        policy.CHANCE_MOISTURE_RAMP,
        policy.CHANCE_HORIZON_DAMPING,
    ],
)
def test_every_policy_ramp_is_ordered_and_hits_its_own_knots(knots):
    """One helper, three ramps: each must reproduce its table exactly."""
    xs = [x for x, _ in knots]
    assert xs == sorted(xs) and len(set(xs)) == len(xs)
    for x, y in knots:
        assert chance_lib._ramp(knots, float(x)) == pytest.approx(y)


# ----------------------------------------------------------------------
# the phase ramp: days since the rain, not a word
# ----------------------------------------------------------------------
@pytest.mark.parametrize("age, base", list(policy.CHANCE_PHASE_RAMP))
def test_the_phase_ramp_hits_its_knots(age, base):
    assert raw(days_since_anchor=(age,)) == pytest.approx(base * 0.8 * 100)


@pytest.mark.parametrize(
    "age, base",
    [
        (2.0, 0.09),  # 0.05 + 2/5 x 0.10
        (6.0, 0.375),  # halfway up the steep climb into the window
        (9.0, 0.60),  # the plateau
        (14.0, 0.475),  # halfway down to 0.35
        (18.5, 0.225),
        (23.0, 0.075),
    ],
)
def test_the_phase_ramp_is_linear_between_its_knots(age, base):
    assert raw(days_since_anchor=(age,)) == pytest.approx(base * 0.8 * 100)


@pytest.mark.parametrize("age", [-5.0, -1.0, 26.0, 400.0])
def test_the_phase_ramp_is_clamped_outside_its_range(age):
    """0.05 either way: before the rain and long after the window."""
    assert raw(days_since_anchor=(age,)) == pytest.approx(0.05 * 0.8 * 100)


def test_no_episode_scores_like_a_window_long_over():
    assert raw(days_since_anchor=()) == pytest.approx(0.05 * 0.8 * 100)


def test_several_episodes_take_the_best_ramp_value():
    """An open older window is not hidden by a fresh rain still waiting."""
    fresh_rain, open_window = 1.0, 9.0
    assert raw(days_since_anchor=(fresh_rain,)) == pytest.approx(0.07 * 0.8 * 100)
    assert raw(days_since_anchor=(fresh_rain, open_window)) == pytest.approx(48.0)
    # order is irrelevant, and an expired episode adds nothing
    assert raw(days_since_anchor=(open_window, fresh_rain)) == pytest.approx(48.0)
    assert raw(days_since_anchor=(9.0, 40.0)) == pytest.approx(48.0)


def test_the_phase_cliff_is_gone():
    """The defect this ramp exists for: 0.15 -> 0.60 overnight.

    With everything else fixed, one day of age may move the raw percentage
    by at most the steepest segment of the ramp -- 22.5 points of base, or
    ~26 % here.  The step function moved 45 points of base in one night.
    """
    bound = slope(policy.CHANCE_PHASE_RAMP) * 100 * 0.8
    curve = [raw(days_since_anchor=(float(age),)) for age in range(0, 31)]
    assert max(abs(b - a) for a, b in zip(curve, curve[1:])) <= bound + 1e-9
    assert bound < 20.0  # the old base step alone was 45 x 0.8 = 36 points


# ----------------------------------------------------------------------
# moisture: a ramp on API30, not four bands
# ----------------------------------------------------------------------
@pytest.mark.parametrize("api30, factor", list(policy.CHANCE_MOISTURE_RAMP))
def test_moisture_hits_the_knots_of_its_ramp(api30, factor):
    assert raw(api30_mm=api30) == pytest.approx(0.60 * factor * 100)


@pytest.mark.parametrize(
    "api30, factor",
    [
        (0.0, 0.5),  # clamped dry
        (15.0, 0.65),
        (25.0, 0.975),
        (28.0, 1.08),
        (35.9, 1.18933),
        (37.5, 1.2),
        (120.0, 1.25),  # clamped wet
    ],
)
def test_moisture_is_linear_between_the_knots(api30, factor):
    assert raw(api30_mm=api30) == pytest.approx(0.60 * factor * 100, rel=1e-4)


def test_the_wetter_place_always_scores_higher():
    """The bands tied 28.0 mm with 35.9 mm and then jumped at one edge."""
    values = [raw(api30_mm=mm) for mm in (10.0, 20.0, 28.0, 30.0, 35.9, 45.0)]
    assert values == sorted(values)
    assert raw(api30_mm=35.9) > raw(api30_mm=28.0)
    # and one millimetre is worth at most one millimetre of the ramp
    step = slope(policy.CHANCE_MOISTURE_RAMP) * 100 * 0.60
    assert abs(raw(api30_mm=30.0) - raw(api30_mm=29.0)) <= step + 1e-9


def test_the_human_readable_bands_are_still_published():
    """``API30_BANDS_MM`` left the arithmetic but not the cheat sheet."""
    assert policy.API30_BANDS_MM == (15.0, 25.0, 40.0)
    assert "15" in policy.interpretation_guide()


@pytest.mark.parametrize(
    "kw, reason",
    [
        (dict(api30_mm=None), chance_lib.NO_API30),
        (dict(api30_quality="missing"), chance_lib.NO_API30),
        (dict(api30_quality="nonsense"), chance_lib.NO_API30),
        (dict(api30_quality=DataQuality.STALE), chance_lib.STALE_API30),
    ],
)
def test_an_unusable_api30_leaves_the_day_without_a_number(kw, reason):
    """No usable measurement, no percentage -- and nothing computed at all.

    A missing API30 used to enter as a neutral x1.0, which is above the
    moisture ramp everywhere below 30 mm: the number went *up* when the
    measurement disappeared (45 % -> 55 % in an open window, exactly the
    ten points the send rule reacts to).
    """
    item = chance_lib.assess_day_chance(**{**NEUTRAL, **kw})

    assert item.value is None
    assert item.insufficient is True
    assert item.reason == reason
    assert item.raw_percent is None
    assert item.factors == ()
    assert item.capped is False


def test_a_partial_api30_still_carries_its_measurement():
    assert raw(api30_quality=DataQuality.PARTIAL) == pytest.approx(48.0)
    assert chance(api30_quality=DataQuality.PARTIAL) == 50


def test_a_missing_api30_is_never_worth_more_than_a_measured_one():
    """The property the neutral multiplier broke, stated directly."""
    measured = [chance(api30_mm=mm) for mm in (0.0, 10.0, 20.0, 30.0, 45.0)]
    assert all(value is not None for value in measured)
    assert chance(api30_mm=None) is None


# ----------------------------------------------------------------------
# temperature, frost, the maps
# ----------------------------------------------------------------------
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


def test_a_stale_map_contributes_nothing():
    """Freshness is the caller's job: a stale map arrives as ``None``."""
    assert factors(chmi_level=None, houbymapa_score=None).keys() == {
        "phase",
        "moisture",
        "horizon",
    }


# ----------------------------------------------------------------------
# the lead-time damping
# ----------------------------------------------------------------------
@pytest.mark.parametrize("lead, factor", list(policy.CHANCE_HORIZON_DAMPING))
def test_the_damping_hits_the_knots_of_its_ramp(lead, factor):
    assert raw(lead_days=lead) == pytest.approx(48.0 * factor)


@pytest.mark.parametrize(
    "lead, factor",
    [(-3.0, 1.0), (1.0, 1.0), (5.0, 0.95), (11.5, 0.825), (30.0, 0.75)],
)
def test_the_damping_is_linear_between_the_knots_and_clamped_outside(lead, factor):
    assert raw(lead_days=lead) == pytest.approx(48.0 * factor)


def test_the_damping_never_grows_with_lead_time():
    curve = [raw(lead_days=float(lead)) for lead in range(-2, 25)]
    assert all(later <= earlier + 1e-9 for earlier, later in zip(curve, curve[1:]))
    # flat for the days the forecast is worth something, then strictly down
    assert raw(lead_days=3) == pytest.approx(raw(lead_days=0))
    assert raw(lead_days=7) < raw(lead_days=3)
    assert raw(lead_days=16) < raw(lead_days=7)
    assert raw(lead_days=6) == pytest.approx(48.0 * 0.925)


def test_the_damping_is_listed_as_its_own_factor():
    assert factors(lead_days=7)["horizon"] == pytest.approx(0.9)


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
    assert chance(days_since_anchor=(16,), api30_mm=0.0) == 20
    assert chance(days_since_anchor=(5,), api30_mm=0.0) == 10


def test_the_result_is_clamped_to_the_reported_range():
    floor = chance(days_since_anchor=(), api30_mm=0.0, frost_present=True)
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
        **{
            **NEUTRAL,
            "days_since_anchor": (5,),
            "api30_mm": 0.0,
            "station_available": False,
        }
    )
    assert low.value == 10 and low.capped is False


def test_every_factor_is_named_in_the_breakdown():
    """The debug output must still explain the number it prints."""
    item = chance_lib.assess_day_chance(
        **{
            **NEUTRAL,
            "frost_present": True,
            "t_mean": 30.0,
            "chmi_level": 4.0,
            "houbymapa_score": 0.63,
            "lead_days": 7,
        }
    )
    assert [name for name, _ in item.factors] == [
        "phase",
        "moisture",
        "temperature",
        "frost",
        "houbymapa",
        "chmi_map",
        "horizon",
    ]
    product = 1.0
    for _, factor in item.factors:
        product *= factor
    assert item.raw_percent == pytest.approx(product * 100)


# ----------------------------------------------------------------------
# the horizon
# ----------------------------------------------------------------------
def horizon(days=4, *, anchor_offset=-9, **kw):
    days_list = [TODAY + timedelta(days=n) for n in range(0, days)]
    return chance_lib.assess_chance_horizon(
        TODAY,
        kw.get("anchors", [TODAY + timedelta(days=anchor_offset)]),
        days_list,
        api30=kw.get("api30", {day: 20.0 for day in days_list}),
        api30_quality={day: DataQuality.FRESH for day in days_list},
        t_mean={day: 15.0 for day in days_list},
        t_min={day: 9.0 for day in days_list},
        frost_present=False,
        chmi_level=kw.get("chmi_level"),
        houbymapa_score=kw.get("houbymapa_score"),
        station_available=kw.get("station_available", True),
    )


def test_the_maps_count_on_every_day_of_the_horizon():
    """A place correction, not a property of the day (see the module doc).

    Today's ČHMÚ 3/5 used to cost x0.9 while tomorrow paid nothing, so the
    same conditions scored higher tomorrow and ``максимум`` could be pure
    arithmetic.  Now identical days are identical numbers.
    """
    outlook = horizon(chmi_level=3.0, houbymapa_score=0.5)
    assert outlook.current.value == 45  # 48 % x 0.9 x 1.0 = 43.2 %
    for item in outlook.outlook:
        assert item.value == 45
        assert [name for name, _ in item.factors] == [
            "phase",
            "moisture",
            "houbymapa",
            "chmi_map",
            "horizon",
        ]
    assert outlook.as_dict()["peak"] == (TODAY, 45)


def test_a_worse_map_lowers_every_day_not_only_today():
    poor = horizon(chmi_level=1.0)
    good = horizon(chmi_level=5.0)
    for worse, better in zip(poor.outlook, good.outlook):
        assert worse.date == better.date
        assert worse.raw_percent < better.raw_percent


def test_the_horizon_ramps_over_the_days_since_the_anchor():
    """The rain was yesterday: the curve climbs, it does not switch on."""
    outlook = horizon(days=12, anchor_offset=-1)
    values = [item.value for item in outlook.outlook]
    # ages 1..12: up the ramp to the plateau at D+7, damped past D+3
    assert values[0] == 5  # 0.07 x 0.8 = 5.6 %
    assert values[6] == 45  # 0.60 x 0.8 x 0.925 (lead 6) = 44.4 %
    assert max(abs(b - a) for a, b in zip(values, values[1:])) <= 20
    assert values[:7] == sorted(values[:7])  # the climb never falls back
    # on the plateau only the lead-time damping is left, so it leans down
    assert values[6:] == sorted(values[6:], reverse=True)


def test_an_older_open_window_outranks_a_fresh_rain_across_the_horizon():
    anchors = [TODAY - timedelta(days=9), TODAY - timedelta(days=1)]
    both = horizon(anchors=anchors)
    older = horizon(anchors=[TODAY - timedelta(days=9)])
    for merged, single in zip(both.outlook, older.outlook):
        assert merged.value == single.value


def test_the_peak_is_the_best_day_earliest_first():
    outlook = horizon(days=3, anchor_offset=-6)
    payload = outlook.as_dict()
    assert payload["today"] == 30  # age 6: 0.375 x 0.8 = 30 %
    assert payload["peak"] == (TODAY + timedelta(days=1), 50)  # age 7: 48 %
    assert payload["curve"][TODAY + timedelta(days=2)] == 50
    assert payload["capped"] is False


def test_a_dead_station_caps_the_whole_curve():
    payload = horizon(station_available=False, chmi_level=5.0, houbymapa_score=1.0)
    assert payload.as_dict()["capped"] is True
    assert set(payload.as_dict()["curve"].values()) == {policy.CHANCE_NO_STATION_CAP}


def test_an_empty_database_reports_no_number_at_all():
    """The floor used to be printed for a location nothing was measured for.

    5 %, ``capped: false`` -- a number that looks like a dry forest and is
    really an empty table.  Today is still evaluated, and says so.
    """
    outlook = chance_lib.assess_chance_horizon(
        TODAY,
        [],
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

    assert outlook.current.date == TODAY
    assert outlook.current.value is None
    assert outlook.current.insufficient is True
    assert outlook.peak is None
    assert outlook.as_dict() == {
        "today": None,
        "curve": {TODAY: None},
        "peak": None,
        "capped": False,
    }


def test_past_days_are_dropped_and_never_damped():
    outlook = horizon(days=2)
    yesterday = TODAY - timedelta(days=1)
    with_past = chance_lib.assess_chance_horizon(
        TODAY,
        [TODAY - timedelta(days=9)],
        [yesterday, TODAY, TODAY + timedelta(days=1)],
        api30={day: 20.0 for day in (yesterday, TODAY, TODAY + timedelta(days=1))},
        api30_quality={
            day: DataQuality.FRESH
            for day in (yesterday, TODAY, TODAY + timedelta(days=1))
        },
        t_mean={day: 15.0 for day in (yesterday, TODAY, TODAY + timedelta(days=1))},
        t_min={day: 9.0 for day in (yesterday, TODAY, TODAY + timedelta(days=1))},
        frost_present=False,
        chmi_level=None,
        houbymapa_score=None,
        station_available=True,
    )
    assert [item.date for item in with_past.outlook] == [
        item.date for item in outlook.outlook
    ]
    assert dict(with_past.current.factors)["horizon"] == pytest.approx(1.0)


def test_the_whole_curve_moves_smoothly_day_by_day():
    """No cliff anywhere on a real-shaped horizon, whatever the inputs.

    The bound is the model's own steepest segment: one day of age may move
    the base by 0.225, and every other factor here is fixed, so nothing can
    move faster than that -- plus one rounding step for the reported value.
    """
    outlook = horizon(days=26, anchor_offset=0, chmi_level=5.0, houbymapa_score=1.0)
    bound = slope(policy.CHANCE_PHASE_RAMP) * 100 * 0.8 * 1.15
    percents = [item.raw_percent for item in outlook.outlook]
    assert max(abs(b - a) for a, b in zip(percents, percents[1:])) <= bound + 1e-9
    values = [item.value for item in outlook.outlook]
    assert max(abs(b - a) for a, b in zip(values, values[1:])) <= bound + policy.CHANCE_STEP
