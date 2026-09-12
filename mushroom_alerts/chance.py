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

    phase base × moisture × temperature × frost × HoubyMapa × ČHMÚ

Maps are today-only, exactly like the verdict's map gate: ČHMÚ and
HoubyMapa publish no forecast, so for a future day both multipliers are
left out (``map_relevant=False``) instead of freezing today's level over
the whole horizon.
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


@dataclass(frozen=True, slots=True)
class DayChance:
    """The chance for one day, plus what produced it."""

    date: date | None
    value: int
    raw_percent: float
    capped: bool
    factors: tuple[tuple[str, float], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "value": self.value,
            "raw_percent": round(self.raw_percent, 2),
            "capped": self.capped,
            "factors": [list(item) for item in self.factors],
        }


@dataclass(frozen=True, slots=True)
class ChanceOutlook:
    """Today's chance and the same calculation across the forecast."""

    current: DayChance
    outlook: tuple[DayChance, ...]

    @property
    def peak(self) -> DayChance | None:
        """The best day of the horizon; the earliest one wins a tie."""
        if not self.outlook:
            return None
        return min(self.outlook, key=lambda item: (-item.value, item.date or date.min))

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


def _quality(value: DataQuality | str | None) -> DataQuality:
    try:
        return value if isinstance(value, DataQuality) else DataQuality(str(value))
    except ValueError:
        return DataQuality.MISSING


def _moisture_factor(
    api30_mm: float | None, api30_quality: DataQuality | str | None
) -> float:
    """Band multiplier of :data:`policy.API30_BANDS_MM`.

    A missing number, or one from a stale/missing source, is not evidence
    of dryness -- it is no evidence at all, so the factor is neutral and
    the station cap (see :func:`assess_day_chance`) does the honest part.
    """
    if api30_mm is None or _quality(api30_quality) in {
        DataQuality.MISSING,
        DataQuality.STALE,
    }:
        return policy.CHANCE_MOISTURE_UNKNOWN
    index = sum(1 for edge in policy.API30_BANDS_MM if float(api30_mm) >= edge)
    return policy.CHANCE_MOISTURE_FACTORS[index]


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
    phase: str,
    api30_mm: float | None,
    api30_quality: DataQuality | str | None,
    t_mean: float | None,
    t_min: float | None,
    frost_present: bool,
    chmi_level: float | None,
    houbymapa_score: float | None,
    station_available: bool,
    map_relevant: bool = True,
    day: date | None = None,
) -> DayChance:
    """Evaluate one day.  Pure: same inputs, same number, always.

    ``frost_present`` only lowers the chance when frost was actually
    measured; an incomplete minimum-temperature history is not evidence of
    frost (the verdict still refuses ``high`` on it).  ``chmi_level`` and
    ``houbymapa_score`` must already be ``None`` when the map is stale --
    freshness is the caller's read model, not arithmetic.
    """
    factors: list[tuple[str, float]] = []
    base = policy.CHANCE_PHASE_BASE.get(phase, policy.CHANCE_PHASE_BASE["no_episode"])
    factors.append(("phase", base))

    factors.append(("moisture", _moisture_factor(api30_mm, api30_quality)))
    if not _temperature_ok(t_mean, t_min):
        factors.append(("temperature", policy.CHANCE_TEMPERATURE_FAILED))
    if frost_present:
        factors.append(("frost", policy.CHANCE_FROST))
    if map_relevant and houbymapa_score is not None:
        factors.append(
            (
                "houbymapa",
                policy.CHANCE_HOUBYMAPA_BASE
                + policy.CHANCE_HOUBYMAPA_SPAN * float(houbymapa_score),
            )
        )
    if map_relevant and chmi_level is not None:
        factors.append(
            (
                "chmi_map",
                policy.CHANCE_CHMI_BASE
                + policy.CHANCE_CHMI_STEP
                * (float(chmi_level) - policy.CHANCE_CHMI_PIVOT),
            )
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


def chance_for_day(**kwargs: Any) -> int:
    """The percentage alone -- see :func:`assess_day_chance` for the inputs."""
    return assess_day_chance(**kwargs).value


def assess_chance_horizon(
    today: date,
    phases: Mapping[date, str],
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
    """Today and every forecast day, with the maps counted for today only."""
    ordered = sorted({today, *(day for day in days if day >= today)})
    outlook = tuple(
        assess_day_chance(
            phase=phases.get(day, "no_episode"),
            api30_mm=api30.get(day),
            api30_quality=api30_quality.get(day),
            t_mean=t_mean.get(day),
            t_min=t_min.get(day),
            frost_present=frost_present,
            chmi_level=chmi_level,
            houbymapa_score=houbymapa_score,
            station_available=station_available,
            map_relevant=day == today,
            day=day,
        )
        for day in ordered
    )
    current = next(item for item in outlook if item.date == today)
    return ChanceOutlook(current, outlook)
