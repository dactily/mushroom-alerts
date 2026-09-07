"""Normalized data quality and exact calendar-window aggregation."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Mapping

from .base import DataQuality, Reading, SeriesPoint, WindowAggregate

__all__ = ["point_from_reading", "calendar_window", "quality_worst"]

_RANK = {
    DataQuality.FRESH: 0,
    DataQuality.PARTIAL: 1,
    DataQuality.STALE: 2,
    DataQuality.MISSING: 3,
}


def quality_worst(*values: DataQuality) -> DataQuality:
    return max(values or (DataQuality.MISSING,), key=_RANK.__getitem__)


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def point_from_reading(reading: Reading, today: date) -> SeriesPoint:
    """Apply source-specific freshness rules to one stored reading."""
    meta = reading.meta or {}
    issued_at = _parse_datetime(meta.get("issued_at") or reading.issued)
    quality = DataQuality.FRESH
    if meta.get("stale"):
        quality = DataQuality.STALE
    elif (
        reading.source == "chmi_station"
        and meta.get("provisional")
        and not meta.get("complete")
    ):
        quality = DataQuality.PARTIAL
    elif reading.source in {"openmeteo", "api30_forecast"}:
        if issued_at is not None and issued_at.date() < today:
            quality = DataQuality.STALE

    valid_at = _parse_datetime(meta.get("until") or meta.get("valid_at"))
    if valid_at is None:
        valid_at = datetime.combine(reading.date, time.min, tzinfo=timezone.utc)
    return SeriesPoint(
        date=reading.date,
        value=float(reading.value),
        source=reading.source,
        quality=quality,
        valid_at=valid_at,
        issued_at=issued_at,
        covered=_as_int(meta.get("covered_slots")),
        expected=_as_int(meta.get("expected_slots")),
    )


def calendar_window(
    values: Mapping[date, float | SeriesPoint], end: date, days: int
) -> WindowAggregate:
    """Aggregate exactly ``days`` calendar dates ending at ``end``."""
    if days <= 0:
        raise ValueError("days must be positive")
    start = end - timedelta(days=days - 1)
    found: list[float] = []
    qualities: list[DataQuality] = []
    for offset in range(days):
        raw = values.get(start + timedelta(days=offset))
        if raw is None:
            continue
        if isinstance(raw, SeriesPoint):
            if raw.value is None:
                continue
            found.append(float(raw.value))
            qualities.append(raw.quality)
        else:
            found.append(float(raw))
            qualities.append(DataQuality.FRESH)

    covered = len(found)
    if not found:
        quality = DataQuality.MISSING
    else:
        quality = quality_worst(*qualities)
        if covered < days and quality is DataQuality.FRESH:
            quality = DataQuality.PARTIAL
    return WindowAggregate(
        start=start,
        end=end,
        total=round(sum(found), 4) if found else None,
        mean=(sum(found) / covered) if found else None,
        covered_days=covered,
        expected_days=days,
        quality=quality,
        lower_bound=covered < days,
    )
