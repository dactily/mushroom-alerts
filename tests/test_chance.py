"""``chance``: the comparable per-location percentage (PLAN §9b).

The number is calibrated against the ČHMÚ map and nothing else, so what can
be pinned down here is the arithmetic: every contribution in isolation, the
weighted mean they enter, the rounding, the bounds, the station cap -- and
the four properties earlier shapes of this model broke:

* **no contribution is a gate.**  The phase used to be a multiplier of 0.05
  outside a window, so soaked ground under two enthusiastic maps still
  reported the 5 % floor.  It is a term of a weighted mean now, and the
  regression test at the bottom of this file is the measurement that forced
  the change;
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

#: Nine days after the rain (the plateau of the phase ramp, 1.00), API30 on
#: the middle knot of the moisture ramp (25 mm -> 0.55), ČHMÚ 3/5 and a
#: HoubyMapa score of 0.5 (both map halves 0.50, so the map term is 0.50),
#: a satisfied temperature gate, no frost and no lead time.
#:
#: Both maps are present on purpose: with all three contributions carrying
#: data the weights sum to one and the arithmetic of every test below is
#: ``0.40·moisture + 0.30·phase + 0.30·map`` with nothing to renormalise.
NEUTRAL = dict(
    days_since_anchor=(9,),
    api30_mm=25.0,
    api30_quality=DataQuality.FRESH,
    t_mean=15.0,
    t_min=9.0,
    frost_present=False,
    chmi_level=3.0,
    houbymapa_score=0.5,
    station_available=True,
)

#: ``0.40x0.55 + 0.30x1.00 + 0.30x0.50 = 0.67`` of 90 % = 60.3 %, reported
#: as 60 %.  Every test varies one input away from this.
BASE = 60.3


def chance(**kw) -> int:
    return chance_lib.chance_for_day(**{**NEUTRAL, **kw})


def raw(**kw) -> float:
    return chance_lib.assess_day_chance(**{**NEUTRAL, **kw}).raw_percent


def factors(**kw) -> dict[str, float]:
    return dict(chance_lib.assess_day_chance(**{**NEUTRAL, **kw}).factors)


def blend(moisture: float, phase: float, map_term: float | None) -> float:
    """The percentage those three contributions are worth, by hand."""
    parts = [
        (policy.CHANCE_WEIGHT_MOISTURE, moisture),
        (policy.CHANCE_WEIGHT_PHASE, phase),
    ]
    if map_term is not None:
        parts.append((policy.CHANCE_WEIGHT_MAP, map_term))
    total = sum(weight for weight, _ in parts)
    return policy.CHANCE_FULL_PCT * sum(w * t for w, t in parts) / total


def slope(knots) -> float:
    """The steepest segment of a ramp, per unit of x."""
    return max(
        abs(y1 - y0) / (x1 - x0) for (x0, y0), (x1, y1) in zip(knots, knots[1:])
    )


#: How much one unit of the phase ramp is worth in percent, with every
#: contribution present.
PHASE_SHARE = policy.CHANCE_WEIGHT_PHASE * policy.CHANCE_FULL_PCT


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
        policy.CHANCE_CHMI_RAMP,
        policy.CHANCE_HORIZON_DAMPING,
    ],
)
def test_every_policy_ramp_is_ordered_and_hits_its_own_knots(knots):
    """One helper, four ramps: each must reproduce its table exactly."""
    xs = [x for x, _ in knots]
    assert xs == sorted(xs) and len(set(xs)) == len(xs)
    for x, y in knots:
        assert chance_lib._ramp(knots, float(x)) == pytest.approx(y)


@pytest.mark.parametrize(
    "knots", [policy.CHANCE_PHASE_RAMP, policy.CHANCE_MOISTURE_RAMP, policy.CHANCE_CHMI_RAMP]
)
def test_the_three_contributions_are_ramps_into_zero_to_one(knots):
    """They are terms of a mean, not multipliers: nothing may exceed 1."""
    assert all(0.0 <= y <= 1.0 for _, y in knots)


def test_the_weights_of_the_three_contributions_sum_to_one():
    """Not required by the arithmetic -- it renormalises -- but by the reader.

    ``CHANCE_FULL_PCT`` is documented as what a place scores when all three
    agree, and that is only true while the weights add up.
    """
    assert (
        policy.CHANCE_WEIGHT_MOISTURE
        + policy.CHANCE_WEIGHT_PHASE
        + policy.CHANCE_WEIGHT_MAP
    ) == pytest.approx(1.0)


# ----------------------------------------------------------------------
# the weighted mean: the shape the refit exists for
# ----------------------------------------------------------------------
def test_an_expired_window_over_soaked_ground_is_not_the_floor_any_more():
    """The measurement that forced this shape (2026-09-13, northern Czechia).

    Krušné hory, Orlické hory and Krušné hory east: ČHMÚ 5/5, HoubyMapa
    0.83–1.00, station API30 23–36 mm, and the rain episode's window long
    over.  The old model multiplied by the phase, so all three reported
    5–10 %.  With the phase a contribution instead of a gate, the ground and
    the maps are allowed to carry the number.
    """
    soaked_but_expired = chance(
        days_since_anchor=(30,), api30_mm=45.0, chmi_level=5.0, houbymapa_score=1.0
    )
    assert soaked_but_expired == 65
    # the old arithmetic, for the record: 0.05 x 1.25 x 1.15 x 100 = 7.2 %
    assert dict(
        chance_lib.assess_day_chance(
            **{
                **NEUTRAL,
                "days_since_anchor": (30,),
                "api30_mm": 45.0,
                "chmi_level": 5.0,
                "houbymapa_score": 1.0,
            }
        ).factors
    )["phase"] == pytest.approx(policy.CHANCE_PHASE_RAMP[-1][1])


def test_moisture_alone_can_carry_the_number():
    """Worst phase, worst maps, wet ground: still four tenths of the scale."""
    value = chance(
        days_since_anchor=(), api30_mm=40.0, chmi_level=1.0, houbymapa_score=0.0
    )
    assert value == 40
    assert raw(
        days_since_anchor=(), api30_mm=40.0, chmi_level=1.0, houbymapa_score=0.0
    ) == pytest.approx(blend(1.0, policy.CHANCE_PHASE_RAMP[-1][1], 0.0))


def test_no_single_contribution_can_zero_the_number():
    """A gate is a multiplier that reaches zero; none of these is one."""
    worst = dict(
        days_since_anchor=(),
        api30_mm=0.0,
        chmi_level=1.0,
        houbymapa_score=0.0,
    )
    for name, kw in (
        ("phase", {"days_since_anchor": ()}),
        ("moisture", {"api30_mm": 0.0}),
        ("map", {"chmi_level": 1.0, "houbymapa_score": 0.0}),
    ):
        alone = raw(**kw)
        assert alone > 0.5 * BASE, f"{name} behaves like a gate"
    # and all three at once still lands on the reported floor, not below it
    assert chance(**worst) == policy.CHANCE_MIN


def test_the_blend_is_the_weighted_mean_and_is_reported_as_a_factor():
    item = chance_lib.assess_day_chance(**NEUTRAL)
    named = dict(item.factors)
    assert named["blend"] == pytest.approx(
        policy.CHANCE_WEIGHT_MOISTURE * named["moisture"]
        + policy.CHANCE_WEIGHT_PHASE * named["phase"]
        + policy.CHANCE_WEIGHT_MAP * named["map"]
    )
    assert item.raw_percent == pytest.approx(policy.CHANCE_FULL_PCT * named["blend"])
    assert item.raw_percent == pytest.approx(BASE)
    assert item.value == 60


def test_a_missing_contribution_drops_out_and_the_weights_renormalise():
    """A location whose maps went stale stays comparable, not merely poorer.

    Counting the absent term as a zero would cost it 30 % of the scale for
    something it never claimed.
    """
    without = chance_lib.assess_day_chance(
        **{**NEUTRAL, "chmi_level": None, "houbymapa_score": None}
    )
    named = dict(without.factors)
    assert "map" not in named
    assert named["blend"] == pytest.approx(
        (
            policy.CHANCE_WEIGHT_MOISTURE * named["moisture"]
            + policy.CHANCE_WEIGHT_PHASE * named["phase"]
        )
        / (policy.CHANCE_WEIGHT_MOISTURE + policy.CHANCE_WEIGHT_PHASE)
    )
    assert without.raw_percent == pytest.approx(blend(0.55, 1.0, None))
    # the same two contributions, scored as if the maps had said zero
    as_zero = blend(0.55, 1.0, 0.0)
    assert without.raw_percent > as_zero


# ----------------------------------------------------------------------
# the phase contribution: days since the rain, not a word
# ----------------------------------------------------------------------
@pytest.mark.parametrize("age, term", list(policy.CHANCE_PHASE_RAMP))
def test_the_phase_ramp_hits_its_knots(age, term):
    assert raw(days_since_anchor=(age,)) == pytest.approx(blend(0.55, term, 0.50))


@pytest.mark.parametrize(
    "age, term",
    [
        (2.0, 0.18),  # 0.10 + 2/5 x 0.20
        (6.0, 0.65),  # halfway up the steep climb into the window
        (9.0, 1.00),  # the plateau
        (14.0, 0.80),  # halfway down to 0.60
        (18.5, 0.45),
        (23.0, 0.225),
    ],
)
def test_the_phase_ramp_is_linear_between_its_knots(age, term):
    assert raw(days_since_anchor=(age,)) == pytest.approx(blend(0.55, term, 0.50))


@pytest.mark.parametrize("age", [-5.0, -1.0, 26.0, 400.0])
def test_the_phase_ramp_is_clamped_outside_its_range(age):
    """The knot values either way: before the rain and long after the window."""
    expected = policy.CHANCE_PHASE_RAMP[0][1] if age < 0 else policy.CHANCE_PHASE_RAMP[-1][1]
    assert raw(days_since_anchor=(age,)) == pytest.approx(blend(0.55, expected, 0.50))


def test_no_episode_scores_like_a_window_long_over():
    assert raw(days_since_anchor=()) == pytest.approx(
        blend(0.55, policy.CHANCE_PHASE_RAMP[-1][1], 0.50)
    )


def test_several_episodes_take_the_best_ramp_value():
    """An open older window is not hidden by a fresh rain still waiting.

    The anchors of a soak arrive here the same way (one per day it went on
    being wet), so this is also what keeps a running soak in its window.
    """
    fresh_rain, open_window = 1.0, 9.0
    assert raw(days_since_anchor=(fresh_rain,)) == pytest.approx(
        blend(0.55, 0.14, 0.50)
    )
    assert raw(days_since_anchor=(fresh_rain, open_window)) == pytest.approx(BASE)
    # order is irrelevant, and an expired episode adds nothing
    assert raw(days_since_anchor=(open_window, fresh_rain)) == pytest.approx(BASE)
    assert raw(days_since_anchor=(9.0, 40.0)) == pytest.approx(BASE)


def test_the_phase_cliff_is_gone():
    """The defect this ramp exists for: 0.15 -> 0.60 overnight.

    With everything else fixed, one day of age may move the result by at
    most the steepest segment of the ramp times its weight -- about 9.5
    points.  The step function moved 45 points of base, and because the base
    was a multiplier the printed number moved further still.
    """
    bound = slope(policy.CHANCE_PHASE_RAMP) * PHASE_SHARE
    curve = [raw(days_since_anchor=(float(age),)) for age in range(0, 31)]
    assert max(abs(b - a) for a, b in zip(curve, curve[1:])) <= bound + 1e-9
    assert bound < 10.0


# ----------------------------------------------------------------------
# moisture: a week of API30, on a ramp, not four bands
# ----------------------------------------------------------------------
@pytest.mark.parametrize("api30, term", list(policy.CHANCE_MOISTURE_RAMP))
def test_moisture_hits_the_knots_of_its_ramp(api30, term):
    assert raw(api30_mm=api30) == pytest.approx(blend(term, 1.0, 0.50))


@pytest.mark.parametrize(
    "api30, term",
    [
        (0.0, 0.05),  # clamped dry
        (15.0, 0.2166667),
        (25.0, 0.55),
        (28.0, 0.64),
        (35.9, 0.877),
        (37.5, 0.925),
        (120.0, 1.00),  # clamped wet
    ],
)
def test_moisture_is_linear_between_the_knots(api30, term):
    assert raw(api30_mm=api30) == pytest.approx(blend(term, 1.0, 0.50), rel=1e-4)


def test_the_wetter_place_always_scores_higher():
    """The bands tied 28.0 mm with 35.9 mm and then jumped at one edge."""
    values = [raw(api30_mm=mm) for mm in (10.0, 20.0, 28.0, 30.0, 35.9, 45.0)]
    assert values == sorted(values)
    assert raw(api30_mm=35.9) > raw(api30_mm=28.0)
    # and one millimetre is worth at most one millimetre of the ramp
    step = slope(policy.CHANCE_MOISTURE_RAMP) * policy.CHANCE_WEIGHT_MOISTURE * policy.CHANCE_FULL_PCT
    assert abs(raw(api30_mm=30.0) - raw(api30_mm=29.0)) <= step + 1e-9


def test_moisture_is_the_mean_of_the_week_not_of_the_morning():
    """Six dry days behind a wet morning are not a wet forest.

    A single day's API30 barely separated the ČHMÚ levels at all over the
    country sample; the week mean does, and so does the mycology.
    """
    week = (10.0,) * 6
    item = chance_lib.assess_day_chance(**{**NEUTRAL, "api30_mm": 45.0, "api30_week": week})
    assert item.moisture_mm == pytest.approx(15.0)  # (45 + 6x10) / 7
    assert item.raw_percent == pytest.approx(blend(0.2166667, 1.0, 0.50), rel=1e-4)
    # ... and the same morning after a wet week is worth much more
    assert raw(api30_mm=45.0, api30_week=(45.0,) * 6) > item.raw_percent


def test_the_moisture_window_is_capped_at_its_policy_length():
    """Handing over more history than the window must not widen it."""
    long_tail = chance_lib.assess_day_chance(
        **{**NEUTRAL, "api30_mm": 45.0, "api30_week": (10.0,) * 20}
    )
    short = chance_lib.assess_day_chance(
        **{
            **NEUTRAL,
            "api30_mm": 45.0,
            "api30_week": (10.0,) * (policy.CHANCE_MOISTURE_DAYS - 1),
        }
    )
    assert long_tail.moisture_mm == pytest.approx(short.moisture_mm)


def test_a_short_history_averages_what_there_is():
    """Missing days are absent, not zero: a hole must not invent a drought."""
    item = chance_lib.assess_day_chance(
        **{**NEUTRAL, "api30_mm": 30.0, "api30_week": (20.0,)}
    )
    assert item.moisture_mm == pytest.approx(25.0)
    assert item.raw_percent == pytest.approx(BASE)


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

    A missing API30 used to enter as a neutral value above most of the
    moisture ramp: the number went *up* when the measurement disappeared.
    Moisture is the one contribution that never drops out -- it takes the
    whole day with it.
    """
    item = chance_lib.assess_day_chance(**{**NEUTRAL, **kw})

    assert item.value is None
    assert item.insufficient is True
    assert item.reason == reason
    assert item.raw_percent is None
    assert item.factors == ()
    assert item.capped is False
    assert item.moisture_mm is None


def test_a_week_of_history_does_not_rescue_a_day_without_its_own_number():
    """The day's own API30 is the gate; the week only shapes the term."""
    item = chance_lib.assess_day_chance(
        **{**NEUTRAL, "api30_mm": None, "api30_week": (30.0,) * 6}
    )
    assert item.value is None and item.reason == chance_lib.NO_API30


def test_a_partial_api30_still_carries_its_measurement():
    assert raw(api30_quality=DataQuality.PARTIAL) == pytest.approx(BASE)
    assert chance(api30_quality=DataQuality.PARTIAL) == 60


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
    assert raw(t_mean=t_mean, t_min=t_min) == pytest.approx(BASE)


@pytest.mark.parametrize(
    "t_mean, t_min",
    [(7.9, 9.0), (22.1, 9.0), (15.0, 2.0), (None, 9.0), (15.0, None)],
)
def test_a_failed_or_unknown_temperature_gate_costs_a_multiplier(t_mean, t_min):
    """Still a multiplier: this one really does suppress, it does not vote."""
    assert raw(t_mean=t_mean, t_min=t_min) == pytest.approx(BASE * 0.6)


def test_frost_costs_a_multiplier():
    assert raw(frost_present=True) == pytest.approx(BASE * 0.4)


@pytest.mark.parametrize(
    "level, term", [(1.0, 0.0), (2.0, 0.25), (3.0, 0.5), (4.0, 0.75), (5.0, 1.0)]
)
def test_the_chmi_level_is_normalised_onto_zero_to_one(level, term):
    assert raw(chmi_level=level, houbymapa_score=None) == pytest.approx(
        blend(0.55, 1.0, term)
    )


@pytest.mark.parametrize("score", [0.0, 0.39, 0.5, 0.63, 1.0])
def test_the_houbymapa_score_enters_exactly_as_published(score):
    """It is not rescaled onto ČHMÚ's levels: it is an independent model.

    Over the country sample HoubyMapa read 0.57 where ČHMÚ said 2/5.
    Stretching one onto the other would have improved the fit against ČHMÚ
    and thrown away the only second opinion we have.
    """
    assert raw(chmi_level=None, houbymapa_score=score) == pytest.approx(
        blend(0.55, 1.0, score)
    )


def test_the_map_term_is_the_mean_of_the_two_maps():
    assert raw(chmi_level=5.0, houbymapa_score=0.6) == pytest.approx(
        blend(0.55, 1.0, (1.0 + 0.6) / 2)
    )
    # they disagree by a level and a half here; neither wins outright
    assert raw(chmi_level=5.0, houbymapa_score=0.6) < raw(
        chmi_level=5.0, houbymapa_score=1.0
    )


def test_a_stale_map_contributes_nothing():
    """Freshness is the caller's job: a stale map arrives as ``None``."""
    assert factors(chmi_level=None, houbymapa_score=None).keys() == {
        "moisture",
        "phase",
        "blend",
        "horizon",
    }
    # one of the two left: the term is that one alone, not an average with 0
    assert raw(chmi_level=None, houbymapa_score=0.8) == pytest.approx(
        blend(0.55, 1.0, 0.8)
    )


# ----------------------------------------------------------------------
# the lead-time damping
# ----------------------------------------------------------------------
@pytest.mark.parametrize("lead, factor", list(policy.CHANCE_HORIZON_DAMPING))
def test_the_damping_hits_the_knots_of_its_ramp(lead, factor):
    assert raw(lead_days=lead) == pytest.approx(BASE * factor)


@pytest.mark.parametrize(
    "lead, factor",
    [(-3.0, 1.0), (1.0, 1.0), (5.0, 0.95), (11.5, 0.825), (30.0, 0.75)],
)
def test_the_damping_is_linear_between_the_knots_and_clamped_outside(lead, factor):
    assert raw(lead_days=lead) == pytest.approx(BASE * factor)


def test_the_damping_never_grows_with_lead_time():
    curve = [raw(lead_days=float(lead)) for lead in range(-2, 25)]
    assert all(later <= earlier + 1e-9 for earlier, later in zip(curve, curve[1:]))
    # flat for the days the forecast is worth something, then strictly down
    assert raw(lead_days=3) == pytest.approx(raw(lead_days=0))
    assert raw(lead_days=7) < raw(lead_days=3)
    assert raw(lead_days=16) < raw(lead_days=7)
    assert raw(lead_days=6) == pytest.approx(BASE * 0.925)


def test_the_damping_is_listed_as_its_own_factor():
    assert factors(lead_days=7)["horizon"] == pytest.approx(0.9)


# ----------------------------------------------------------------------
# rounding, bounds, the cap
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "api30, expected",
    [(25.0, 60), (0.0, 40), (30.0, 65), (40.0, 75)],
)
def test_the_result_is_rounded_to_five(api30, expected):
    """No false precision: 60.3 % is reported as 60 %, 42.3 % as 40 %."""
    assert chance(api30_mm=api30) == expected


@pytest.mark.parametrize(
    "percent, expected",
    [(2.4, 0), (2.5, 5), (7.5, 10), (12.5, 15), (22.5, 25), (87.5, 90)],
)
def test_rounding_is_half_up_not_bankers(percent, expected):
    """``round`` is banker's: it would send 2.5 % to 0 and 22.5 % to 20."""
    assert chance_lib._round_to_step(percent) == expected


def test_the_result_is_clamped_to_the_reported_range():
    floor = chance(
        days_since_anchor=(),
        api30_mm=0.0,
        chmi_level=1.0,
        houbymapa_score=0.0,
        frost_present=True,
    )
    assert floor == policy.CHANCE_MIN
    ceiling = chance(api30_mm=100.0, chmi_level=5.0, houbymapa_score=1.0)
    assert ceiling <= policy.CHANCE_MAX
    # everything at 1.0 is exactly CHANCE_FULL_PCT, which is inside the range
    assert ceiling == 90


def test_a_dead_station_caps_the_number():
    wet = dict(api30_mm=45.0, chmi_level=5.0, houbymapa_score=1.0)
    assert chance(**wet) == 90

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
            "days_since_anchor": (),
            "api30_mm": 0.0,
            "chmi_level": 1.0,
            "houbymapa_score": 0.0,
            "station_available": False,
        }
    )
    assert low.value == policy.CHANCE_MIN and low.capped is False


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
        "moisture",
        "phase",
        "map",
        "blend",
        "temperature",
        "frost",
        "horizon",
    ]
    named = dict(item.factors)
    expected = policy.CHANCE_FULL_PCT * named["blend"]
    for key in ("temperature", "frost", "horizon"):
        expected *= named[key]
    assert item.raw_percent == pytest.approx(expected)
    assert item.as_dict()["moisture_mm"] == pytest.approx(25.0)


# ----------------------------------------------------------------------
# the horizon
# ----------------------------------------------------------------------
def horizon(days=4, *, anchor_offset=-9, **kw):
    days_list = [TODAY + timedelta(days=n) for n in range(0, days)]
    api30 = kw.get("api30")
    if api30 is None:
        api30 = {day: 25.0 for day in days_list}
        # a week of history behind today, so the moisture window is full
        api30.update(
            {TODAY - timedelta(days=n): 25.0 for n in range(1, policy.CHANCE_MOISTURE_DAYS)}
        )
    return chance_lib.assess_chance_horizon(
        TODAY,
        kw.get("anchors", [TODAY + timedelta(days=anchor_offset)]),
        days_list,
        api30=api30,
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
    assert outlook.current.value == 60
    for item in outlook.outlook:
        assert item.value == 60
        assert [name for name, _ in item.factors] == [
            "moisture",
            "phase",
            "map",
            "blend",
            "horizon",
        ]
    assert outlook.as_dict()["peak"] == (TODAY, 60)


def test_a_worse_map_lowers_every_day_not_only_today():
    poor = horizon(chmi_level=1.0)
    good = horizon(chmi_level=5.0)
    for worse, better in zip(poor.outlook, good.outlook):
        assert worse.date == better.date
        assert worse.raw_percent < better.raw_percent


def test_the_horizon_reads_the_moisture_window_out_of_the_days_behind_today():
    """``api30`` may reach back before ``today``, and the first week uses it."""
    days = [TODAY + timedelta(days=n) for n in range(0, 3)]
    api30 = {day: 40.0 for day in days}
    api30.update({TODAY - timedelta(days=n): 5.0 for n in range(1, 7)})
    outlook = chance_lib.assess_chance_horizon(
        TODAY,
        [TODAY - timedelta(days=9)],
        days,
        api30=api30,
        api30_quality={day: DataQuality.FRESH for day in days},
        t_mean={day: 15.0 for day in days},
        t_min={day: 9.0 for day in days},
        frost_present=False,
        chmi_level=None,
        houbymapa_score=None,
        station_available=True,
    )
    # today: (40 + 6x5) / 7 = 10.0; two days on: (3x40 + 4x5) / 7 = 20.0
    assert outlook.current.moisture_mm == pytest.approx(10.0)
    assert outlook.outlook[2].moisture_mm == pytest.approx(140 / 7)
    assert [item.date for item in outlook.outlook] == days  # no extra rows


def test_the_horizon_ramps_over_the_days_since_the_anchor():
    """The rain was yesterday: the curve climbs, it does not switch on."""
    outlook = horizon(days=12, anchor_offset=-1)
    values = [item.value for item in outlook.outlook]
    # no maps here, so the two remaining weights renormalise over 0.7:
    # age 1 -> (0.40x0.55 + 0.30x0.14) / 0.70 x 90 = 33.7 %
    assert values[0] == 35
    assert max(abs(b - a) for a, b in zip(values, values[1:])) <= 15
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
    assert payload["today"] == 55  # age 6: phase 0.65
    assert payload["peak"] == (TODAY + timedelta(days=1), 65)  # age 7: plateau
    assert payload["curve"][TODAY + timedelta(days=2)] == 65
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
        "moisture_mm": None,
    }


def test_past_days_are_dropped_and_never_damped():
    outlook = horizon(days=2)
    yesterday = TODAY - timedelta(days=1)
    spanned = [yesterday, TODAY, TODAY + timedelta(days=1)]
    with_past = chance_lib.assess_chance_horizon(
        TODAY,
        [TODAY - timedelta(days=9)],
        spanned,
        api30={
            **{day: 25.0 for day in spanned},
            **{
                TODAY - timedelta(days=n): 25.0
                for n in range(1, policy.CHANCE_MOISTURE_DAYS)
            },
        },
        api30_quality={day: DataQuality.FRESH for day in spanned},
        t_mean={day: 15.0 for day in spanned},
        t_min={day: 9.0 for day in spanned},
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

    The bound is the model's own steepest segment: with the moisture flat
    and the maps fixed, only the phase and the damping move, and the phase
    may move by 0.35 of a term worth 0.30 of the scale.
    """
    outlook = horizon(days=26, anchor_offset=0, chmi_level=5.0, houbymapa_score=1.0)
    bound = slope(policy.CHANCE_PHASE_RAMP) * PHASE_SHARE
    percents = [item.raw_percent for item in outlook.outlook]
    assert max(abs(b - a) for a, b in zip(percents, percents[1:])) <= bound + 1e-9
    values = [item.value for item in outlook.outlook]
    assert max(abs(b - a) for a, b in zip(values, values[1:])) <= bound + policy.CHANCE_STEP
