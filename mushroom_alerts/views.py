"""One coherent read model shared by rules, brief, and status."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from . import api30 as api30_lib
from .base import DataQuality, FetchResult, Location, Reading
from .fetch_chmi_map import LEVEL_LABELS
from .quality import calendar_window, point_from_reading, quality_worst
from .store import Store

CHMI_MAP = "chmi_map"
HOUBYMAPA = "houbymapa"
STATION = "chmi_station"
OPENMETEO = "openmeteo"
API30_FORECAST = api30_lib.FORECAST_SOURCE
TZ = ZoneInfo("Europe/Prague")

__all__ = ["location_snapshot", "forecast_bundle", "source_status"]


def _apply_run_status(
    statuses: dict[str, dict[str, Any]],
    results: Sequence[FetchResult],
    slug: str,
) -> None:
    """Overlay failures from this fetch pass on top of stored-state quality."""
    for result in results:
        current = dict(statuses.get(result.source) or {})
        error = result.location_errors.get(slug)
        rows = result.for_location(slug)
        if not result.ok:
            current.update(quality=DataQuality.MISSING.value, error=result.error)
        elif error:
            current.update(
                quality=(DataQuality.PARTIAL if rows else DataQuality.MISSING).value,
                error=error,
            )
        elif rows:
            current["current_run_ok"] = True
        else:
            current.update(quality=DataQuality.MISSING.value, error="no data in current run")
        statuses[result.source] = current


def _quality(readings: Iterable[Reading], today: date) -> DataQuality:
    points = [point_from_reading(reading, today) for reading in readings]
    return quality_worst(*(point.quality for point in points)) if points else DataQuality.MISSING


def source_status(
    source: str, readings: Iterable[Reading], today: date
) -> dict[str, Any]:
    rows = list(readings)
    if not rows:
        return {"quality": DataQuality.MISSING.value, "valid_at": None, "issued_at": None}
    quality = _quality(rows, today)
    newest = max(rows, key=lambda reading: reading.date)
    if source == STATION and newest.date < today - timedelta(days=1):
        quality = DataQuality.STALE
    point = point_from_reading(newest, today)
    return {
        "quality": quality.value,
        "valid_at": None if point.valid_at is None else point.valid_at.isoformat(),
        "issued_at": None if point.issued_at is None else point.issued_at.isoformat(),
        "date": newest.date,
        "covered": point.covered,
        "expected": point.expected,
    }


def _run_quality(run: Any, today: date) -> DataQuality:
    if run is None:
        return DataQuality.MISSING
    try:
        retrieved = datetime.fromisoformat(str(run["retrieved_at"]).replace("Z", "+00:00"))
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=TZ)
        return DataQuality.FRESH if retrieved.astimezone(TZ).date() == today else DataQuality.STALE
    except (TypeError, ValueError):
        return DataQuality.STALE


def _curve(
    store: Store, run_id: str | None, slug: str, metric: str
) -> dict[date, float]:
    if not run_id:
        return {}
    return {
        date.fromisoformat(row["target_date"]): float(row["value"])
        for row in store.forecast_run_points(run_id, location=slug, metric=metric)
    }


def _point_qualities(store: Store, run_id: str | None, slug: str) -> dict[date, DataQuality]:
    if not run_id:
        return {}
    qualities: dict[date, DataQuality] = {}
    for row in store.forecast_run_points(run_id, location=slug):
        try:
            meta = json.loads(row["meta_json"] or "{}")
            quality = DataQuality(str(meta.get("quality", "fresh")))
        except (json.JSONDecodeError, TypeError, ValueError):
            quality = DataQuality.FRESH
        target = date.fromisoformat(row["target_date"])
        previous = qualities.get(target)
        qualities[target] = quality if previous is None else quality_worst(previous, quality)
    return qualities


def _input_openmeteo_run(store: Store, api_run: Any, slug: str) -> Any:
    if api_run is None:
        return None
    try:
        candidates = json.loads(api_run["input_run_ids_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        candidates = []
    for run_id in candidates:
        run = store.get_forecast_run(str(run_id))
        if run is None or run["source"] != OPENMETEO:
            continue
        if store.forecast_run_points(str(run_id), location=slug):
            return run
    return None


def forecast_bundle(
    store: Store,
    slug: str,
    today: date,
    *,
    threshold: float | None = None,
    next_rain_mm: float = 5.0,
) -> dict[str, Any]:
    """Return coherent Open-Meteo and API30 runs for one location."""
    threshold = api30_lib.threshold_mm() if threshold is None else threshold
    api_run = store.latest_forecast_run(API30_FORECAST, slug, api30_lib.METRIC)
    open_run = _input_openmeteo_run(store, api_run, slug)
    if open_run is None:
        open_run = store.latest_forecast_run(OPENMETEO, slug)

    api_run_id = None if api_run is None else str(api_run["run_id"])
    open_run_id = None if open_run is None else str(open_run["run_id"])
    api_curve = _curve(store, api_run_id, slug, api30_lib.METRIC)
    rain = _curve(store, open_run_id, slug, "precip_mm")
    t_mean = _curve(store, open_run_id, slug, "t_mean")
    t_min = _curve(store, open_run_id, slug, "t_min")
    curve = sorted(api_curve.items())
    cross = api30_lib.crossing(curve, threshold, today=today) if curve else None
    peak = max(curve, key=lambda pair: (pair[1], pair[0])) if curve else None
    next_rain = next(
        ((day, rain[day]) for day in sorted(rain) if day > today and rain[day] >= next_rain_mm),
        None,
    )
    api_point_qualities = _point_qualities(store, api_run_id, slug)
    api_today_quality = api_point_qualities.get(today, DataQuality.MISSING)
    api_quality = quality_worst(_run_quality(api_run, today), api_today_quality)
    open_quality = _run_quality(open_run, today)
    qualities = (open_quality, api_quality)
    quality = quality_worst(*qualities)
    return {
        "quality": quality.value,
        "openmeteo_quality": qualities[0].value,
        "api30_quality": qualities[1].value,
        "api30_quality_by_date": {
            day: point_quality.value for day, point_quality in api_point_qualities.items()
        },
        "cross_quality": None if cross is None else api_point_qualities.get(cross, DataQuality.MISSING).value,
        "openmeteo_run_id": open_run_id,
        "api30_run_id": api_run_id,
        "openmeteo_retrieved_at": None if open_run is None else open_run["retrieved_at"],
        "api30_retrieved_at": None if api_run is None else api_run["retrieved_at"],
        "issued": None if api_run is None else date.fromisoformat(str(api_run["retrieved_at"])[:10]),
        "today_mm": api_curve.get(today),
        "peak": peak,
        "cross": cross,
        "threshold_mm": threshold,
        "next_rain": next_rain,
        "curve": curve,
        "rain": rain,
        "t_mean": t_mean,
        "t_min": t_min,
    }


def location_snapshot(
    store: Store,
    location: Location,
    today: date,
    *,
    history_days: int = 45,
    rain_days: int = 3,
    next_rain_mm: float = 5.0,
    results: Sequence[FetchResult] = (),
) -> dict[str, Any]:
    """Current observations and a coherent forecast view for a location."""
    slug = location.slug
    since = today - timedelta(days=history_days)
    out: dict[str, Any] = {
        "name": location.name,
        "slug": slug,
        "chmi": None,
        "houbymapa": None,
        "station": None,
        "forecast": None,
        "source_status": {},
    }

    level = store.latest(CHMI_MAP, slug, "level")
    chmi_rows = [] if level is None else [level]
    out["source_status"][CHMI_MAP] = source_status(CHMI_MAP, chmi_rows, today)
    if level is not None:
        previous = store.latest(CHMI_MAP, slug, "level", before=level.date)
        out["chmi"] = {
            "level": float(level.value),
            "date": level.date,
            "label": (level.meta or {}).get("label") or LEVEL_LABELS.get(int(level.value)),
            "stale": out["source_status"][CHMI_MAP]["quality"] == DataQuality.STALE.value,
            "quality": out["source_status"][CHMI_MAP]["quality"],
            "previous": None if previous is None else float(previous.value),
        }

    h_level = store.latest(HOUBYMAPA, slug, "level")
    h_score = store.latest(HOUBYMAPA, slug, "score")
    houby_rows = [row for row in (h_level, h_score) if row is not None]
    out["source_status"][HOUBYMAPA] = source_status(HOUBYMAPA, houby_rows, today)
    if houby_rows:
        newest = max(row.date for row in houby_rows)
        out["houbymapa"] = {
            "level": None if h_level is None else float(h_level.value),
            "score": None if h_score is None else float(h_score.value),
            "date": newest,
            "label": None if h_level is None else LEVEL_LABELS.get(int(h_level.value)),
            "stale": out["source_status"][HOUBYMAPA]["quality"] == DataQuality.STALE.value,
            "quality": out["source_status"][HOUBYMAPA]["quality"],
        }

    sra_rows = store.series(STATION, slug, "sra_mm", since=since, until=today)
    t_rows = store.series(STATION, slug, "t_mean", since=since, until=today)
    sra = {row.date: float(row.value) for row in sra_rows}
    t_mean = {row.date: float(row.value) for row in t_rows}
    api = store.latest(STATION, slug, "api30_mm")
    status_rows = list(sra_rows)
    if api is not None:
        status_rows.append(api)
    out["source_status"][STATION] = source_status(STATION, status_rows, today)
    if sra or api is not None:
        sra_points = {row.date: point_from_reading(row, today) for row in sra_rows}
        t_points = {row.date: point_from_reading(row, today) for row in t_rows}
        rain_window = calendar_window(sra_points, today, rain_days)
        newest_sra = max(sra_rows, key=lambda row: row.date) if sra_rows else None
        last_t = max(t_mean) if t_mean else None
        out["station"] = {
            "api30_mm": None if api is None else float(api.value),
            "api30_date": None if api is None else api.date,
            "sra_window_mm": None if rain_window.total is None else round(rain_window.total, 1),
            "sra_window_days": rain_window.covered_days,
            "sra_window_expected_days": rain_window.expected_days,
            "sra_window_quality": rain_window.quality.value,
            "sra_window_lower_bound": rain_window.lower_bound,
            "sra_last_date": max(sra) if sra else None,
            "sra_until": None if newest_sra is None else (newest_sra.meta or {}).get("until"),
            "sra_covered_slots": None if newest_sra is None else (newest_sra.meta or {}).get("covered_slots"),
            "sra_expected_slots": None if newest_sra is None else (newest_sra.meta or {}).get("expected_slots"),
            "t_mean": None if last_t is None else t_mean[last_t],
            "t_mean_date": last_t,
            "station": None if newest_sra is None else (newest_sra.meta or {}).get("station"),
            "quality": out["source_status"][STATION]["quality"],
            "series": {"sra_mm": sra, "t_mean": t_mean},
            "series_points": {"sra_mm": sra_points, "t_mean": t_points},
        }

    forecast = forecast_bundle(store, slug, today, next_rain_mm=next_rain_mm)
    out["source_status"][OPENMETEO] = {
        "quality": forecast["openmeteo_quality"],
        "run_id": forecast["openmeteo_run_id"],
        "retrieved_at": forecast["openmeteo_retrieved_at"],
    }
    out["source_status"][API30_FORECAST] = {
        "quality": forecast["api30_quality"],
        "run_id": forecast["api30_run_id"],
        "retrieved_at": forecast["api30_retrieved_at"],
    }
    if forecast["openmeteo_run_id"] or forecast["api30_run_id"]:
        out["forecast"] = forecast
    _apply_run_status(out["source_status"], results, slug)
    return out
