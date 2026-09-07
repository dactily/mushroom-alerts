"""API30 -- the antecedent precipitation index ČHMÚ publishes, plus a
forecast extension of it.

This module is **pure computation**: no HTTP, no store, and deliberately
**no** ``fetch`` function and **no** ``SOURCE`` attribute, so that
``__main__.discover_fetchers`` does not mistake it for a fetcher.  The
wiring in ``rules.py`` calls it explicitly after the fetchers have run.

Why it exists
-------------
ČHMÚ publishes ``API30`` per station but only for days that already
happened.  Feeding the same recursion with Open-Meteo's 16-day
precipitation forecast produces an API30 *curve into the future*, which is
the one thing none of the upstream sources offers -- PLAN §3 trigger 4.

The formula (verified against live ČHMÚ data)
---------------------------------------------
PLAN §5 states ``API30(t) = Σ_{j=1..30} SRA(t−j) · 0.92^(j−1)`` with a
claimed mean error of 0.87 mm.  That is close but not what ČHMÚ actually
computes.  Refitting against the published ``API30`` element of station
Valašské Meziříčí (``0-203-0-11769``) over 341 days, 2025-10-01 .. 2026-09-06::

    Σ_{j=1..30} SRA(t−j)·0.93^j        MAE 0.025 mm, max 0.05 mm   <- exact
    Σ_{j=1..30} SRA(t−j)·0.92^(j−1)    MAE 0.655 mm, max 2.29 mm   (PLAN)
    Σ_{j=0..29} SRA(t−j)·0.93^j        MAE 1.55 mm,  max 17.9 mm

0.05 mm is exactly the rounding of the published values (one decimal), and
a scan of ``decay`` over 0.900..0.959 in steps of 0.001 puts the minimum
squarely on 0.930, and of the window length on 30.  So the reproduction is
exact: the decay is **0.93**, the window is the 30 days *before* ``t``
(day ``t`` itself is excluded -- the station reads the gauge at 06:00 UTC,
so ``SRA(t)`` is not known when ``API30(t)`` is stamped), and the weight of
lag ``j`` is ``0.93**j``, i.e. even the most recent day is already damped
once.

Both knobs stay configurable; :data:`PLAN_DECAY` / :data:`PLAN_LAG_OFFSET`
reproduce the PLAN variant for comparison.

Splicing observations with a forecast
-------------------------------------
:func:`extend_series` merges a station SRA series with an Open-Meteo one:
the station wins wherever it has a value, the forecast fills yesterday /
today (the daily station file lags ~1 day) and every day ahead.

The two are not the same quantity, in two ways, and both are deliberate
approximations:

* **Different day windows.**  Station ``SRA`` dated *D* covers
  ``[D 06:00Z, D+1 06:00Z)``; Open-Meteo ``precip_mm`` dated *D* is the
  calendar day *D* in Europe/Prague.  They overlap by 18 h.  The merge maps
  Open-Meteo day *D* onto station day *D* with **no shift** -- the
  alternative (splitting each model day between two station days) would
  invent sub-daily structure the daily API has never had, and the residual
  error is far smaller than the next point.
* **Different magnitude.**  Measured 2026-09-07 over 62 days at Valašské
  Meziříčí, Open-Meteo totals 184.6 mm against the gauge's 96.8 mm -- about
  1.9x wet, with per-day correlation 0.24 (0.82 on 5-day blocks).

So the extended curve is a projection, not a measurement: keep the station
authoritative for every day it covers (which is what this function does)
and read the tail as indicative.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping

from .base import Reading

__all__ = [
    "DECAY",
    "WINDOW",
    "LAG_OFFSET",
    "MAX_GAPS",
    "DEFAULT_THRESHOLD_MM",
    "THRESHOLD_ENV",
    "threshold_mm",
    "PLAN_DECAY",
    "PLAN_LAG_OFFSET",
    "FORECAST_SOURCE",
    "METRIC",
    "T_MEAN_MIN",
    "T_MEAN_MAX",
    "T_MIN_ABOVE",
    "Api30",
    "MergedSeries",
    "weights",
    "api30",
    "api30_detail",
    "extend_series",
    "merge_series",
    "forecast_api30",
    "forecast_api30_details",
    "crossing",
    "to_readings",
    "temp_ok",
]

#: Daily decay factor, refitted against the published ČHMÚ values.
DECAY = 0.93

#: How many days before ``t`` contribute.
WINDOW = 30

#: Weight of lag ``j`` is ``DECAY ** (j - LAG_OFFSET)``.  ČHMÚ uses 0 (so
#: yesterday already carries ``DECAY``); PLAN §5 assumed 1.
LAG_OFFSET = 0

#: More than this many missing days inside the window -> refuse to guess.
MAX_GAPS = 3

#: Provisional trigger threshold, PLAN §3 trigger 4.
#:
#: **PLAN's 40 mm was wrong and is gone.**  PLAN §3 justified it with
#: "API30 = 41 mm on 2026-09-07 corresponds to ČHMÚ level 3/5", but the
#: station actually published API30 = 20.3 mm for 2026-09-06 at Valašské
#: Meziříčí (17.0 mm at Valašská Bystřice) on the very day the ČHMÚ map
#: showed 3/5 for both.  The PLAN number is roughly double the real one, so
#: a 40 mm gate would have stayed shut through the whole 2026 season
#: measured here (the live 16-day curve of 2026-09-07 peaks at 26.9 mm).
#:
#: 25 mm is a provisional value: a bit above the 20 mm that scored 3/5, low
#: enough for the forecast curve to reach it.  Calibration against the ČHMÚ
#: map levels accumulating in SQLite is still pending (PLAN §3/§5): pick the
#: threshold that best separates days with level >= 4 from days with <= 3.
#: Override without editing code via ``$MUSHROOM_API30_THRESHOLD``.
DEFAULT_THRESHOLD_MM = 25.0

#: Environment variable that overrides :data:`DEFAULT_THRESHOLD_MM`.
THRESHOLD_ENV = "MUSHROOM_API30_THRESHOLD"

#: The PLAN §5 variant, kept so the difference can be reproduced in tests.
PLAN_DECAY = 0.92
PLAN_LAG_OFFSET = 1

#: ``Reading.source`` / ``Reading.metric`` used by :func:`to_readings`.
FORECAST_SOURCE = "api30_forecast"
METRIC = "api30_mm"

#: Temperature gate of PLAN §3 trigger 4.
T_MEAN_MIN = 8.0
T_MEAN_MAX = 22.0
T_MIN_ABOVE = 2.0


def threshold_mm(default: float | None = None) -> float:
    """The API30 trigger threshold: ``$MUSHROOM_API30_THRESHOLD`` or default.

    Junk in the environment is ignored rather than fatal -- a typo in a cron
    line must not silence the alerts.
    """
    raw = os.environ.get(THRESHOLD_ENV)
    fallback = DEFAULT_THRESHOLD_MM if default is None else float(default)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = float(raw.strip())
    except ValueError:
        return fallback
    return value if value > 0 else fallback


@dataclass(frozen=True, slots=True)
class Api30:
    """Result of one API30 evaluation, with provenance."""

    date: date
    value: float
    gaps: int
    """Days inside the window with no precipitation value (counted as 0)."""
    missing: tuple[date, ...] = ()
    forecast_days: int = 0
    """How many days of the window came from a forecast, if known."""
    model_weight_fraction: float = 0.0
    """Share of the available recency weights backed by model values."""

    def __float__(self) -> float:  # convenience for arithmetic/tests
        return self.value


@dataclass(frozen=True, slots=True)
class MergedSeries:
    """Values plus the dates filled by the model."""

    values: dict[date, float]
    forecast_days: frozenset[date]


def weights(
    *, window: int = WINDOW, decay: float = DECAY, lag_offset: int = LAG_OFFSET
) -> list[float]:
    """The weight applied to lag 1, 2, ... ``window`` (for inspection)."""
    return [decay ** (j - lag_offset) for j in range(1, window + 1)]


def api30_detail(
    series: Mapping[date, float],
    day: date,
    *,
    window: int = WINDOW,
    decay: float = DECAY,
    lag_offset: int = LAG_OFFSET,
    max_gaps: int = MAX_GAPS,
    forecast_days: Iterable[date] | None = None,
) -> Api30 | None:
    """``Σ_{j=1..window} series[day−j] · decay**(j−lag_offset)``.

    Missing days are treated as 0 mm but counted; more than ``max_gaps`` of
    them and the answer is ``None`` (we would be inventing a dry spell).
    ``None`` values in ``series`` count as gaps too.
    """
    forecast = set(forecast_days or ())
    total = 0.0
    missing: list[date] = []
    n_forecast = 0
    available_weight = 0.0
    forecast_weight = 0.0
    for j in range(1, window + 1):
        d = day - timedelta(days=j)
        value = series.get(d)
        if value is None:
            missing.append(d)
            continue
        weight = decay ** (j - lag_offset)
        try:
            total += float(value) * weight
        except (TypeError, ValueError):
            missing.append(d)
            continue
        available_weight += weight
        if d in forecast:
            n_forecast += 1
            forecast_weight += weight
    if len(missing) > max_gaps:
        return None
    return Api30(
        date=day,
        value=total,
        gaps=len(missing),
        missing=tuple(sorted(missing)),
        forecast_days=n_forecast,
        model_weight_fraction=(forecast_weight / available_weight if available_weight else 0.0),
    )


def api30(
    series: Mapping[date, float],
    day: date,
    *,
    window: int = WINDOW,
    decay: float = DECAY,
    lag_offset: int = LAG_OFFSET,
    max_gaps: int = MAX_GAPS,
) -> float | None:
    """API30 in mm for ``day``, or ``None`` when the window is too gappy."""
    detail = api30_detail(
        series,
        day,
        window=window,
        decay=decay,
        lag_offset=lag_offset,
        max_gaps=max_gaps,
    )
    return None if detail is None else detail.value


def extend_series(
    observed: Mapping[date, float], forecast: Mapping[date, float]
) -> dict[date, float]:
    """Merge a station series with a forecast one; the station always wins.

    Entries whose value is ``None`` are dropped from both sides, so a
    forecast can fill a hole the station left behind.
    """
    return merge_series(observed, forecast).values


def merge_series(
    observed: Mapping[date, float], forecast: Mapping[date, float]
) -> MergedSeries:
    """Merge values and retain which dates came from the model."""
    out: dict[date, float] = {}
    model_days: set[date] = set()
    for source in (forecast, observed):
        for day, value in source.items():
            if value is None:
                continue
            try:
                out[day] = float(value)
                if source is forecast:
                    model_days.add(day)
                else:
                    model_days.discard(day)
            except (TypeError, ValueError):
                continue
    return MergedSeries(out, frozenset(model_days))


def forecast_api30(
    observed: Mapping[date, float],
    forecast: Mapping[date, float],
    *,
    today: date,
    horizon: int = 16,
    window: int = WINDOW,
    decay: float = DECAY,
    lag_offset: int = LAG_OFFSET,
    max_gaps: int = MAX_GAPS,
) -> list[tuple[date, float]]:
    """API30 for ``today`` .. ``today + horizon`` inclusive, oldest first.

    ``horizon=16`` is exactly what Open-Meteo's ``forecast_days=16``
    supports: those 16 days are ``today`` .. ``today+15``, and because
    ``API30(t)`` never uses ``SRA(t)`` the curve is still fully determined
    on ``today+16``.

    Days whose window is too gappy are skipped rather than guessed, so the
    caller can still use the part of the curve that is trustworthy.
    """
    return [
        (detail.date, detail.value)
        for detail in forecast_api30_details(
            observed,
            forecast,
            today=today,
            horizon=horizon,
            window=window,
            decay=decay,
            lag_offset=lag_offset,
            max_gaps=max_gaps,
        )
    ]


def forecast_api30_details(
    observed: Mapping[date, float],
    forecast: Mapping[date, float],
    *,
    today: date,
    horizon: int = 16,
    window: int = WINDOW,
    decay: float = DECAY,
    lag_offset: int = LAG_OFFSET,
    max_gaps: int = MAX_GAPS,
) -> list[Api30]:
    """Detailed curve with gaps and model provenance for every point."""
    merged = merge_series(observed, forecast)
    curve: list[Api30] = []
    for offset in range(0, horizon + 1):
        day = today + timedelta(days=offset)
        detail = api30_detail(
            merged.values,
            day,
            window=window,
            decay=decay,
            lag_offset=lag_offset,
            max_gaps=max_gaps,
            forecast_days=merged.forecast_days,
        )
        if detail is not None:
            curve.append(detail)
    return curve


def crossing(
    curve: Iterable[tuple[date, float]],
    threshold: float = DEFAULT_THRESHOLD_MM,
    *,
    today: date,
) -> date | None:
    """First day after ``today`` where the curve crosses ``threshold`` upward.

    Returns ``None`` when today's value is already at or above the
    threshold (nothing to announce -- the condition is not *new*), when
    today is not in the curve at all, or when it never gets there.
    """
    points = sorted(curve)
    today_value = next((v for d, v in points if d == today), None)
    if today_value is None or today_value >= threshold:
        return None
    previous = today_value
    for day, value in points:
        if day <= today:
            continue
        if previous < threshold <= value:
            return day
        previous = value
    return None


def to_readings(
    curve: Iterable[tuple[date, float] | Api30],
    location_slug: str,
    *,
    today: date,
    threshold: float = DEFAULT_THRESHOLD_MM,
) -> list[Reading]:
    """Turn the curve into ``Reading``s of source ``api30_forecast``.

    Days after ``today`` carry ``meta["issued"] = today``, which is what
    marks them as a forecast for ``base``/``store`` (the store mirrors them
    into the ``forecasts`` table).  Today's own value is an estimate of an
    observation, so it is stored without ``issued`` and does not pollute
    the forecast-error archive.
    """
    readings: list[Reading] = []
    normalized = sorted(
        ((item.date, item.value, item) if isinstance(item, Api30) else (item[0], item[1], None))
        for item in curve
    )
    for day, value, detail in normalized:
        meta: dict[str, object] = {
            "threshold_mm": threshold,
            "decay": DECAY,
            "window": WINDOW,
        }
        if detail is not None:
            meta.update(
                {
                    "gaps": detail.gaps,
                    "missing": [d.isoformat() for d in detail.missing],
                    "model_days": detail.forecast_days,
                    "model_weight_fraction": round(detail.model_weight_fraction, 6),
                    "quality": "partial" if detail.gaps else "fresh",
                }
            )
        if day > today:
            meta["issued"] = today.isoformat()
            meta["horizon_days"] = (day - today).days
        else:
            meta["provisional"] = True
        readings.append(
            Reading(
                source=FORECAST_SOURCE,
                location=location_slug,
                date=day,
                metric=METRIC,
                value=round(float(value), 2),
                text=None,
                meta=meta,
            )
        )
    return readings


def temp_ok(t_mean: float | None, t_min: float | None) -> bool:
    """PLAN §3 trigger 4 temperature gate: mean 8..22 °C and minimum > 2 °C.

    Unknown temperatures fail the gate -- the trigger is opt-in, not opt-out.
    """
    if t_mean is None or t_min is None:
        return False
    try:
        mean, low = float(t_mean), float(t_min)
    except (TypeError, ValueError):
        return False
    return T_MEAN_MIN <= mean <= T_MEAN_MAX and low > T_MIN_ABOVE
