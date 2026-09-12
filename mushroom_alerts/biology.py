"""Pure biological timing model built from normalized daily series.

Rainfall is stored as daily observations.  This module derives repeatable
events from that history, merges overlapping rolling windows from the same
wet spell, and evaluates every relevant event for a requested day.  A newer
rain must not hide an older window that is already active.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from typing import Any, Mapping, Sequence

from . import policy
from .base import DataQuality, SeriesPoint
from .quality import calendar_window

__all__ = [
    "RainEpisode",
    "DayAssessment",
    "BiologicalAssessment",
    "detect_rain_episodes",
    "assess_day",
    "assess_horizon",
]


@dataclass(frozen=True, slots=True)
class RainEpisode:
    """One wet spell, possibly formed by several qualifying rolling windows."""

    event_id: str
    location: str
    start: date
    end: date
    anchor: date
    total_mm: float
    covered_days: int
    expected_days: int
    quality: DataQuality
    lower_bound: bool
    temperature_mean_c: float
    temperature_covered_days: int

    @property
    def primary_start(self) -> date:
        return self.anchor + timedelta(days=policy.GROWTH_WINDOW_FROM_DAYS)

    @property
    def primary_end(self) -> date:
        return self.anchor + timedelta(days=policy.GROWTH_WINDOW_TO_DAYS)

    @property
    def residual_end(self) -> date:
        return self.anchor + timedelta(days=policy.GROWTH_RESIDUAL_TO_DAYS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "start": self.start,
            "end": self.end,
            "date": self.anchor,
            "total_mm": self.total_mm,
            "covered_days": self.covered_days,
            "expected_days": self.expected_days,
            "quality": self.quality.value,
            "lower_bound": self.lower_bound,
            "temperature_mean_c": self.temperature_mean_c,
            "temperature_covered_days": self.temperature_covered_days,
            "growth_window": [self.primary_start, self.primary_end],
            "residual_window_end": self.residual_end,
        }


@dataclass(frozen=True, slots=True)
class DayAssessment:
    """Conservative categorical assessment for one calendar day."""

    date: date
    verdict: str
    phase: str
    phase_text: str
    dominant_event_id: str | None
    active_event_ids: tuple[str, ...]
    upcoming_event_ids: tuple[str, ...]
    high_blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "verdict": self.verdict,
            "verdict_label": policy.BIOLOGICAL_VERDICT_LABELS[self.verdict],
            "phase": self.phase,
            "phase_text": self.phase_text,
            "dominant_event_id": self.dominant_event_id,
            "active_event_ids": list(self.active_event_ids),
            "upcoming_event_ids": list(self.upcoming_event_ids),
            "high_blockers": list(self.high_blockers),
        }


@dataclass(frozen=True, slots=True)
class BiologicalAssessment:
    """Today's assessment plus the same calculation across the forecast."""

    current: DayAssessment
    candidate_high_date: date | None
    outlook: tuple[DayAssessment, ...]

    def as_dict(self) -> dict[str, Any]:
        current = self.current.as_dict()
        current.pop("date", None)
        current["scope"] = "site_conditions_not_observed_fruit_bodies"
        current["scope_text"] = (
            "оценка условий участка, не подтверждение отдельных плодовых тел"
        )
        current["candidate_high_date"] = self.candidate_high_date
        current["outlook"] = [item.as_dict() for item in self.outlook]
        return current


@dataclass(frozen=True, slots=True)
class _QualifiedWindow:
    start: date
    end: date


def _value(raw: float | SeriesPoint | None) -> float | None:
    """Numeric value of a series entry, or ``None`` when it carries none.

    ``calendar_window`` silently skips empty points, so the anchor search has
    to skip them the same way instead of raising and killing the whole brief.
    """
    if raw is None:
        return None
    if isinstance(raw, SeriesPoint):
        return None if raw.value is None else float(raw.value)
    return float(raw)


def _event_id(location: str, start: date) -> str:
    material = f"{location}\0{start.isoformat()}".encode()
    return "rain-" + sha256(material).hexdigest()[:16]


def _episode_from_group(
    location: str,
    windows: Sequence[_QualifiedWindow],
    rain_points: Mapping[date, float | SeriesPoint],
    temperature_points: Mapping[date, float | SeriesPoint],
) -> RainEpisode | None:
    start = min(item.start for item in windows)
    end = max(item.end for item in windows)
    days = (end - start).days + 1
    rain = calendar_window(rain_points, end, days)
    temperature = calendar_window(temperature_points, end, days)
    candidates = [
        (day, value)
        for day, raw in rain_points.items()
        if start <= day <= end and (value := _value(raw)) is not None
    ]
    if not candidates or rain.total is None or temperature.mean is None:
        return None
    anchor = max(candidates, key=lambda item: (item[1], item[0]))[0]
    return RainEpisode(
        event_id=_event_id(location, start),
        location=location,
        start=start,
        end=end,
        anchor=anchor,
        total_mm=round(rain.total, 1),
        covered_days=rain.covered_days,
        expected_days=rain.expected_days,
        quality=rain.quality,
        lower_bound=rain.lower_bound,
        temperature_mean_c=round(temperature.mean, 1),
        temperature_covered_days=temperature.covered_days,
    )


def detect_rain_episodes(
    location: str,
    rain_points: Mapping[date, float | SeriesPoint],
    temperature_points: Mapping[date, float | SeriesPoint],
    today: date,
) -> tuple[RainEpisode, ...]:
    """Return all qualifying wet spells available in the supplied history."""
    qualified: list[_QualifiedWindow] = []
    for end in sorted(day for day in rain_points if day <= today):
        rain = calendar_window(rain_points, end, policy.RAIN_EPISODE_DAYS)
        temperature = calendar_window(
            temperature_points, end, policy.RAIN_EPISODE_DAYS
        )
        if rain.total is None or rain.total < policy.RAIN_EPISODE_MM:
            continue
        if rain.quality in {DataQuality.MISSING, DataQuality.STALE}:
            continue
        if not temperature.complete or temperature.mean is None:
            continue
        if not (
            policy.RAIN_T_MEAN_MIN
            <= temperature.mean
            <= policy.RAIN_T_MEAN_MAX
        ):
            continue
        qualified.append(_QualifiedWindow(rain.start, rain.end))

    if not qualified:
        return ()
    groups: list[list[_QualifiedWindow]] = []
    for window in qualified:
        if not groups or window.start > groups[-1][-1].end + timedelta(days=1):
            groups.append([window])
        else:
            groups[-1].append(window)
    built = (
        _episode_from_group(location, group, rain_points, temperature_points)
        for group in groups
    )
    return tuple(episode for episode in built if episode is not None)


def _phase(episode: RainEpisode, day: date) -> str:
    if day < episode.primary_start:
        return "waiting"
    if day <= episode.primary_end:
        return "primary_window"
    if day <= episode.residual_end:
        return "residual_window"
    return "expired"


_PHASE_RANK = {
    "expired": 0,
    "waiting": 1,
    "residual_window": 2,
    "primary_window": 3,
}


def _dominant(
    episodes: Sequence[RainEpisode], day: date
) -> tuple[RainEpisode | None, str]:
    if not episodes:
        return None, "no_episode"
    episode = max(
        episodes,
        key=lambda item: (_PHASE_RANK[_phase(item, day)], item.anchor, item.end),
    )
    return episode, _phase(episode, day)


def _phase_text(episode: RainEpisode | None, phase: str) -> str:
    if episode is None:
        return "подтверждённый дождевой эпизод не найден"
    if phase == "waiting":
        return (
            f"дождь прошёл {episode.anchor.isoformat()}, ждём роста с "
            f"{episode.primary_start.isoformat()}"
        )
    if phase == "primary_window":
        return (
            f"основное расчётное окно {episode.primary_start.isoformat()}–"
            f"{episode.primary_end.isoformat()}; наличие грибов требует подтверждения"
        )
    if phase == "residual_window":
        return (
            f"остаточная вероятность после основного окна сохраняется до "
            f"{episode.residual_end.isoformat()}"
        )
    return f"расчётное окно закончилось {episode.residual_end.isoformat()}"


def _quality(value: DataQuality | str | None) -> DataQuality:
    try:
        return value if isinstance(value, DataQuality) else DataQuality(str(value))
    except ValueError:
        return DataQuality.MISSING


def _temperature_ok(t_mean: float | None, t_min: float | None) -> bool:
    if t_mean is None or t_min is None:
        return False
    return (
        policy.API30_T_MEAN_MIN <= float(t_mean) <= policy.API30_T_MEAN_MAX
        and float(t_min) > policy.API30_T_MIN_ABOVE
    )


def assess_day(
    day: date,
    episodes: Sequence[RainEpisode],
    *,
    api30_mm: float | None,
    api30_quality: DataQuality | str | None,
    t_mean: float | None,
    t_min: float | None,
    forecast_fresh: bool,
    history_sufficient: bool,
    frost_known: bool,
    frost_present: bool,
    map_high: bool,
    usable_input: bool,
    threshold_mm: float,
    map_relevant: bool = True,
) -> DayAssessment:
    """Evaluate one day, taking the strongest phase across all episodes.

    ``map_high`` describes the maps as published *today*.  That is evidence
    about today only: ČHMÚ and HoubyMapa publish no forecast, so a level that
    happens to be 3 this morning says nothing about a day a week out.  Callers
    pass ``map_relevant=False`` for future days, where the map neither blocks
    a high verdict nor props up a medium one.
    """
    dominant, phase = _dominant(episodes, day)
    active = tuple(
        item.event_id
        for item in episodes
        if _phase(item, day) in {"primary_window", "residual_window"}
    )
    upcoming = tuple(
        item.event_id for item in episodes if _phase(item, day) == "waiting"
    )
    blockers: list[str] = []
    if phase == "no_episode":
        blockers.append("no_qualified_rain_episode")
    elif phase == "waiting":
        blockers.append("growth_window_not_started")
    elif phase == "residual_window":
        blockers.append("primary_window_finished_residual_active")
    elif phase == "expired":
        blockers.append("growth_window_finished")

    point_quality = _quality(api30_quality)
    if point_quality is not DataQuality.FRESH:
        blockers.append("api30_not_fresh")
    elif api30_mm is None or float(api30_mm) < threshold_mm:
        blockers.append("api30_below_threshold")
    if not forecast_fresh:
        blockers.append("forecast_not_fresh")
    elif not _temperature_ok(t_mean, t_min):
        blockers.append("temperature_gate_failed")
    if not history_sufficient:
        blockers.append("history_insufficient")
    if not frost_known or frost_present:
        blockers.append("frost_or_incomplete_frost_history")
    map_support = map_high and map_relevant
    if map_relevant and not map_high:
        blockers.append("no_fresh_high_map_support")

    if not blockers:
        verdict = "high"
    else:
        event_support = phase in {
            "waiting",
            "primary_window",
            "residual_window",
        }
        if phase in {"primary_window", "residual_window"}:
            if frost_known and frost_present:
                event_support = False
            if (
                point_quality is DataQuality.FRESH
                and api30_mm is not None
                and float(api30_mm) < policy.API30_BANDS_MM[0]
            ):
                event_support = False
        if event_support or map_support:
            verdict = "medium"
        else:
            verdict = "low" if usable_input else "insufficient"

    return DayAssessment(
        date=day,
        verdict=verdict,
        phase=phase,
        phase_text=_phase_text(dominant, phase),
        dominant_event_id=None if dominant is None else dominant.event_id,
        active_event_ids=active,
        upcoming_event_ids=upcoming,
        high_blockers=tuple(blockers),
    )


def assess_horizon(
    today: date,
    episodes: Sequence[RainEpisode],
    days: Sequence[date],
    *,
    api30: Mapping[date, float],
    api30_quality: Mapping[date, DataQuality | str],
    t_mean: Mapping[date, float],
    t_min: Mapping[date, float],
    forecast_fresh: bool,
    history_sufficient: bool,
    frost_known: bool,
    frost_present: bool,
    map_high: bool,
    usable_input: bool,
    threshold_mm: float,
) -> BiologicalAssessment:
    """Evaluate today and every supplied forecast day with identical rules."""
    ordered = sorted({today, *(day for day in days if day >= today)})
    outlook = tuple(
        assess_day(
            day,
            episodes,
            api30_mm=api30.get(day),
            api30_quality=api30_quality.get(day),
            t_mean=t_mean.get(day),
            t_min=t_min.get(day),
            forecast_fresh=forecast_fresh,
            history_sufficient=history_sufficient,
            frost_known=frost_known,
            frost_present=frost_present,
            map_high=map_high,
            usable_input=usable_input,
            threshold_mm=threshold_mm,
            map_relevant=day == today,
        )
        for day in ordered
    )
    current = next(item for item in outlook if item.date == today)
    candidate = next((item.date for item in outlook if item.verdict == "high"), None)
    return BiologicalAssessment(current, candidate, outlook)
