"""``python -m mushroom_alerts brief``: the data brief an LLM agent reads.

Why this exists
---------------
``check`` collapses everything into one line per location and an exit code.
That is a good machine contract and a poor thing to read: "API30 20 mm,
SRA 3d 0.2 mm" says nothing to a human about whether to take a basket.
The conservative biological verdict now lives in the application. Hermes
Agent (see ``hermes/PROMPT.md``) only turns that verdict and the underlying
facts into short prose. This prevents an LLM from treating moisture
immediately after rain as proof that fruiting bodies already exist.

``check`` remains the deterministic exit-code interface (PLAN §2b).

Shape of the brief (per location, ~1-2 KB)
------------------------------------------
1. header: name, coordinates, brief time in Europe/Prague, the station;
2. facts for today: ČHMÚ map level (+ change vs yesterday and vs 7 days
   ago), HoubyMapa level/score (+ the same changes), station API30, SRA
   over 1/3/7/30 days, yesterday's temperatures, soil temperatures,
   humidity;
3. the last :data:`HISTORY_DAYS` days as a table (SRA, API30, T);
4. the forecast: Open-Meteo rain/temperature per day plus the derived API30
   curve, the threshold crossing and the peak;
5. which deterministic triggers fired today, and which sources failed.

The "Как читать" cheat sheet (:data:`CHEAT_SHEET`) is printed **once** at
the end, not per location: it is stable text and repeating it per location
would double the brief for no information.

Everything is read back out of SQLite rather than out of the fetch results,
so ``brief`` renders the same thing whether the data arrived a second ago
or yesterday (a dead source shows its last stored value, dated).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from . import api30 as api30_lib
from . import rules as rules_lib
from . import policy
from .base import FetchResult, Location, SeriesPoint
from .quality import calendar_window, point_from_reading
from .store import Store

__all__ = [
    "TZ_NAME",
    "HISTORY_DAYS",
    "DEFAULT_DAYS",
    "SRA_WINDOWS",
    "CHEAT_SHEET",
    "build",
    "render",
    "local_now",
]

#: Timezone the brief is stamped in (PLAN §2: Hermes runs at 08:30 Prague).
TZ_NAME = "Europe/Prague"

#: How many past days the history table shows (the station fetcher stores 35).
HISTORY_DAYS = 14

#: Default forecast horizon, matching Open-Meteo's ``forecast_days=16``.
DEFAULT_DAYS = 16

#: Trailing windows summed for the "осадки SRA" line.
SRA_WINDOWS = (1, 3, 7, 30)

#: Soil metrics printed, in order, when the station publishes them.
SOIL_METRICS = (("t_soil_5", "5 см"), ("t_soil_10", "10 см"), ("t_soil_20", "20 см"))

CHMI_MAP = rules_lib.CHMI_MAP
HOUBYMAPA = rules_lib.HOUBYMAPA
STATION = rules_lib.STATION
OPENMETEO = rules_lib.OPENMETEO
API30_FORECAST = rules_lib.API30_FORECAST

#: Interpretation cheat sheet.  Stable text: Hermes is told to treat it as
#: authoritative, so it must not drift from run to run.  Numbers in it are
#: the ones measured during the reconnaissance (PLAN §1, §5, §6).
CHEAT_SHEET = policy.interpretation_guide()


# ----------------------------------------------------------------------
# small formatting helpers
# ----------------------------------------------------------------------
DASH = "—"

HIGH_BLOCKER_LABELS = {
    "no_qualified_rain_episode": "нет подтверждённого дождевого эпизода",
    "growth_window_not_started": "окно D+7 ещё не началось",
    "growth_window_finished": "окно D+12 закончилось",
    "api30_not_fresh": "API30 неполный или устарел",
    "api30_below_threshold": "API30 ниже порога",
    "forecast_not_fresh": "прогноз устарел",
    "temperature_gate_failed": "температурное условие не выполнено",
    "history_insufficient": "истории недостаточно",
    "frost_or_incomplete_frost_history": "есть заморозок или неполна история минимумов",
    "no_fresh_high_map_support": "нет свежей высокой поддержки карт",
}


def local_now() -> datetime:
    """Now in :data:`TZ_NAME`, or naive local time if the tzdb is missing."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:  # noqa: BLE001 - no tzdata: a stamp is better than a crash
        return datetime.now()


def _n(value: float | None, digits: int = 1) -> str:
    return DASH if value is None else f"{value:.{digits}f}"


def _iso(day: date | None) -> str:
    return DASH if day is None else day.isoformat()


def _level(value: float | None) -> str:
    return DASH if value is None else f"{value:.0f}/5"


def _level_score(level: float | None, score: float | None) -> str:
    if level is None and score is None:
        return DASH
    if score is None:
        return _level(level)
    if level is None:
        return f"score {score:.2f}"
    return f"{_level(level)} ({score:.2f})"


# ----------------------------------------------------------------------
# gathering -- everything comes out of the store
# ----------------------------------------------------------------------
def _value_on(store: Store, source: str, slug: str, metric: str, day: date) -> float | None:
    reading = store.get_reading(source, slug, metric, day)
    return None if reading is None else float(reading.value)


def _latest(store: Store, source: str, slug: str, metric: str) -> tuple[date, float] | None:
    reading = store.latest(source, slug, metric)
    return None if reading is None else (reading.date, float(reading.value))


def _series(
    store: Store, source: str, slug: str, metric: str, since: date, until: date
) -> dict[date, float]:
    return {
        r.date: float(r.value)
        for r in store.series(source, slug, metric, since=since, until=until)
    }


def _curve(
    store: Store, source: str, slug: str, metric: str, today: date
) -> dict[date, float]:
    """The newest forecast run for one metric, plus today's own value.

    The store keeps forecasts in their own table keyed by issue date, while
    the value for *today* is written as an observation -- so a curve is
    always "newest issue + today".  Same rule for Open-Meteo and for the
    derived ``api30_forecast``.
    """
    out: dict[date, float] = {}
    issued = store.latest_issue(source, slug, metric)
    if issued:
        for row in store.forecast_series(source, slug, metric, issued):
            out[date.fromisoformat(row["target_date"])] = float(row["value"])
    now = _value_on(store, source, slug, metric, today)
    if now is not None:
        out[today] = now
    return out


def _window_sums(
    sra: Mapping[date, float | SeriesPoint],
    today: date,
    windows: Sequence[int] = SRA_WINDOWS,
) -> list[dict[str, Any]]:
    """Trailing SRA sums over exact calendar intervals."""
    out = []
    for span in windows:
        aggregate = calendar_window(sra, today, span)
        out.append(
            {
                "window_days": span,
                "mm": None if aggregate.total is None else round(aggregate.total, 1),
                "days": aggregate.covered_days,
                "expected_days": aggregate.expected_days,
                "quality": aggregate.quality.value,
                "lower_bound": aggregate.lower_bound,
            }
        )
    return out


def _map_block(
    store: Store, source: str, slug: str, snap_block: Mapping[str, Any] | None, metric: str
) -> dict[str, Any] | None:
    """ČHMÚ / HoubyMapa level with its change vs yesterday and vs a week ago."""
    if not snap_block:
        return None
    day = snap_block["date"]
    return {
        "yesterday": _value_on(store, source, slug, metric, day - timedelta(days=1)),
        "week_ago": _value_on(store, source, slug, metric, day - timedelta(days=7)),
    }


def location_view(
    store: Store,
    location: Location,
    today: date,
    *,
    days: int = DEFAULT_DAYS,
    signals: Sequence[Mapping[str, Any]] = (),
    results: Sequence[FetchResult] = (),
) -> dict[str, Any]:
    """Every fact the brief prints about one location, straight from SQLite."""
    slug = location.slug
    snap = rules_lib.snapshot(store, location, today, results)
    threshold = api30_lib.threshold_mm()

    # -- now ----------------------------------------------------------
    chmi = None
    if snap.get("chmi"):
        chmi = dict(snap["chmi"])
        chmi.update(_map_block(store, CHMI_MAP, slug, snap["chmi"], "level") or {})

    houby = None
    if snap.get("houbymapa"):
        houby = dict(snap["houbymapa"])
        day = houby["date"]
        houby["yesterday_level"] = _value_on(store, HOUBYMAPA, slug, "level", day - timedelta(days=1))
        houby["yesterday_score"] = _value_on(store, HOUBYMAPA, slug, "score", day - timedelta(days=1))
        houby["week_ago_level"] = _value_on(store, HOUBYMAPA, slug, "level", day - timedelta(days=7))
        houby["week_ago_score"] = _value_on(store, HOUBYMAPA, slug, "score", day - timedelta(days=7))

    station_snap = snap.get("station") or {}
    series = station_snap.get("series") or {}
    sra: dict[date, float] = dict(series.get("sra_mm") or {})
    t_mean: dict[date, float] = dict(series.get("t_mean") or {})
    since = today - timedelta(days=max(HISTORY_DAYS, max(SRA_WINDOWS)) + 1)
    api_obs = _series(store, STATION, slug, "api30_mm", since, today)
    t_min = _series(store, STATION, slug, "t_min", since, today)
    t_max = _series(store, STATION, slug, "t_max", since, today)
    sra_points = {
        r.date: point_from_reading(r, today)
        for r in store.series(STATION, slug, "sra_mm", since=since, until=today)
    }

    temp_day = max(t_mean) if t_mean else None
    soil = []
    for metric, label in SOIL_METRICS:
        found = _latest(store, STATION, slug, metric)
        if found is not None:
            soil.append({"metric": metric, "label": label, "date": found[0], "value": found[1]})
    rh = _latest(store, STATION, slug, "rh")

    station: dict[str, Any] | None = None
    if station_snap:
        station = {
            "meta": station_snap.get("station"),
            "api30_mm": station_snap.get("api30_mm"),
            "api30_date": station_snap.get("api30_date"),
            "sra": _window_sums(sra_points, today),
            "sra_last_date": station_snap.get("sra_last_date"),
            "sra_until": station_snap.get("sra_until"),
            "sra_covered_slots": station_snap.get("sra_covered_slots"),
            "sra_expected_slots": station_snap.get("sra_expected_slots"),
            "quality": station_snap.get("quality", "missing"),
            "temp_date": temp_day,
            "t_mean": None if temp_day is None else t_mean.get(temp_day),
            "t_min": None if temp_day is None else t_min.get(temp_day),
            "t_max": None if temp_day is None else t_max.get(temp_day),
            "soil": soil,
            "rh": None if rh is None else {"date": rh[0], "value": rh[1]},
        }

    # -- history ------------------------------------------------------
    history = [
        {
            "date": day,
            "sra_mm": sra.get(day),
            "api30_mm": api_obs.get(day),
            "t_mean": t_mean.get(day),
        }
        for day in (
            today - timedelta(days=n) for n in range(HISTORY_DAYS - 1, -1, -1)
        )
    ]

    # -- forecast: all model metrics come from the coherent run selected
    # by the shared snapshot builder.
    bundle = snap.get("forecast") or {}
    rain = dict(bundle.get("rain") or {})
    f_mean = dict(bundle.get("t_mean") or {})
    f_min = dict(bundle.get("t_min") or {})
    api_curve = dict(bundle.get("curve") or [])
    biological = snap.get("biological") or {}
    outlook = {
        row["date"]: row
        for row in (biological.get("guidance") or {}).get("outlook", [])
    }

    rows = []
    for offset in range(0, max(days, 0) + 1):
        day = today + timedelta(days=offset)
        row = {
            "date": day,
            "offset": offset,
            "precip_mm": rain.get(day),
            "t_mean": f_mean.get(day),
            "t_min": f_min.get(day),
            "api30_mm": api_curve.get(day),
            "verdict": (outlook.get(day) or {}).get("verdict_label"),
        }
        rows.append(row)
    while rows and all(
        rows[-1][k] is None
        for k in ("precip_mm", "t_mean", "t_min", "api30_mm", "verdict")
    ):
        rows.pop()

    curve = sorted(api_curve.items())
    cross = api30_lib.crossing(curve, threshold, today=today) if curve else None
    peak = max(curve, key=lambda pair: (pair[1], pair[0])) if curve else None
    next_rain = next(
        ((d, rain[d]) for d in sorted(rain) if d > today and rain[d] >= rules_lib.NEXT_RAIN_MM),
        None,
    )
    today_api30 = api_curve.get(today)
    forecast = {
        "days": rows,
        "threshold_mm": threshold,
        "today_mm": today_api30,
        "cross": cross,
        "above_threshold_today": today_api30 is not None and today_api30 >= threshold,
        "peak": peak,
        "next_rain": next_rain,
        "quality": bundle.get("quality", "missing"),
        "openmeteo_quality": bundle.get("openmeteo_quality", "missing"),
        "api30_quality": bundle.get("api30_quality", "missing"),
        "openmeteo_run_id": bundle.get("openmeteo_run_id"),
        "api30_run_id": bundle.get("api30_run_id"),
        "openmeteo_retrieved_at": bundle.get("openmeteo_retrieved_at"),
        "api30_retrieved_at": bundle.get("api30_retrieved_at"),
    }

    return {
        "name": location.name,
        "slug": slug,
        "lat": location.lat,
        "lon": location.lon,
        "chmi": chmi,
        "houbymapa": houby,
        "station": station,
        "history": history,
        "forecast": forecast,
        "biological": biological,
        "source_status": snap.get("source_status") or {},
        "signals": [dict(s) for s in signals],
    }


def build(
    store: Store,
    locations: Sequence[Location],
    today: date,
    *,
    days: int = DEFAULT_DAYS,
    decision_data: Mapping[str, Any] | None = None,
    notes: Sequence[str] = (),
    failed_sources: Sequence[str] = (),
    results: Sequence[FetchResult] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """The whole brief as a plain dict: what :func:`render` prints.

    ``decision_data`` is ``Decision.data`` from ``rules.decide`` -- the same
    blob ``check --json`` publishes; the fired triggers are read out of it,
    so the brief and ``check`` can never disagree about what fired.
    """
    per_location = (decision_data or {}).get("locations") or {}
    stamp = now or local_now()
    return {
        "date": today.isoformat(),
        "generated_at": stamp.isoformat(timespec="minutes"),
        "timezone": TZ_NAME,
        "threshold_mm": api30_lib.threshold_mm(),
        "history_days": HISTORY_DAYS,
        "forecast_days": days,
        "notes": list(notes),
        "failed_sources": list(failed_sources),
        "cheat_sheet": CHEAT_SHEET,
        "locations": [
            location_view(
                store,
                loc,
                today,
                days=days,
                signals=(per_location.get(loc.slug) or {}).get("signals") or (),
                results=results,
            )
            for loc in locations
        ],
    }


# ----------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------
def _facts_lines(view: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    chmi = view.get("chmi")
    if chmi:
        label = f" ({chmi['label']})" if chmi.get("label") else ""
        stale = ", карта не обновлялась (stale)" if chmi.get("stale") else ""
        out.append(
            f"  карта ČHMÚ: {_level(chmi['level'])}{label}, карта за {_iso(chmi['date'])}"
            f"; вчера {_level(chmi.get('yesterday'))}"
            f", 7 дней назад {_level(chmi.get('week_ago'))}{stale}"
        )
    else:
        out.append("  карта ČHMÚ: нет данных")

    houby = view.get("houbymapa")
    if houby:
        stale = ", не обновлялась (stale)" if houby.get("stale") else ""
        out.append(
            f"  HoubyMapa: {_level_score(houby.get('level'), houby.get('score'))}"
            f", за {_iso(houby['date'])}"
            f"; вчера {_level_score(houby.get('yesterday_level'), houby.get('yesterday_score'))}"
            f", 7 дней назад {_level_score(houby.get('week_ago_level'), houby.get('week_ago_score'))}"
            f"{stale}"
        )
    else:
        out.append("  HoubyMapa: нет данных")

    station = view.get("station")
    if not station:
        out.append("  станция: нет данных")
        return out

    out.append(
        f"  станция API30: {_n(station.get('api30_mm'))} мм за {_iso(station.get('api30_date'))}"
    )
    sums = ", ".join(
        f"{w['window_days']} д {_n(w['mm'])} мм" + ("" if w["days"] >= w["window_days"] else f" ({w['days']} дн. с данными)")
        for w in station.get("sra") or []
    )
    station_tail = f"; последний день станции {_iso(station.get('sra_last_date'))}"
    if station.get("quality") == "partial" and station.get("sra_until"):
        station_tail += f", выпало к {str(station['sra_until'])[11:16]} UTC"
    out.append(f"  осадки SRA: {sums}" + station_tail)
    out.append(
        f"  температура за {_iso(station.get('temp_date'))}: средняя {_n(station.get('t_mean'))} °C"
        f", мин {_n(station.get('t_min'))} °C, макс {_n(station.get('t_max'))} °C"
    )
    if station.get("soil"):
        soil = ", ".join(f"{s['label']} {_n(s['value'])} °C" for s in station["soil"])
        out.append(f"  температура почвы: {soil}")
    if station.get("rh"):
        out.append(f"  влажность: {_n(station['rh']['value'], 0)} % за {_iso(station['rh']['date'])}")
    return out


def _history_lines(view: Mapping[str, Any]) -> list[str]:
    rows = view.get("history") or []
    out = [f"история {len(rows)} дн. (дата | SRA мм | API30 мм | T ср °C):"]
    for row in rows:
        out.append(
            f"  {_iso(row['date'])} {_n(row['sra_mm']):>6} {_n(row['api30_mm']):>7} {_n(row['t_mean']):>7}"
        )
    return out


def _biological_lines(view: Mapping[str, Any]) -> list[str]:
    bio = view.get("biological") or {}
    temp = bio.get("temperature_7d") or {}
    frost = bio.get("frost") or {}
    episode = bio.get("rain_episode")
    dynamics = bio.get("api30_dynamics") or {}
    history = bio.get("history") or {}
    guidance = bio.get("guidance") or {}
    out = ["биологическая оценка (детерминированная):"]
    out.append(
        f"  вердикт сегодня: {guidance.get('verdict_label', 'недостаточно данных')} "
        f"(rules v{bio.get('rules_version', '?')})"
    )
    out.append(f"  фаза: {guidance.get('phase_text', 'недостаточно данных')}")
    candidate = guidance.get("candidate_high_date")
    if candidate is None:
        out.append("  возможная высокая вероятность: нет в горизонте прогноза")
    else:
        forecast_days = (view.get("forecast") or {}).get("days") or []
        today_row = next((row for row in forecast_days if row.get("offset") == 0), None)
        horizon = None if today_row is None else (candidate - today_row["date"]).days
        vague = (
            " (ориентировочно)"
            if horizon is not None and horizon > rules_lib.VAGUE_AFTER_DAYS
            else ""
        )
        out.append(f"  возможная высокая вероятность: с {_iso(candidate)}{vague}")
    blockers = guidance.get("high_blockers") or []
    if blockers:
        out.append(
            "  почему сегодня не высокая: "
            + ", ".join(HIGH_BLOCKER_LABELS.get(str(item), str(item)) for item in blockers)
        )
    out.append(
        f"  T средняя 7 д: {_n(temp.get('mean_c'))} °C "
        f"({temp.get('covered_days', 0)}/{temp.get('expected_days', 7)} дн., "
        f"качество {temp.get('quality', 'missing')})"
    )
    if frost.get("minimum_c") is None:
        out.append("  заморозок 7 д: недостаточно данных")
    else:
        marker = "да" if frost.get("present") else "нет"
        out.append(
            f"  заморозок 7 д: {marker}; минимум {_n(frost.get('minimum_c'))} °C "
            f"{_iso(frost.get('date'))}"
        )
    if episode:
        growth = episode.get("growth_window") or [None, None]
        out.append(
            f"  дождевой эпизод: {_n(episode.get('total_mm'))} мм, максимум "
            f"{_iso(episode.get('date'))}; окно D+7...D+12 "
            f"{_iso(growth[0])}–{_iso(growth[1])}; качество {episode.get('quality')}"
        )
    else:
        out.append("  дождевой эпизод ≥ 20 мм / 3 д: не найден или недостаточно данных")
    out.append(
        f"  динамика API30: {_n(dynamics.get('value_mm'))} мм за {_iso(dynamics.get('date'))}, "
        f"Δ1д {_n(dynamics.get('delta_1d_mm'))} мм, Δ3д {_n(dynamics.get('delta_3d_mm'))} мм"
    )
    enough = "достаточна" if history.get("sufficient") else "недостаточна"
    out.append(
        f"  история API30: {enough}; покрытие {history.get('covered_days', 0)}/"
        f"{history.get('expected_days', 30)} дн., разрывов {history.get('gaps', 30)}"
    )
    return out


def _forecast_lines(view: Mapping[str, Any]) -> list[str]:
    fc = view.get("forecast") or {}
    rows = fc.get("days") or []
    threshold = fc.get("threshold_mm")
    out = [
        f"прогноз, сегодня + {max(len(rows) - 1, 0)} дн. "
        "(дата | +дн | дождь мм | T ср °C | T мин °C | API30 мм | оценка):"
    ]
    quality = str(fc.get("quality") or "missing")
    if quality in {"missing", "stale", "partial"}:
        out.append(f"  качество: {quality}; недостаточно данных для уверенного вывода")
    for row in rows:
        out.append(
            f"  {_iso(row['date'])} {('+' + str(row['offset'])):>4}"
            f" {_n(row['precip_mm']):>7} {_n(row['t_mean']):>7} {_n(row['t_min']):>7}"
            f" {_n(row['api30_mm']):>7} {row.get('verdict') or DASH}"
        )
    if not rows:
        out.append("  нет данных")

    cross = fc.get("cross")
    if quality != "fresh":
        out.append(f"  порог API30 {_n(threshold, 0)} мм: вывод недоступен")
    elif cross is not None:
        horizon = fc.get("cross_horizon")
        tail = "" if horizon is None else f" (через {horizon} дн.)"
        vague = (
            " — ориентировочно, горизонт > 7 дней"
            if horizon is not None and horizon > rules_lib.VAGUE_AFTER_DAYS
            else ""
        )
        out.append(f"  порог API30 {_n(threshold, 0)} мм: пересечение {_iso(cross)}{tail}{vague}")
    elif fc.get("above_threshold_today"):
        out.append(f"  порог API30 {_n(threshold, 0)} мм: уже пройден сегодня")
    else:
        out.append(f"  порог API30 {_n(threshold, 0)} мм: в горизонте прогноза не достигается")
    if fc.get("peak"):
        day, value = fc["peak"]
        out.append(f"  пик API30: {_n(value)} мм {_iso(day)}")
    if quality == "fresh" and fc.get("next_rain"):
        day, value = fc["next_rain"]
        out.append(f"  ближайший дождь ≥ {_n(rules_lib.NEXT_RAIN_MM, 0)} мм: {_n(value)} мм {_iso(day)}")
    elif quality == "fresh":
        out.append(f"  ближайший дождь ≥ {_n(rules_lib.NEXT_RAIN_MM, 0)} мм: нет в прогнозе")
    else:
        out.append(f"  ближайший дождь ≥ {_n(rules_lib.NEXT_RAIN_MM, 0)} мм: недостаточно данных")
    return out


def _render_location(view: Mapping[str, Any], today: date, stamp: str) -> list[str]:
    station_meta = ((view.get("station") or {}).get("meta")) or {}
    bits = []
    if station_meta.get("name"):
        bits.append(str(station_meta["name"]))
    if station_meta.get("elev_m") is not None:
        bits.append(f"{float(station_meta['elev_m']):.0f} м")
    if station_meta.get("distance_km") is not None:
        bits.append(f"{float(station_meta['distance_km']):.1f} км")
    station_line = ", ".join(bits) if bits else DASH

    lines = [
        f"🍄 {view['name']} ({view['lat']}, {view['lon']})",
        f"бриф {stamp} · станция ČHMÚ: {station_line}",
        "факты сегодня:",
    ]
    lines += _facts_lines(view)
    lines.append("")
    lines += _biological_lines(view)
    lines.append("")
    lines += _history_lines(view)
    lines.append("")

    # the crossing horizon is easier to compute here than to thread through
    fc = view.get("forecast") or {}
    if fc.get("cross") is not None:
        fc = dict(fc)
        fc["cross_horizon"] = (fc["cross"] - today).days
        view = dict(view)
        view["forecast"] = fc
    lines += _forecast_lines(view)
    lines.append("")

    signals = view.get("signals") or []
    if signals:
        fired = "; ".join(f"{s.get('trigger')}: {s.get('text')}" for s in signals)
    else:
        fired = "нет"
    lines.append(f"сработавшие триггеры (детерминированные, из rules.decide): {fired}")
    return lines


def render(brief: Mapping[str, Any]) -> str:
    """The brief as plain text -- this is literally what Hermes reads."""
    today = date.fromisoformat(str(brief["date"]))
    stamp = f"{brief['date']} {str(brief['generated_at'])[11:16]} {brief['timezone']}"
    out = [
        f"БРИФ ПО ГРИБАМ · {stamp} · порог API30 {_n(brief.get('threshold_mm'), 0)} мм",
        "",
    ]
    for view in brief.get("locations") or []:
        out += _render_location(view, today, stamp)
        out.append("")
    notes = brief.get("notes") or []
    out.append("сбои источников: " + ("; ".join(notes) if notes else "нет"))
    out.append("")
    out.append(str(brief.get("cheat_sheet") or CHEAT_SHEET))
    return "\n".join(out).rstrip() + "\n"


def to_json(brief: Mapping[str, Any]) -> dict[str, Any]:
    """JSON-safe copy of :func:`build`'s dict (dates -> ISO strings)."""
    return rules_lib._jsonable(dict(brief))
