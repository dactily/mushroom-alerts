"""One comparable number per location: the chance in percent (PLAN §9b).

Why a number and not a word
---------------------------
``biology.assess_day`` answers a safety question about one place: may this
site be called *high* today?  It refuses until the episode phase, the
moisture, the temperature, the history and the maps all allow it, and that
refusal is correct.  It is also useless for choosing between forests: a
measurement over eight locations on 12.09.2026 put every single one at
«средняя» while HoubyMapa ranged 0.62–0.90 and station API30 20–27 mm.  The
inputs separate the places, the four-word verdict does not.

So this module turns the *same* inputs into a percentage.  It is a
comparison aid, not a probability: "how much does this place today look
like the conditions under which mushrooms come".  Nothing here is
calibrated against finds -- that needs the feedback loop of PLAN §8 -- so
the result is reported in steps of 5 % inside 5–95 %, and a location whose
station is missing or stale is capped: a guess must not outrank a
measurement.

The verdict logic is deliberately untouched.  Both live side by side: the
verdict says what may be claimed, the chance says where to drive.

All of it is multiplicative, and every constant lives in :mod:`policy`::

    episode ramp × moisture × temperature × frost
                 × HoubyMapa × ČHMÚ × lead-time damping

Ramps, not steps
----------------
Three of those factors used to be step functions, and every step was a lie
the output could not hide:

* the phase base jumped 0.15 → 0.60 the morning a window opened, so a curve
  went 15 % → 75 % overnight -- mushrooms ramp up over days, so the base is
  now :data:`policy.CHANCE_PHASE_RAMP`, a piecewise-linear function of the
  days since the rain anchor.  With several episodes the **maximum** over
  them wins: a place can already be in an older rain's window while a newer
  rain is still in its waiting phase;
* the moisture bands gave 28.0 mm and 35.9 mm the same ×1.15 and then
  jumped by a third at one edge; API30 now enters through
  :data:`policy.CHANCE_MOISTURE_RAMP`, so the wetter place always scores
  higher.  :data:`policy.API30_BANDS_MM` stays, as the human-readable
  bands of the cheat sheet;
* the far horizon was undamped, so 75 % six days out read like a
  certainty it is not -- :data:`policy.CHANCE_HORIZON_DAMPING` now scales
  by lead time.

A consequence worth stating: because the base is continuous, **a day can no
longer be reproduced from the phase word alone**.  ``waiting`` covers
everything from the day of the rain to the day before the window opens, and
those are different numbers now.  The categorical phase string stays in the
output for the message wording; the arithmetic is in the ``factors``
breakdown of :class:`DayChance`.

No usable moisture number, no percentage
----------------------------------------
A missing API30 used to be treated as "no evidence either way" and enter as
a neutral ×1.0.  That is arithmetically tidy and practically backwards: 1.0
is a *better* multiplier than the ramp gives any API30 below 30 mm, so
losing the number raised the result.  In an open window 20 mm scored 45 %
and the same day with the number gone scored 55 % -- exactly the ten points
the send rule reacts to, earned by losing data.  A stale forecast release
did the same on a larger scale, and an empty database still reported the
5 % floor as if something had been measured.

So the rule is now the blunt one: without a usable API30 for that day there
is **no number at all**.  :attr:`DayChance.value` is ``None``,
:attr:`DayChance.insufficient` says so and :attr:`DayChance.reason` says
why; the report prints «нет данных» and names the location in its caveats.
A number that is absent is not a low number and must never be shown as one.

Maps are a property of the place, not of the day
------------------------------------------------
ČHMÚ and HoubyMapa publish no forecast.  The verdict therefore drops them
for future days, and that is right for a safety gate.  Doing the same in
the chance made days incomparable: identical conditions scored higher
tomorrow than today, because today's ČHMÚ 3/5 cost ×0.9 and tomorrow's cost
nothing, and ``максимум`` could be a pure artefact of that.  So the two map
multipliers are applied to *every* day of the horizon as a per-location
correction ("поправка места по сегодняшним картам"): what the maps mostly
encode is terrain and soil, whose relative ranking between locations
outlives the forecast.  A stale map still contributes nothing -- the caller
passes ``None`` for it, exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from . import policy
from .base import DataQuality

__all__ = [
    "DayChance",
    "ChanceOutlook",
    "chance_for_day",
    "assess_day_chance",
    "assess_chance_horizon",
]


#: Why a day carries no number.  Machine-readable: the Russian wording is
#: the report's business, not the arithmetic's.
NO_API30 = "api30_missing"
STALE_API30 = "api30_stale"


@dataclass(frozen=True, slots=True)
class DayChance:
    """The chance for one day, plus what produced it.

    ``value`` is ``None`` when the day had no usable API30.  Nothing was
    computed in that case: ``raw_percent`` is ``None``, ``factors`` is
    empty, and ``reason`` carries :data:`NO_API30` or :data:`STALE_API30`.
    """

    date: date | None
    value: int | None
    raw_percent: float | None
    capped: bool
    factors: tuple[tuple[str, float], ...]
    insufficient: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "value": self.value,
            "raw_percent": (
                None if self.raw_percent is None else round(self.raw_percent, 2)
            ),
            "capped": self.capped,
            "factors": [list(item) for item in self.factors],
            "insufficient": self.insufficient,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ChanceOutlook:
    """Today's chance and the same calculation across the forecast."""

    current: DayChance
    outlook: tuple[DayChance, ...]

    @property
    def peak(self) -> DayChance | None:
        """The best day that has a number; the earliest one wins a tie."""
        known = [item for item in self.outlook if item.value is not None]
        if not known:
            return None
        return min(known, key=lambda item: (-(item.value or 0), item.date or date.min))

    @property
    def capped(self) -> bool:
        return any(item.capped for item in self.outlook)

    def as_dict(self) -> dict[str, Any]:
        peak = self.peak
        return {
            "today": self.current.value,
            "curve": {item.date: item.value for item in self.outlook},
            "peak": None if peak is None else (peak.date, peak.value),
            "capped": self.capped,
        }


def _ramp(knots: Sequence[tuple[float, float]], x: float) -> float:
    """Piecewise-linear value of ``knots`` at ``x``.

    ``knots`` are ``(x, y)`` pairs ordered by ``x``.  Between two knots the
    value is a straight line; outside the range it is the first or the last
    value, so a ramp never extrapolates into a number nobody chose.  Every
    ramp of this module goes through here, so they cannot drift apart.
    """
    first_x, first_y = knots[0]
    if x <= first_x:
        return first_y
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if x <= x1:
            span = x1 - x0
            return y1 if span <= 0 else y0 + (y1 - y0) * (x - x0) / span
    return knots[-1][1]


def _quality(value: DataQuality | str | None) -> DataQuality:
    try:
        return value if isinstance(value, DataQuality) else DataQuality(str(value))
    except ValueError:
        return DataQuality.MISSING


def _phase_factor(days_since_anchor: Sequence[float]) -> float:
    """:data:`policy.CHANCE_PHASE_RAMP` over the best of the episodes.

    The maximum, not the newest and not the latest: an older rain whose
    window is open now must not be hidden by a fresh rain that is still in
    its waiting phase.  A place with no episode at all is treated as one
    whose window is long over -- the last knot of the ramp.
    """
    if not days_since_anchor:
        return policy.CHANCE_PHASE_RAMP[-1][1]
    return max(
        _ramp(policy.CHANCE_PHASE_RAMP, float(age)) for age in days_since_anchor
    )


def _unusable_reason(
    api30_mm: float | None, api30_quality: DataQuality | str | None
) -> str:
    """Why this day has no usable API30, or ``""`` when it has one.

    A missing number is not evidence of dryness -- but it is not evidence of
    moisture either, and every neutral value one could pick sits above part
    of :data:`policy.CHANCE_MOISTURE_RAMP`, so any choice would let lost
    data raise the number.  There is no percentage without a measurement.
    """
    quality = _quality(api30_quality)
    if quality is DataQuality.STALE:
        return STALE_API30
    if api30_mm is None or quality is DataQuality.MISSING:
        return NO_API30
    return ""


def _moisture_factor(api30_mm: float) -> float:
    """:data:`policy.CHANCE_MOISTURE_RAMP` at this API30."""
    return _ramp(policy.CHANCE_MOISTURE_RAMP, float(api30_mm))


def _temperature_ok(t_mean: float | None, t_min: float | None) -> bool:
    """The API30 temperature gate, identical to the verdict's."""
    if t_mean is None or t_min is None:
        return False
    return (
        policy.API30_T_MEAN_MIN <= float(t_mean) <= policy.API30_T_MEAN_MAX
        and float(t_min) > policy.API30_T_MIN_ABOVE
    )


def _round_to_step(percent: float) -> int:
    """Half-up rounding to :data:`policy.CHANCE_STEP` (``round`` is banker's)."""
    step = policy.CHANCE_STEP
    return int((percent / step) + 0.5) * step if percent >= 0 else 0


def assess_day_chance(
    *,
    days_since_anchor: Sequence[float],
    api30_mm: float | None,
    api30_quality: DataQuality | str | None,
    t_mean: float | None,
    t_min: float | None,
    frost_present: bool,
    chmi_level: float | None,
    houbymapa_score: float | None,
    station_available: bool,
    lead_days: float = 0,
    day: date | None = None,
) -> DayChance:
    """Evaluate one day.  Pure: same inputs, same number, always.

    ``days_since_anchor`` is the age of this day against every known rain
    episode, in days; an empty sequence means no qualifying rain at all.
    ``lead_days`` is ``day - today``; a past day damps by nothing.

    ``frost_present`` only lowers the chance when frost was actually
    measured; an incomplete minimum-temperature history is not evidence of
    frost (the verdict still refuses ``high`` on it).  ``chmi_level`` and
    ``houbymapa_score`` must already be ``None`` when the map is stale --
    freshness is the caller's read model, not arithmetic.

    Without a usable ``api30_mm`` the day gets no number at all: see the
    module docstring for why a neutral multiplier was worse than silence.
    """
    reason = _unusable_reason(api30_mm, api30_quality)
    if reason:
        return DayChance(
            date=day,
            value=None,
            raw_percent=None,
            capped=False,
            factors=(),
            insufficient=True,
            reason=reason,
        )

    factors: list[tuple[str, float]] = [("phase", _phase_factor(days_since_anchor))]

    # An empty ``reason`` already guarantees a number to stand on.
    factors.append(("moisture", _moisture_factor(float(api30_mm))))  # type: ignore[arg-type]
    if not _temperature_ok(t_mean, t_min):
        factors.append(("temperature", policy.CHANCE_TEMPERATURE_FAILED))
    if frost_present:
        factors.append(("frost", policy.CHANCE_FROST))
    # The maps correct the place, so they apply to every day of the horizon.
    if houbymapa_score is not None:
        factors.append(
            (
                "houbymapa",
                policy.CHANCE_HOUBYMAPA_BASE
                + policy.CHANCE_HOUBYMAPA_SPAN * float(houbymapa_score),
            )
        )
    if chmi_level is not None:
        factors.append(
            (
                "chmi_map",
                policy.CHANCE_CHMI_BASE
                + policy.CHANCE_CHMI_STEP
                * (float(chmi_level) - policy.CHANCE_CHMI_PIVOT),
            )
        )
    factors.append(
        ("horizon", _ramp(policy.CHANCE_HORIZON_DAMPING, float(lead_days)))
    )

    raw = 1.0
    for _, factor in factors:
        raw *= factor
    percent = raw * 100.0

    capped = not station_available and percent > policy.CHANCE_NO_STATION_CAP
    if capped:
        percent = float(policy.CHANCE_NO_STATION_CAP)

    value = min(max(_round_to_step(percent), policy.CHANCE_MIN), policy.CHANCE_MAX)
    return DayChance(
        date=day,
        value=value,
        raw_percent=raw * 100.0,
        capped=capped,
        factors=tuple(factors),
    )


def chance_for_day(**kwargs: Any) -> int | None:
    """The percentage alone -- see :func:`assess_day_chance` for the inputs.

    ``None`` when the day had no usable API30 and therefore no number.
    """
    return assess_day_chance(**kwargs).value


def assess_chance_horizon(
    today: date,
    anchors: Sequence[date],
    days: Sequence[date],
    *,
    api30: Mapping[date, float],
    api30_quality: Mapping[date, DataQuality | str],
    t_mean: Mapping[date, float],
    t_min: Mapping[date, float],
    frost_present: bool,
    chmi_level: float | None,
    houbymapa_score: float | None,
    station_available: bool,
) -> ChanceOutlook:
    """Today and every forecast day, on one ramp and one set of maps.

    ``anchors`` are the peak days of the known rain episodes -- the same
    episodes the verdict reasons about, reduced to what the ramp needs.
    """
    ordered = sorted({today, *(day for day in days if day >= today)})
    outlook = tuple(
        assess_day_chance(
            days_since_anchor=[(day - anchor).days for anchor in anchors],
            api30_mm=api30.get(day),
            api30_quality=api30_quality.get(day),
            t_mean=t_mean.get(day),
            t_min=t_min.get(day),
            frost_present=frost_present,
            chmi_level=chmi_level,
            houbymapa_score=houbymapa_score,
            station_available=station_available,
            lead_days=(day - today).days,
            day=day,
        )
        for day in ordered
    )
    current = next(item for item in outlook if item.date == today)
    return ChanceOutlook(current, outlook)
