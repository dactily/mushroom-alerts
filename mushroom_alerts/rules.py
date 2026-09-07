"""The decision layer: fetch results + SQLite history -> exit code and text.

``decide`` is the only thing the CLI calls (contract in ``base``); it is
also where the API30 forecast curve is *derived* and written back to the
store, because the trigger needs the curve anyway -- see "Derivation" below.

Triggers (PLAN §3, one per location, any of them fires exit ``10``)
-------------------------------------------------------------------
``chmi_map``
    Today's ČHMÚ level is >= 4, **or** it rose by at least one step against
    the previous stored day.  A payload flagged ``meta["stale"]`` is
    ignored: an upstream that stopped updating must not look like news.
``houbymapa``
    The nearest cell has ``l >= 4`` or ``s >= 0.6`` today and did not
    yesterday.
``rain_forecast``
    Station rain over the last three station days (today's provisional
    10-minute total included) >= 20 mm with a mean temperature of 12..22 °C.
    Mushrooms follow the rain by roughly a week, so the message names the
    window ``rain + 7 .. rain + 12`` days.
``rain_window``
    The second half of the same story: today falls inside a window
    announced earlier and the station's API30 is still at/above the
    threshold, i.e. the ground really did stay wet.
``api30_cross``
    The forecast API30 curve crosses the threshold upward on some day *D*
    ahead (today is still below it), and Open-Meteo's temperature on *D*
    passes ``api30.temp_ok``.  Re-announced only when *D* moves by more
    than two days or the last announcement is older than 14 days -- a
    forecast that wobbles by a day is not news (PLAN §6).

Every trigger is a small pure function taking plain values and returning an
optional :class:`Signal`; :func:`decide` does all the store I/O around them,
which is what makes them testable without a database.

Derivation
----------
:func:`derive_api30` runs inside :func:`decide`, once per location, *after*
the fetchers have stored their readings: station ``sra_mm`` from SQLite is
the observed part, Open-Meteo ``precip_mm`` from this run is the forecast
part, and the resulting curve is upserted as source ``api30_forecast``
(today's value as an observation, later days as forecasts, which the store
mirrors into the ``forecasts`` table).  With no Open-Meteo result in this
run -- ``check --only chmi_map`` -- the step is skipped and the stored curve
is left alone; with no station history it is skipped too.

Antispam (PLAN §3 trigger 5)
----------------------------
Deliberately minimal, and the "season is open, stay silent until it drops
to <= 2" hysteresis of PLAN §3 is **not** implemented (dropped by the user).
What is implemented: a signal carries a ``key`` -- its identity as *news* --
and is suppressed when the same key was already sent for that location on
an earlier day inside ``Signal.cooldown``.  Re-running ``check`` on the same
day always re-emits (same text, same exit code, no second row in
``notifications``), so a repeated cron run is idempotent.

``notifications.text`` holds a JSON blob ``{"key", "text", "data"}`` rather
than bare prose: the rain window and the crossing date have to be read back
by the next run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from . import api30 as api30_lib
from . import fetch_openmeteo as openmeteo
from .base import (
    DataQuality,
    EXIT_SIGNAL,
    EXIT_SILENT,
    Decision,
    FetchResult,
    Location,
    Reading,
    SeriesPoint,
)
from .fetch_chmi_map import LEVEL_LABELS
from .quality import calendar_window
from .store import Store
from .views import location_snapshot

__all__ = [
    "Signal",
    "decide",
    "snapshot",
    "describe",
    "derive_api30",
    "chmi_map_signal",
    "houbymapa_signal",
    "rain_signal",
    "rain_window_signal",
    "api30_signal",
]

# -- sources ------------------------------------------------------------
CHMI_MAP = "chmi_map"
HOUBYMAPA = "houbymapa"
STATION = "chmi_station"
OPENMETEO = "openmeteo"
API30_FORECAST = api30_lib.FORECAST_SOURCE

#: How a dead source is named in the report, already in the right gender.
UNAVAILABLE = {
    CHMI_MAP: "карта ČHMÚ недоступна",
    HOUBYMAPA: "HoubyMapa недоступна",
    STATION: "станция недоступна",
    OPENMETEO: "Open-Meteo недоступен",
    API30_FORECAST: "API30 недоступен",
}

# -- trigger ids (also the ``trigger`` column of ``notifications``) ------
T_CHMI = "chmi_map"
T_HOUBY = "houbymapa"
T_RAIN_FORECAST = "rain_forecast"
T_RAIN_WINDOW = "rain_window"
T_API30_CROSS = "api30_cross"

# -- tunables (PLAN §3) --------------------------------------------------
CHMI_LEVEL = 4.0
HOUBY_LEVEL = 4.0
HOUBY_SCORE = 0.6
RAIN_DAYS = 3
RAIN_MM = 20.0
RAIN_T_MIN = 12.0
RAIN_T_MAX = 22.0
WINDOW_FROM_DAYS = 7
WINDOW_TO_DAYS = 12
CROSS_SHIFT_DAYS = 2
CROSS_MAX_AGE_DAYS = 14
#: Identical news is not repeated for this many days (antispam).
REPEAT_AFTER_DAYS = 7
#: A crossing further ahead than this is only "orientačně".
VAGUE_AFTER_DAYS = 7
#: Rain worth naming in the forecast summary.
NEXT_RAIN_MM = 5.0
#: How much station history the derivation and the rain trigger read.
HISTORY_DAYS = 45


@dataclass(frozen=True, slots=True)
class Signal:
    """One fired trigger: what to say, and how to recognise it next time."""

    trigger: str
    text: str
    key: str
    data: dict[str, Any] = field(default_factory=dict)
    cooldown: int = REPEAT_AFTER_DAYS


# ----------------------------------------------------------------------
# formatting helpers
# ----------------------------------------------------------------------
def _d(day: date | None) -> str:
    """Czech short date: ``2026-09-28`` -> ``28.9.``"""
    return "" if day is None else f"{day.day}.{day.month}."


def _mm(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.0f} mm" if abs(value) >= 10 else f"{value:.1f} mm"


def _c(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f} °C"


def _label(level: float | None) -> str | None:
    return None if level is None else LEVEL_LABELS.get(int(level))


# ----------------------------------------------------------------------
# triggers -- pure functions, no store
# ----------------------------------------------------------------------
def chmi_map_signal(today_level: Reading | None, previous: Reading | None) -> Signal | None:
    """PLAN §3 trigger 1: level >= 4, or one step up against the last day."""
    if today_level is None:
        return None
    if (today_level.meta or {}).get("stale"):
        return None
    level = float(today_level.value)
    before = None if previous is None else float(previous.value)
    rose = before is not None and level - before >= 1
    if not (level >= CHMI_LEVEL or rose):
        return None
    arrow = " ↑" if rose else ""
    label = _label(level)
    text = f"ČHMÚ {level:.0f}/5{arrow}" + (f" ({label})" if label else "")
    return Signal(
        trigger=T_CHMI,
        text=text,
        key=f"{level:.0f}/{'up' if rose else 'high'}",
        data={
            "level": level,
            "previous": before,
            "rose": rose,
            "date": today_level.date.isoformat(),
        },
    )


def houbymapa_signal(
    level: float | None,
    score: float | None,
    previous_level: float | None,
    previous_score: float | None,
    *,
    stale: bool = False,
) -> Signal | None:
    """PLAN §3 trigger 2: ``l >= 4`` or ``s >= 0.6`` today but not yesterday."""

    def good(lvl: float | None, sc: float | None) -> bool:
        return (lvl is not None and lvl >= HOUBY_LEVEL) or (
            sc is not None and sc >= HOUBY_SCORE
        )

    if stale or not good(level, score) or good(previous_level, previous_score):
        return None
    label = _label(level)
    parts = []
    if level is not None:
        parts.append(f"{level:.0f}/5")
    if score is not None:
        parts.append(f"{score:.2f}")
    if label:
        parts.append(f"({label})")
    return Signal(
        trigger=T_HOUBY,
        text="HoubyMapa " + " ".join(parts),
        key=f"{level}/{score}",
        data={
            "level": level,
            "score": score,
            "previous_level": previous_level,
            "previous_score": previous_score,
        },
    )


def rain_signal(
    sra: Mapping[date, float | SeriesPoint],
    t_mean: Mapping[date, float | SeriesPoint],
    today: date,
    *,
    days: int = RAIN_DAYS,
    minimum_mm: float = RAIN_MM,
) -> Signal | None:
    """PLAN §3 trigger 3, first half: enough rain at the right temperature.

    ``sra`` may include today's provisional lower bound.  The interval is
    calendar-based; missing rain cannot prove dryness, and missing or stale
    temperature closes the trigger.
    """
    rain = calendar_window(sra, today, days)
    temps = calendar_window(t_mean, today, days)
    if rain.total is None or temps.mean is None:
        return None
    if rain.total < minimum_mm or temps.covered_days != days:
        return None
    if temps.quality in {DataQuality.STALE, DataQuality.MISSING}:
        return None
    if not RAIN_T_MIN <= temps.mean <= RAIN_T_MAX:
        return None
    first_day = today - timedelta(days=days - 1)
    window = [d for d in sorted(sra) if first_day <= d <= today]
    anchor = max(
        window,
        key=lambda d: float(sra[d].value if isinstance(sra[d], SeriesPoint) else sra[d]),
    )
    start = anchor + timedelta(days=WINDOW_FROM_DAYS)
    end = anchor + timedelta(days=WINDOW_TO_DAYS)
    return Signal(
        trigger=T_RAIN_FORECAST,
        text=(
            f"дождь {_mm(rain.total)} за {days} дня (максимум {_d(anchor)}), "
            f"T {_c(temps.mean)} → окно {_d(start)}–{_d(end)}"
        ),
        # The identity of the news is the rain episode, not its exact sum:
        # tomorrow the same rain still dominates a shifted 3-day window.
        key=anchor.isoformat(),
        data={
            "total_mm": round(rain.total, 1),
            "anchor": anchor.isoformat(),
            "days": [d.isoformat() for d in window],
            "covered_days": rain.covered_days,
            "expected_days": rain.expected_days,
            "lower_bound": rain.lower_bound,
            "t_mean": round(temps.mean, 1),
            "window": [start.isoformat(), end.isoformat()],
        },
        cooldown=WINDOW_TO_DAYS,
    )


def rain_window_signal(
    episode: Mapping[str, Any] | None,
    api30_now: float | None,
    threshold: float,
    today: date,
) -> Signal | None:
    """PLAN §3 trigger 3, second half: the announced window has arrived.

    ``episode`` is the ``data`` blob of the earlier ``rain_forecast``
    notification; the window only counts if the ground is still wet, i.e.
    the station's API30 is at or above the threshold.
    """
    if not episode or api30_now is None or api30_now < threshold:
        return None
    raw = episode.get("window") or []
    if len(raw) != 2:
        return None
    try:
        start = date.fromisoformat(str(raw[0]))
        end = date.fromisoformat(str(raw[1]))
    except ValueError:
        return None
    if not start <= today <= end:
        return None
    anchor = ""
    if episode.get("anchor"):
        try:
            anchor = f" после дождя {_d(date.fromisoformat(str(episode['anchor'])))}"
        except ValueError:
            anchor = ""
    return Signal(
        trigger=T_RAIN_WINDOW,
        text=(
            f"окно роста{anchor} началось ({_d(start)}–{_d(end)}), "
            f"API30 {_mm(api30_now)} ≥ {_mm(threshold)}"
        ),
        key=start.isoformat(),
        data={
            "window": [start.isoformat(), end.isoformat()],
            "api30_mm": round(api30_now, 1),
            "threshold_mm": threshold,
        },
        cooldown=(end - start).days + 1,
    )


def api30_signal(
    curve: Sequence[tuple[date, float]],
    temperatures: Mapping[date, Mapping[str, float]],
    threshold: float,
    today: date,
    *,
    rain: Mapping[date, float] | None = None,
) -> Signal | None:
    """PLAN §3 trigger 4: the forecast API30 crosses ``threshold`` on day D.

    The temperature gate uses Open-Meteo's own ``t_mean``/``t_min`` on D
    (``api30.temp_ok``); unknown temperature fails it on purpose.

    One consequence is deliberate: the API30 curve runs one day further than
    the rain forecast (``API30(t)`` never uses ``SRA(t)``, so ``today+16`` is
    still computable from a 16-day forecast that ends on ``today+15``), and
    that last day has no temperature.  A crossing that lands exactly there
    stays silent for one day and fires tomorrow, when the horizon has rolled
    forward -- better than inventing a temperature for it.  ``status`` shows
    the crossing anyway, so it is not invisible.
    """
    if not curve:
        return None
    day = api30_lib.crossing(curve, threshold, today=today)
    if day is None:
        return None
    temps = temperatures.get(day) or {}
    if not api30_lib.temp_ok(temps.get("t_mean"), temps.get("t_min")):
        return None
    peak_day, peak_value = max(curve, key=lambda pair: (pair[1], pair[0]))
    horizon = (day - today).days
    text = f"прогноз: API30 ≥ {_mm(threshold)} с {_d(day)}"
    text += f" (пик {_mm(peak_value)} {_d(peak_day)}"
    wettest = _wettest(rain or {}, today, day)
    if wettest is not None:
        text += f", дождь {_mm(wettest[1])} {_d(wettest[0])}"
    text += ")"
    if horizon > VAGUE_AFTER_DAYS:
        text += " — ориентировочно"
    return Signal(
        trigger=T_API30_CROSS,
        text=text,
        key=day.isoformat(),
        data={
            "cross": day.isoformat(),
            "horizon_days": horizon,
            "threshold_mm": threshold,
            "peak": [peak_day.isoformat(), round(peak_value, 1)],
            "rain": None if wettest is None else [wettest[0].isoformat(), round(wettest[1], 1)],
            "vague": horizon > VAGUE_AFTER_DAYS,
        },
    )


def _wettest(
    rain: Mapping[date, float], today: date, until: date
) -> tuple[date, float] | None:
    """The rain day that drove a crossing: wettest day in ``(today, D]``."""
    days = [d for d in rain if today < d <= until and float(rain[d]) > 0]
    if not days:
        return None
    best = max(days, key=lambda d: float(rain[d]))
    return best, float(rain[best])


# ----------------------------------------------------------------------
# derivation: the API30 forecast curve
# ----------------------------------------------------------------------
def derive_api30(
    store: Store,
    location: Location,
    forecast_result: FetchResult | None,
    *,
    today: date,
    threshold: float,
) -> list[tuple[date, float]]:
    """Compute and persist the API30 curve for one location.

    Returns the curve (possibly empty).  Nothing is written when either
    half is missing, so ``check --only openmeteo`` on a fresh database is a
    no-op rather than a crash, and ``check --only chmi_map`` leaves
    yesterday's stored curve intact.
    """
    if forecast_result is None or not forecast_result.ok:
        return []
    since = today - timedelta(days=HISTORY_DAYS)
    observed = {}
    for reading in store.series(
        STATION, location.slug, "sra_mm", since=since, until=today
    ):
        meta = reading.meta or {}
        if meta.get("provisional") and not meta.get("complete"):
            continue
        observed[reading.date] = float(reading.value)
    if not observed:
        return []
    forecast = openmeteo.series(forecast_result.readings, location.slug, "precip_mm")
    if not forecast:
        return []
    details = api30_lib.forecast_api30_details(observed, forecast, today=today)
    if details:
        input_run_ids = sorted(
            {
                str((reading.meta or {}).get("run_id"))
                for reading in forecast_result.readings
                if (reading.meta or {}).get("run_id")
            }
        )
        store.upsert_readings(
            api30_lib.to_readings(details, location.slug, today=today, threshold=threshold),
            retrieved_at=forecast_result.fetched_at,
            calculation_version=api30_lib.CALCULATION_VERSION,
            input_run_ids=input_run_ids,
        )
    return [(detail.date, detail.value) for detail in details]


# ----------------------------------------------------------------------
# snapshot: what one location looks like right now, straight from SQLite
# ----------------------------------------------------------------------
def snapshot(
    store: Store,
    location: Location,
    today: date,
    results: Sequence[FetchResult] = (),
) -> dict[str, Any]:
    """Everything the report needs, through the shared read model."""
    return location_snapshot(
        store,
        location,
        today,
        history_days=HISTORY_DAYS,
        rain_days=RAIN_DAYS,
        next_rain_mm=NEXT_RAIN_MM,
        results=results,
    )


# ----------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------
def _facts(snap: Mapping[str, Any]) -> list[str]:
    parts: list[str] = []
    chmi = snap.get("chmi")
    if chmi:
        arrow = ""
        if chmi["previous"] is not None:
            if chmi["level"] > chmi["previous"]:
                arrow = " ↑"
            elif chmi["level"] < chmi["previous"]:
                arrow = " ↓"
        flag = " (starý)" if chmi["stale"] else ""
        parts.append(f"ČHMÚ {chmi['level']:.0f}/5{arrow}{flag}")
    houby = snap.get("houbymapa")
    if houby:
        bits = []
        if houby["level"] is not None:
            bits.append(f"{houby['level']:.0f}/5")
            if houby["score"] is not None:
                bits.append(f"({houby['score']:.2f})")
        elif houby["score"] is not None:
            bits.append(f"score {houby['score']:.2f}")
        if houby["stale"]:
            bits.append("(starý)")
        parts.append(("HoubyMapa " + " ".join(bits)).strip())
    station = snap.get("station")
    if station:
        bits = []
        if station["api30_mm"] is not None:
            bits.append(f"API30 {_mm(station['api30_mm'])}")
        if station["sra_window_mm"] is not None:
            bits.append(f"SRA {station['sra_window_days']}d {_mm(station['sra_window_mm'])}")
        if station["t_mean"] is not None:
            bits.append(f"T {_c(station['t_mean'])}")
        if bits:
            parts.append("станция " + ", ".join(bits))
    return parts


def _forecast_segment(snap: Mapping[str, Any]) -> str | None:
    fc = snap.get("forecast")
    if not fc:
        return None
    bits = []
    if fc["today_mm"] is not None:
        bits.append(f"API30 сегодня {_mm(fc['today_mm'])}")
    if fc.get("peak") is not None:
        peak_day, peak_value = fc["peak"]
        bits.append(f"max {_mm(peak_value)} {_d(peak_day)}")
    if fc["next_rain"] is not None:
        day, value = fc["next_rain"]
        bits.append(f"дождь {_mm(value)} {_d(day)}")
    threshold = fc["threshold_mm"]
    if fc.get("quality") != DataQuality.FRESH.value:
        bits.append(f"порог {_mm(threshold)}: недостаточно данных")
    elif fc["cross"] is not None:
        bits.append(f"порог {_mm(threshold)} пройден {_d(fc['cross'])}")
    elif fc["today_mm"] is not None and fc["today_mm"] >= threshold:
        bits.append(f"порог {_mm(threshold)} пройден уже сегодня")
    else:
        bits.append(f"порог {_mm(threshold)} —")
    return "прогноз: " + ", ".join(bits)


def format_line(
    snap: Mapping[str, Any],
    signals: Sequence[Signal] = (),
    notes: Sequence[str] = (),
) -> str:
    """One location, one line (PLAN §3).

    With signals the line carries them; without, it carries the forecast
    summary -- that is the difference between ``check`` and ``status``.
    """
    facts = _facts(snap)
    extras: list[str] = []
    if signals:
        # The ČHMÚ / HoubyMapa triggers describe the same numbers the facts
        # already carry, only with the arrow and the label -- so let the
        # trigger wording replace that fact instead of repeating it.
        for sig in signals:
            head = "ČHMÚ" if sig.trigger == T_CHMI else "HoubyMapa" if sig.trigger == T_HOUBY else None
            if head is None:
                extras.append(sig.text)
                continue
            facts = [sig.text if f.startswith(head) else f for f in facts]
            if not any(f.startswith(head) for f in facts):
                facts.append(sig.text)
    else:
        segment = _forecast_segment(snap)
        if segment:
            extras.append(segment)
    body = ", ".join(facts)
    if extras:
        body = " · ".join([body, *extras]) if body else " · ".join(extras)
    if not body:
        body = "нет данных"
    for note in notes:
        body += f" ({note})"
    return f"🍄 {snap['name']}: {body}"


def describe(store: Store, location: Location, today: date) -> str:
    """``status``: the stored snapshot of one location as a single line."""
    return format_line(snapshot(store, location, today))


# ----------------------------------------------------------------------
# antispam / notification bookkeeping
# ----------------------------------------------------------------------
def _last_note(store: Store, slug: str, trigger: str) -> tuple[date, dict[str, Any]] | None:
    row = store.last_notification(slug, trigger)
    if row is None:
        return None
    try:
        day = date.fromisoformat(row["date"])
    except (TypeError, ValueError):
        return None
    try:
        payload = json.loads(row["text"])
        if not isinstance(payload, dict):
            raise ValueError
    except (TypeError, ValueError):
        payload = {"key": row["text"], "text": row["text"], "data": {}}
    return day, payload


def _allowed(store: Store, slug: str, signal: Signal, today: date) -> bool:
    """Antispam gate.  See the module docstring; deliberately small."""
    last = _last_note(store, slug, signal.trigger)
    if last is None:
        return True
    day, payload = last
    if day == today:
        return True  # same-day re-run: identical output, identical exit code
    if signal.trigger == T_API30_CROSS:
        age = (today - day).days
        if age > CROSS_MAX_AGE_DAYS:
            return True
        try:
            previous = date.fromisoformat(str((payload.get("data") or {}).get("cross")))
        except (TypeError, ValueError):
            return True
        current = date.fromisoformat(signal.data["cross"])
        return abs((current - previous).days) > CROSS_SHIFT_DAYS
    if payload.get("key") == signal.key and (today - day).days <= signal.cooldown:
        return False
    return True


def _record(store: Store, slug: str, signal: Signal, today: date) -> None:
    last = _last_note(store, slug, signal.trigger)
    if last is not None and last[0] == today and last[1].get("key") == signal.key:
        return  # already logged today -- do not duplicate the row
    store.add_notification(
        today,
        slug,
        signal.trigger,
        json.dumps(
            {"key": signal.key, "text": signal.text, "data": signal.data},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


# ----------------------------------------------------------------------
# the entry point
# ----------------------------------------------------------------------
def _latest(results: Iterable[FetchResult], slug: str, source: str, metric: str) -> Reading | None:
    """Newest *observation* of one metric produced by **this run**."""
    best: Reading | None = None
    for result in results:
        if result.source != source or not result.ok:
            continue
        for reading in result.readings:
            if reading.location != slug or reading.metric != metric:
                continue
            if reading.is_forecast:
                continue
            if best is None or reading.date > best.date:
                best = reading
    return best


def _signals_for(
    location: Location,
    results: Sequence[FetchResult],
    snap: Mapping[str, Any],
    curve: Sequence[tuple[date, float]],
    *,
    store: Store,
    today: date,
    threshold: float,
) -> list[Signal]:
    slug = location.slug
    signals: list[Signal] = []

    # -- trigger 1: ČHMÚ map ------------------------------------------
    level = _latest(results, slug, CHMI_MAP, "level")
    if level is not None:
        previous = store.latest(CHMI_MAP, slug, "level", before=level.date)
        signal = chmi_map_signal(level, previous)
        if signal is not None:
            signals.append(signal)

    # -- trigger 2: HoubyMapa -----------------------------------------
    h_level = _latest(results, slug, HOUBYMAPA, "level")
    h_score = _latest(results, slug, HOUBYMAPA, "score")
    if h_level is not None or h_score is not None:
        day = max(r.date for r in (h_level, h_score) if r is not None)
        p_level = store.latest(HOUBYMAPA, slug, "level", before=day)
        p_score = store.latest(HOUBYMAPA, slug, "score", before=day)
        signal = houbymapa_signal(
            None if h_level is None else float(h_level.value),
            None if h_score is None else float(h_score.value),
            None if p_level is None else float(p_level.value),
            None if p_score is None else float(p_score.value),
            stale=bool(((h_level or h_score).meta or {}).get("stale")),
        )
        if signal is not None:
            signals.append(signal)

    # -- trigger 3: rain precursor, then the window it announced -------
    station = snap.get("station") or {}
    series = station.get("series") or {}
    series_points = station.get("series_points") or {}
    sra = series.get("sra_mm") or {}
    t_mean = series.get("t_mean") or {}
    station_quality = station.get("quality")
    station_usable = station_quality not in {DataQuality.STALE.value, DataQuality.MISSING.value}
    if sra and station_usable:
        signal = rain_signal(
            series_points.get("sra_mm") or sra,
            series_points.get("t_mean") or t_mean,
            today,
        )
        if signal is not None:
            signals.append(signal)
    episode = _last_note(store, slug, T_RAIN_FORECAST)
    if episode is not None and station_usable:
        api_now = station.get("api30_mm")
        if api_now is None and sra:
            api_now = api30_lib.api30(sra, today)
        signal = rain_window_signal(episode[1].get("data"), api_now, threshold, today)
        if signal is not None:
            signals.append(signal)

    # -- trigger 4: the forecast API30 crossing ------------------------
    forecast_result = next(
        (r for r in results if r.source == OPENMETEO and r.ok), None
    )
    forecast = snap.get("forecast") or {}
    cross = api30_lib.crossing(curve, threshold, today=today) if curve else None
    cross_quality = forecast.get("cross_quality") if cross is not None else None
    forecast_usable = (
        forecast.get("openmeteo_quality") == DataQuality.FRESH.value
        and cross_quality == DataQuality.FRESH.value
    )
    if curve and forecast_result is not None and forecast_usable:
        signal = api30_signal(
            curve,
            openmeteo.temperatures(forecast_result.readings, slug),
            threshold,
            today,
            rain=openmeteo.series(forecast_result.readings, slug, "precip_mm"),
        )
        if signal is not None:
            signals.append(signal)
    return signals


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(_jsonable(k)): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def decide(
    locations: list[Location],
    results: list[FetchResult],
    *,
    store: Store,
    today: date,
) -> Decision:
    """PLAN §2/§3: exit ``10`` plus a message, or ``0`` and the plain snapshot.

    Readings of this run are already in the store when we get here (see
    ``__main__.cmd_check``), which is what makes "yesterday" queryable.
    """
    threshold = api30_lib.threshold_mm()
    forecast_result = next((r for r in results if r.source == OPENMETEO), None)
    notes = [UNAVAILABLE.get(r.source, f"{r.source} недоступен") for r in results if not r.ok]
    # Every source dead is exit 1 in the CLI, and the message would never be
    # sent -- so do not burn the antispam slot on a signal nobody will read.
    blackout = bool(results) and all(not r.ok for r in results)

    lines: list[str] = []
    fired_lines: list[str] = []
    data: dict[str, Any] = {
        "date": today.isoformat(),
        "threshold_mm": threshold,
        "failed_sources": [r.source for r in results if not r.ok],
        "locations": {},
    }

    for location in locations:
        curve = derive_api30(
            store, location, forecast_result, today=today, threshold=threshold
        )
        snap = snapshot(store, location, today, results)
        candidates = _signals_for(
            location,
            results,
            snap,
            curve,
            store=store,
            today=today,
            threshold=threshold,
        )
        fired = (
            []
            if blackout
            else [s for s in candidates if _allowed(store, location.slug, s, today)]
        )
        for signal in fired:
            _record(store, location.slug, signal, today)

        lines.append(format_line(snap, notes=notes))
        if fired:
            fired_lines.append(format_line(snap, fired, notes=notes))

        view = {k: v for k, v in snap.items() if k != "station"}
        if snap.get("station"):
            view["station"] = {
                k: v
                for k, v in snap["station"].items()
                if k not in {"series", "series_points"}
            }
        data["locations"][location.slug] = {
            "snapshot": _jsonable(view),
            "signals": [
                {"trigger": s.trigger, "text": s.text, "key": s.key, "data": s.data}
                for s in fired
            ],
            "suppressed": [s.trigger for s in candidates if s not in fired],
        }

    if fired_lines:
        return Decision(exit_code=EXIT_SIGNAL, text="\n".join(fired_lines), data=data)
    return Decision(exit_code=EXIT_SILENT, text="\n".join(lines), data=data)
