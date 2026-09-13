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

A weighted mean, not a product
------------------------------
Every constant lives in :mod:`policy`, and the arithmetic is::

    score   = (w_moisture·moisture + w_phase·phase + w_map·map)
            / (the weights of the contributions that had data)
    percent = CHANCE_FULL_PCT · score
            × temperature × frost × lead-time damping

The phase used to be a **factor**, and that was the defect this shape
exists for.  Outside a rain episode's window it was 0.05, so it multiplied
everything else away: soaked ground plus two maps calling the place
excellent still produced the 5 % floor.  Measured on 2026-09-13 over the
eight strongest ČHMÚ zones of northern Czechia, seven of them scored 5–20 %
while ČHMÚ said 5/5 and HoubyMapa 0.83–1.00 (``tools/sample_chmu.py``).
A gate disguised as a multiplier.

So the three contributions now *share* the number.  Each is 0..1, none can
zero the others, and a contribution with no data drops out of both the sum
and the divisor -- a location whose maps went stale stays comparable with
one whose maps are fresh, instead of being quietly discounted.  What stayed
multiplicative is what genuinely suppresses: a failed temperature gate,
frost, and the distance into the forecast.

Moisture is a week, not a morning
---------------------------------
``moisture`` reads the mean API30 of the last
:data:`policy.CHANCE_MOISTURE_DAYS` days, not the day's own value.  Over
the country sample a single day's API30 hardly separated the ČHMÚ levels at
all (Spearman 0.32; medians 22 / 27 / 28 / 32 mm for levels 2–5) because a
first rain on ground that had been dry for weeks looks exactly like ground
that has been wet all month.  The week mean separates them (0.70; 15 / 22 /
24 / 33 mm), and it is the mycological statement as well: what fruits is
ground that has *stayed* wet.  A fresh soaking still lifts the number the
day it falls -- it enters the mean immediately and fills it over the week,
while the phase term opens seven days later.

Two kinds of episode
--------------------
``days_since_anchor`` now carries the anchors of both kinds:
``biology.detect_rain_episodes`` for a downpour and
``biology.detect_soak_episodes`` for a stand that went wet slowly and
stayed wet.  Around Liberec the station had API30 26–32 mm out of drizzle,
best three-day window 7 mm: no pulse, wet ground, and the old model called
it 10 %.  The **maximum** over the anchors wins, as before.

Ramps, not steps
----------------
Three parts of this used to be step functions, and every step was a lie
the output could not hide:

* the phase term jumped 0.15 → 0.60 the morning a window opened, so a curve
  went 15 % → 75 % overnight -- mushrooms ramp up over days, so it is now
  :data:`policy.CHANCE_PHASE_RAMP`, a piecewise-linear function of the
  days since the rain anchor.  With several episodes the **maximum** over
  them wins: a place can already be in an older rain's window while a newer
  rain is still in its waiting phase;
* the moisture bands gave 28.0 mm and 35.9 mm the same value and then
  jumped by a third at one edge; API30 now enters through
  :data:`policy.CHANCE_MOISTURE_RAMP`, so the wetter place always scores
  higher.  :data:`policy.API30_BANDS_MM` stays, as the human-readable
  bands of the cheat sheet;
* the far horizon was undamped, so 75 % six days out read like a
  certainty it is not -- :data:`policy.CHANCE_HORIZON_DAMPING` now scales
  by lead time.

A consequence worth stating: because the terms are continuous, **a day can
no longer be reproduced from the phase word alone**.  ``waiting`` covers
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
nothing, and ``максимум`` could be a pure artefact of that.  So the map
contribution counts on *every* day of the horizon as a per-location
correction ("поправка места по сегодняшним картам"): what the maps mostly
encode is terrain and soil, whose relative ranking between locations
outlives the forecast.  A stale map still contributes nothing -- the caller
passes ``None`` for it, exactly as before -- and with both of them stale
the whole term drops out and its weight goes to the other two, which is
what makes such a location comparable rather than merely poorer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
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

    ``factors`` explains the arithmetic in the order it happens: the three
    weighted contributions, then ``blend`` -- the weighted mean they
    produced, i.e. the fraction of :data:`policy.CHANCE_FULL_PCT` this day
    earned -- then whatever multiplied it afterwards.  ``moisture_mm`` is
    the week mean the moisture contribution was read from, so the printed
    number can be reproduced by hand.
    """

    date: date | None
    value: int | None
    raw_percent: float | None
    capped: bool
    factors: tuple[tuple[str, float], ...]
    insufficient: bool = False
    reason: str = ""
    moisture_mm: float | None = None

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
            "moisture_mm": (
                None if self.moisture_mm is None else round(self.moisture_mm, 2)
            ),
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
            # The week the moisture contribution was read from.  The report
            # prints it next to today's own API30, because since the term
            # became a week mean the day's number alone no longer lets a
            # reader reproduce the percentage that is meant to explain it.
            "moisture_mm": (
                None
                if self.current.moisture_mm is None
                else round(self.current.moisture_mm, 1)
            ),
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


def _phase_term(days_since_anchor: Sequence[float]) -> float:
    """:data:`policy.CHANCE_PHASE_RAMP` over the best of the anchors.

    The maximum, not the newest and not the latest: an older rain whose
    window is open now must not be hidden by a fresh rain that is still in
    its waiting phase.  The same maximum is what keeps a running soak in
    its window, since a soak hands over one anchor per day it lasted.

    A place with no episode at all is treated as one whose window is long
    over -- the last knot of the ramp.  Not zero: "no fresh trigger" is a
    statement about the timing, not a veto on the other contributions.
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


def _moisture_mm(api30_mm: float, api30_week: Sequence[float]) -> float:
    """Mean API30 of the moisture window, ending on this day.

    ``api30_week`` are the days *before* it, newest first; only the first
    :data:`policy.CHANCE_MOISTURE_DAYS`-1 of them count, and days the
    caller had no usable number for are simply absent, so a short history
    averages over what there is instead of inventing dry days.
    """
    values = [float(api30_mm), *(float(v) for v in api30_week)]
    window = values[: max(1, policy.CHANCE_MOISTURE_DAYS)]
    return sum(window) / len(window)


def _moisture_term(moisture_mm: float) -> float:
    """:data:`policy.CHANCE_MOISTURE_RAMP` at this week mean."""
    return _ramp(policy.CHANCE_MOISTURE_RAMP, float(moisture_mm))


def _map_term(
    chmi_level: float | None, houbymapa_score: float | None
) -> float | None:
    """The mean of whichever published map is fresh, or ``None`` for neither.

    ČHMÚ is normalised by :data:`policy.CHANCE_CHMI_RAMP`; HoubyMapa's score
    is already 0..1 and is left alone.  Rescaling it onto ČHMÚ's levels
    would be the one thing this model must not do -- it is an independent
    model, and averaging the two is how their disagreement gets a price.
    """
    terms = [
        term
        for term in (
            None
            if chmi_level is None
            else _ramp(policy.CHANCE_CHMI_RAMP, float(chmi_level)),
            None if houbymapa_score is None else float(houbymapa_score),
        )
        if term is not None
    ]
    return sum(terms) / len(terms) if terms else None


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
    api30_week: Sequence[float] = (),
    lead_days: float = 0,
    day: date | None = None,
) -> DayChance:
    """Evaluate one day.  Pure: same inputs, same number, always.

    ``days_since_anchor`` is the age of this day against every known
    episode, in days -- pulses and soaks together; an empty sequence means
    no qualifying rain at all.  ``lead_days`` is ``day - today``; a past day
    damps by nothing.

    ``api30_mm`` is this day's own API30: it decides whether the day may
    have a number at all, and it is the newest member of the moisture
    window.  ``api30_week`` are the days before it, newest first, already
    filtered to values the caller considers usable; leaving it empty judges
    the day on itself alone.

    ``frost_present`` only lowers the chance when frost was actually
    measured; an incomplete minimum-temperature history is not evidence of
    frost (the verdict still refuses ``high`` on it).  ``chmi_level`` and
    ``houbymapa_score`` must already be ``None`` when the map is stale --
    freshness is the caller's read model, not arithmetic.

    Without a usable ``api30_mm`` the day gets no number at all: see the
    module docstring for why a neutral value was worse than silence.
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

    # An empty ``reason`` already guarantees a number to stand on.
    moisture_mm = _moisture_mm(float(api30_mm), api30_week)  # type: ignore[arg-type]
    contributions: list[tuple[str, float, float]] = [
        ("moisture", policy.CHANCE_WEIGHT_MOISTURE, _moisture_term(moisture_mm)),
        ("phase", policy.CHANCE_WEIGHT_PHASE, _phase_term(days_since_anchor)),
    ]
    # The maps correct the place, so they apply to every day of the horizon;
    # with neither of them fresh the term drops out and the two remaining
    # weights are renormalised, rather than counting as a zero.
    map_term = _map_term(chmi_level, houbymapa_score)
    if map_term is not None:
        contributions.append(("map", policy.CHANCE_WEIGHT_MAP, map_term))

    total_weight = sum(weight for _, weight, _ in contributions)
    blend = (
        sum(weight * term for _, weight, term in contributions) / total_weight
        if total_weight > 0
        else 0.0
    )

    # What is left really does multiply: a failed temperature gate, frost
    # and the distance into the forecast suppress a number, they do not
    # vote on it.
    multipliers: list[tuple[str, float]] = []
    if not _temperature_ok(t_mean, t_min):
        multipliers.append(("temperature", policy.CHANCE_TEMPERATURE_FAILED))
    if frost_present:
        multipliers.append(("frost", policy.CHANCE_FROST))
    multipliers.append(
        ("horizon", _ramp(policy.CHANCE_HORIZON_DAMPING, float(lead_days)))
    )

    percent = policy.CHANCE_FULL_PCT * blend
    for _, factor in multipliers:
        percent *= factor
    raw_percent = percent

    factors: list[tuple[str, float]] = [
        (name, term) for name, _, term in contributions
    ]
    factors.append(("blend", blend))
    factors.extend(multipliers)

    capped = not station_available and percent > policy.CHANCE_NO_STATION_CAP
    if capped:
        percent = float(policy.CHANCE_NO_STATION_CAP)

    value = min(max(_round_to_step(percent), policy.CHANCE_MIN), policy.CHANCE_MAX)
    return DayChance(
        date=day,
        value=value,
        raw_percent=raw_percent,
        capped=capped,
        factors=tuple(factors),
        moisture_mm=moisture_mm,
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

    ``anchors`` are the anchor days of the known episodes -- the peaks of
    the rain pulses the verdict reasons about, plus every anchor day of a
    soak (``biology.SoakEpisode.anchors``), reduced to what the ramp needs.

    ``api30`` **may reach back before ``today``**, and should: the moisture
    term averages :data:`policy.CHANCE_MOISTURE_DAYS` days ending on the
    day judged, so the first week of the horizon is read partly off the
    days behind us.  Only ``days`` decides which days get a number, so the
    extra history never turns into extra rows; the caller is expected to
    have put only usable values in there, exactly as it does for the maps.
    """
    ordered = sorted({today, *(day for day in days if day >= today)})

    def week_before(day: date) -> list[float]:
        """The usable API30 of the days before ``day``, newest first."""
        return [
            value
            for offset in range(1, policy.CHANCE_MOISTURE_DAYS)
            if (value := api30.get(day - timedelta(days=offset))) is not None
        ]

    outlook = tuple(
        assess_day_chance(
            days_since_anchor=[(day - anchor).days for anchor in anchors],
            api30_mm=api30.get(day),
            api30_quality=api30_quality.get(day),
            api30_week=week_before(day),
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
