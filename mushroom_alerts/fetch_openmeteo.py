"""Open-Meteo daily forecast -> 16 days of precipitation and temperature.

One ``GET https://api.open-meteo.com/v1/forecast`` per location (two per
run with the default ``locations.yaml``, against a published limit of
10 000 requests/day -- PLAN §1).  The response, verified live 2026-09-07::

    {"latitude": 49.48, "longitude": 17.98, "generationtime_ms": 0.27,
     "utc_offset_seconds": 7200, "timezone": "Europe/Prague",
     "timezone_abbreviation": "GMT+2", "elevation": 309.0,
     "daily_units": {"time": "iso8601", "precipitation_sum": "mm",
                     "temperature_2m_mean": "°C", ...},
     "daily": {"time": ["2026-09-04", ...],
               "precipitation_sum": [0.0, 2.8, ...],
               "temperature_2m_mean": [...], "temperature_2m_min": [...],
               "temperature_2m_max": [...]}}

``latitude``/``longitude`` are the *grid cell* centre, not what we asked
for, and ``elevation`` is the model's terrain height there -- both go into
``meta`` so a suspicious value can be traced later.  ``past_days=3`` plus
``forecast_days=16`` yields 19 rows: three days before today, then today
and fifteen days ahead.

Observation vs forecast
-----------------------
Nothing here is a measurement.  Days **after today** are a forecast and
get ``meta["issued"]``, which is what ``base``/``store`` key on (the store
mirrors them into the ``forecasts`` table).  Days **up to and including
today** come from the model's own analysis of the recent past, not from a
gauge, so they get ``meta["provisional"] = True`` and no ``issued``: they
are good enough to plug the ~1 day lag of the ČHMÚ daily station files
(PLAN §6) but must never be treated as station data.

How different are they?  Measured on 2026-09-07 against station
``0-203-0-11769`` over the 62 days Open-Meteo still had (2026-07-07 ..
2026-09-06): daily MAE 2.90 mm, correlation 0.24 per day but 0.82 on 5-day
sums, and totals of **184.6 mm (Open-Meteo) vs 96.8 mm (station)** -- the
model runs about 1.9x wet at this point.  Individual convective days are
displaced by a day (2026-08-28 station 17.2 mm vs Open-Meteo 32.5 mm on
08-29).  So an API30 curve spliced onto this will read high; keep the
station as the authority for everything it covers (``api30.extend_series``
does exactly that) and treat the forecast tail as indicative.

``models``
----------
``models=best_match`` is Open-Meteo's default (ICON-D2/ICON-EU over
Czechia).  The response does **not** echo which model was selected -- with
a single ``models=`` value the ``daily`` keys stay unsuffixed and no model
field appears anywhere -- so ``meta["model"]`` records what we *asked*
for, not what the server resolved.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .base import FetchResult, Location, Reading

__all__ = [
    "SOURCE",
    "URL",
    "DAILY_VARIABLES",
    "METRICS",
    "PAST_DAYS",
    "FORECAST_DAYS",
    "TIMEZONE",
    "MODEL",
    "fetch",
    "params_for",
    "parse_daily",
    "series",
    "issued_days",
    "temperatures",
]

SOURCE = "openmeteo"
URL = "https://api.open-meteo.com/v1/forecast"

#: Requested daily variables, in the order Open-Meteo documents them.
DAILY_VARIABLES = (
    "precipitation_sum",
    "temperature_2m_mean",
    "temperature_2m_min",
    "temperature_2m_max",
)

#: Open-Meteo variable -> ``Reading.metric`` (see ``base`` for the table).
#: ``precip_mm`` deliberately differs from the station's ``sra_mm``: the
#: station measures 06-06 UTC in a gauge, this is a model's local-midnight
#: total, and mixing them under one name would hide the bias above.
METRICS = {
    "precipitation_sum": "precip_mm",
    "temperature_2m_mean": "t_mean",
    "temperature_2m_min": "t_min",
    "temperature_2m_max": "t_max",
}

PAST_DAYS = 3
FORECAST_DAYS = 16
TIMEZONE = "Europe/Prague"
MODEL = "best_match"


def params_for(
    location: Location,
    *,
    past_days: int = PAST_DAYS,
    forecast_days: int = FORECAST_DAYS,
) -> dict[str, Any]:
    """Query string for one location.  Kept pure so tests can pin it."""
    return {
        "latitude": location.lat,
        "longitude": location.lon,
        "daily": ",".join(DAILY_VARIABLES),
        "past_days": past_days,
        "forecast_days": forecast_days,
        "timezone": TIMEZONE,
        "models": MODEL,
    }


def parse_daily(payload: Any) -> tuple[list[date], dict[str, list[Any]]]:
    """Validate the payload; return the day list and the per-variable columns.

    Raises ``ValueError`` for anything that is not the documented shape --
    :func:`fetch` turns that into a soft failure.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    if payload.get("error"):
        raise ValueError(str(payload.get("reason") or "open-meteo reported an error"))
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise ValueError("payload has no 'daily' block")
    times = daily.get("time")
    if not isinstance(times, list) or not times:
        raise ValueError("payload has no 'daily.time'")
    days: list[date] = []
    for raw in times:
        try:
            days.append(date.fromisoformat(str(raw)[:10]))
        except ValueError as exc:
            raise ValueError(f"bad date {raw!r} in daily.time") from exc
    columns: dict[str, list[Any]] = {}
    for name in DAILY_VARIABLES:
        column = daily.get(name)
        if not isinstance(column, list) or len(column) != len(days):
            continue  # a missing variable is survivable; a wrong one is not
        columns[name] = column
    if "precipitation_sum" not in columns:
        raise ValueError("payload has no usable 'precipitation_sum'")
    return days, columns


def _readings_for_location(
    location: Location, payload: Any, today: date
) -> list[Reading]:
    days, columns = parse_daily(payload)
    base_meta: dict[str, Any] = {
        "model": MODEL,
        "grid": [payload.get("latitude"), payload.get("longitude")],
        "elevation": payload.get("elevation"),
        "timezone": payload.get("timezone"),
    }
    readings: list[Reading] = []
    for i, day in enumerate(days):
        meta = dict(base_meta)
        if day > today:
            # PLAN §3 trigger 4 / base.py: 'issued' is what makes it a forecast.
            meta["issued"] = today.isoformat()
            meta["horizon_days"] = (day - today).days
        else:
            # Model analysis of the recent past, not a gauge reading.
            meta["provisional"] = True
        for name, metric in METRICS.items():
            column = columns.get(name)
            if column is None:
                continue
            value = column[i]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue  # Open-Meteo sends null past its analysis horizon
            readings.append(
                Reading(
                    source=SOURCE,
                    location=location.slug,
                    date=day,
                    metric=metric,
                    value=float(value),
                    text=None,
                    meta=dict(meta),
                )
            )
    return readings


def fetch(locations: Iterable[Location], *, http: Any, today: date) -> FetchResult:
    """One request per location.  Never raises -- PLAN §6 / ``base`` contract."""
    locs = list(locations)
    readings: list[Reading] = []
    problems: list[str] = []
    for loc in locs:
        try:
            payload = http.get_json(URL, params_for(loc))
            readings.extend(_readings_for_location(loc, payload, today))
        except Exception as exc:  # noqa: BLE001 - soft failure, PLAN §6
            problems.append(f"{loc.slug}: {type(exc).__name__}: {exc}")
    error = "; ".join(problems) or None
    if not readings:
        return FetchResult.failure(SOURCE, error or "no locations resolved")
    return FetchResult.success(SOURCE, readings, error)


def series(
    readings: Iterable[Reading],
    location_slug: str,
    metric: str = "precip_mm",
) -> dict[date, float]:
    """``{day: value}`` for one location/metric -- the shape ``api30`` wants.

    Convenience for ``rules.py``: ``series(result.readings, "valmez")``
    gives the precipitation series to hand to
    ``api30.forecast_api30(..., forecast=...)``.
    """
    out: dict[date, float] = {}
    for r in readings:
        if r.source == SOURCE and r.location == location_slug and r.metric == metric:
            out[r.date] = float(r.value)
    return out


def issued_days(readings: Iterable[Reading]) -> set[date]:
    """Days among ``readings`` that are genuinely forecast (have ``issued``)."""
    return {r.date for r in readings if r.is_forecast}


def temperatures(
    readings: Iterable[Reading], location_slug: str
) -> dict[date, dict[str, float]]:
    """``{day: {"t_mean": .., "t_min": .., "t_max": ..}}`` for the temp gate."""
    out: dict[date, dict[str, float]] = {}
    for r in readings:
        if r.source != SOURCE or r.location != location_slug:
            continue
        if r.metric in ("t_mean", "t_min", "t_max"):
            out.setdefault(r.date, {})[r.metric] = float(r.value)
    return out
