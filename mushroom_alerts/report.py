"""The ready-to-send report: everything the messenger agent must not compute.

Why this exists
---------------
``brief`` prints every fact plus two tables; that is a debugging view.  The
Hermes Agent used to read it, apply the biology cheat sheet, compare today
against a durable notepad, and decide whether to write to Telegram at all.
All of that is deterministic work, so it belongs here:

* the numbers, the phase and the caveats come out of the shared read model
  (``views``/``biology``/``chance``) exactly as ``brief`` shows them;
* the send/silent decision is taken against the previous report stored in
  SQLite (``reports``), not against an agent's memory;
* the result is one short Russian block, already worded for a human.

The agent only obeys ``ОТПРАВЛЯТЬ`` and re-wraps the block as a Telegram
message.  It adds no number, no date and no interpretation.

What the block says (PLAN §9)
-----------------------------
The user watches up to twenty forests within 50 km and decides for himself
where to drive: he knows which road is easy and where he has found things
before, and the script knows none of that.  So the block is a list of
"location — chance, %" **in the order of** ``locations.yaml``, never
sorted, never advising.  The single place where the script does order
something is picking the two locations that get a technical line under
``ПОДРОБНО``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping, Sequence

import json

from . import policy
from . import rules as rules_lib
from .store import Store

__all__ = [
    "MODES",
    "DAILY",
    "WEEKEND",
    "NEAR_DAYS",
    "DETAIL_LOCATIONS",
    "error_class_of",
    "summarize",
    "decide",
    "render",
    "to_json",
    "publish",
]

DAILY = "daily"
WEEKEND = "weekend"
MODES = (DAILY, WEEKEND)

#: Past this horizon the forecast is a guess and the block says so.
NEAR_DAYS = 7

#: How many locations get a technical line.  Two keeps the block readable
#: whether the user watches two forests or twenty.
DETAIL_LOCATIONS = 2

#: Above this many, a caveat counts locations instead of naming them.
CAVEAT_NAMES = 3

#: ``ПРИЧИНА`` is a diagnostic line, never shown to the human; with twenty
#: locations it must still not outgrow the block it explains.
REASON_ITEMS = 4

_PHASE_RANK = {
    "no_episode": 0,
    "expired": 1,
    "waiting": 2,
    "residual_window": 3,
    "primary_window": 4,
}

WEEKDAY_NAMES = ("сб", "вс")

SOURCE_WORDS = {
    "chmi_map": "карта ČHMÚ недоступна",
    "houbymapa": "HoubyMapa недоступна",
    "chmi_station": "станция ČHMÚ недоступна",
    "openmeteo": "прогноз Open-Meteo недоступен",
    "api30_forecast": "прогнозная кривая API30 не рассчитана",
}


# ----------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------
def _dm(day: date | None) -> str:
    """Dates are spoken, never ISO: ``12.09``."""
    return "—" if day is None else f"{day.day:02d}.{day.month:02d}"


def _mm(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} мм"


def _level(value: float | None) -> str | None:
    return None if value is None else f"{value:.0f}/5"


def _pct(value: int | None) -> str:
    return "нет данных" if value is None else f"{value} %"


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _weekend_days(today: date) -> tuple[date, date]:
    """The upcoming Saturday and Sunday (today's own, if it is a weekend)."""
    weekday = today.weekday()
    if weekday == 6:  # Sunday: the weekend that is running now
        saturday = today - timedelta(days=1)
    else:
        saturday = today + timedelta(days=(5 - weekday) % 7)
    return saturday, saturday + timedelta(days=1)


def _vague(days: int | None) -> str:
    """Past a week the forecast is not trustworthy -- say so, every time."""
    return " (ориентировочно)" if days is not None and days > NEAR_DAYS else ""


def _locations_word(count: int) -> str:
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return "локаций"
    tail %= 10
    if tail == 1:
        return "локация"
    if 2 <= tail <= 4:
        return "локации"
    return "локаций"


# ----------------------------------------------------------------------
# gathering: a compact view of one brief
# ----------------------------------------------------------------------
def error_class_of(
    *,
    all_failed: bool,
    calculation_error: str | None,
    failed_sources: Sequence[str],
) -> str:
    """``none`` | ``station`` | ``brief`` -- the only failures worth a message.

    A dead ČHMÚ map or HoubyMapa is a caveat, not news: it never changes the
    class and therefore never triggers a send on its own.
    """
    if all_failed or calculation_error:
        return "brief"
    if "chmi_station" in set(failed_sources):
        return "station"
    return "none"


def _window_mm(station: Mapping[str, Any] | None, days: int) -> float | None:
    for window in (station or {}).get("sra") or []:
        if window.get("window_days") == days:
            return window.get("mm")
    return None


def _location_summary(
    view: Mapping[str, Any], today: date, weekend: tuple[date, date]
) -> dict[str, Any]:
    bio = view.get("biological") or {}
    guidance = bio.get("guidance") or {}
    chance = bio.get("chance") or {}
    station = view.get("station") or {}
    forecast = view.get("forecast") or {}
    chmi = view.get("chmi") or {}
    houby = view.get("houbymapa") or {}

    # Two different API30 numbers exist for today, and the block used to
    # print the wrong one: the chance is computed from the derived forecast
    # curve, while the station reports what it measured.  They differ by a
    # lot (36 mm against 24 mm on 12.09.2026), so the reader could not
    # reproduce the percentage from the line that was meant to explain it.
    api30 = forecast.get("today_mm")
    api30_station = station.get("api30_mm")

    candidate = _as_date(guidance.get("candidate_high_date"))

    curve = {
        day: int(value)
        for raw, value in (chance.get("curve") or {}).items()
        if value is not None and (day := _as_date(raw)) is not None
    }
    peak_raw = chance.get("peak")
    peak = (
        None
        if not peak_raw
        else (_as_date(peak_raw[0]), None if peak_raw[1] is None else int(peak_raw[1]))
    )
    today_chance = None if chance.get("today") is None else int(chance["today"])

    rows = {row["date"]: row for row in forecast.get("days") or []}
    weekend_days = [
        {
            "date": day,
            "label": WEEKDAY_NAMES[index],
            "chance": curve.get(day),
            "verdict_label": (rows.get(day) or {}).get("verdict")
            or policy.BIOLOGICAL_VERDICT_LABELS["insufficient"],
        }
        for index, day in enumerate(weekend)
    ]

    episode = bio.get("rain_episode") or {}
    growth = episode.get("growth_window") or [None, None]

    return {
        "slug": view["slug"],
        "name": view["name"],
        "short_name": str(view.get("short_name") or view["name"]),
        "verdict": guidance.get("verdict", "insufficient"),
        "verdict_label": guidance.get(
            "verdict_label", policy.BIOLOGICAL_VERDICT_LABELS["insufficient"]
        ),
        "chance": today_chance,
        "chance_curve": curve,
        "chance_peak": peak,
        "chance_capped": bool(chance.get("capped")),
        "phase": guidance.get("phase", "no_episode"),
        "event_id": guidance.get("dominant_event_id"),
        "episode": {
            "anchor": episode.get("date"),
            "primary_start": growth[0],
            "residual_end": episode.get("residual_window_end"),
        },
        "api30_mm": api30,  # what the chance was computed from
        "api30_station_mm": api30_station,  # what the station measured
        "chmi_level": chmi.get("level"),
        "houbymapa_level": houby.get("level"),
        "houbymapa_score": houby.get("score"),
        "rain_7d_mm": _window_mm(station, 7),
        "candidate_high_date": candidate,
        "next_rain": forecast.get("next_rain"),
        "weekend": weekend_days,
    }


def _phase_sentence(item: Mapping[str, Any]) -> str:
    """One plain sentence about the rain cycle of one location."""
    phase = item["phase"]
    episode = item.get("episode") or {}
    if phase == "waiting":
        return (
            f"дождь прошёл {_dm(episode.get('anchor'))}, "
            f"условия для роста ожидаются с {_dm(episode.get('primary_start'))}"
        )
    if phase == "primary_window":
        return "расчётное окно роста идёт, стоит проверить лес"
    if phase == "residual_window":
        return (
            "основное окно прошло, остаточная вероятность держится до "
            f"{_dm(episode.get('residual_end'))}"
        )
    if phase == "expired":
        return "окно закончилось, ждём следующего дождя"
    return "подходящего дождя не было"


def _headline_phase(summaries: Sequence[Mapping[str, Any]]) -> str:
    """The strongest phase across locations -- one line for the whole block."""
    if not summaries:
        return "данных нет"
    return _phase_sentence(max(summaries, key=lambda item: _PHASE_RANK.get(item["phase"], 0)))


def _caveats(
    summaries: Sequence[Mapping[str, Any]],
    notes: Sequence[str],
    failed_sources: Sequence[str],
    error_class: str,
) -> str:
    words = [SOURCE_WORDS.get(source, f"{source}: нет данных") for source in failed_sources]
    capped = [item["short_name"] for item in summaries if item["chance_capped"]]
    if capped:
        # Naming twenty locations would be longer than the list itself.
        who = (
            ", ".join(capped)
            if len(capped) <= CAVEAT_NAMES
            else f"{len(capped)} {_locations_word(len(capped))}"
        )
        words.append(
            f"без свежей станции шанс ограничен {policy.CHANCE_NO_STATION_CAP} %: {who}"
        )
    if error_class == "brief" and not words:
        words.append("бриф не собрался")
    if not words and notes:
        words.append("часть данных пришла с ошибкой")
    return ", ".join(dict.fromkeys(words)) if words else "нет"


def summarize(
    brief: Mapping[str, Any],
    *,
    mode: str,
    today: date,
    calculation_error: str | None = None,
    all_failed: bool = False,
) -> dict[str, Any]:
    """Everything the block prints, computed once, in the script.

    The location order is the order of ``brief["locations"]``, which is the
    order of ``locations.yaml``.  Nothing in here sorts it.
    """
    if mode not in MODES:
        raise ValueError(f"unknown report mode: {mode}")
    weekend = _weekend_days(today)
    views = list(brief.get("locations") or [])
    summaries = [_location_summary(view, today, weekend) for view in views]
    failed_sources = list(brief.get("failed_sources") or [])
    klass = error_class_of(
        all_failed=all_failed,
        calculation_error=calculation_error,
        failed_sources=failed_sources,
    )
    candidates = [
        item["candidate_high_date"]
        for item in summaries
        if item["candidate_high_date"] is not None
    ]
    return {
        "mode": mode,
        "date": today,
        "weekend": list(weekend),
        "locations": summaries,
        "phase_text": _headline_phase(summaries),
        "earliest_candidate": min(candidates) if candidates else None,
        "error_class": klass,
        "rules_version": policy.RULES_VERSION,
        "caveats": _caveats(
            summaries, brief.get("notes") or (), failed_sources, klass
        ),
    }


def state_of(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a report that the next run compares itself against."""
    return {
        "chances": {item["slug"]: item["chance"] for item in summary["locations"]},
        "verdicts": {item["slug"]: item["verdict"] for item in summary["locations"]},
        "candidates": {
            item["slug"]: (
                None
                if item["candidate_high_date"] is None
                else item["candidate_high_date"].isoformat()
            )
            for item in summary["locations"]
        },
        "events": {item["slug"]: item["event_id"] for item in summary["locations"]},
        "error_class": summary["error_class"],
        "rules_version": summary["rules_version"],
    }


# ----------------------------------------------------------------------
# the send / silent decision
# ----------------------------------------------------------------------
def _best(chances: Mapping[str, Any]) -> int | None:
    values = [int(value) for value in chances.values() if value is not None]
    return max(values) if values else None


def _reason(reasons: Sequence[str]) -> str:
    if len(reasons) <= REASON_ITEMS:
        return "; ".join(reasons)
    return "; ".join(reasons[:REASON_ITEMS]) + f"; ещё изменений: {len(reasons) - REASON_ITEMS}"


def _crossing_reason(previous: int | None, current: int | None) -> str | None:
    """The best chance crossing :data:`policy.CHANCE_ALERT_PCT` is news."""
    threshold = policy.CHANCE_ALERT_PCT
    was = previous is not None and previous >= threshold
    now = current is not None and current >= threshold
    if was == now:
        return None
    if now:
        return f"лучший шанс {current} %, выше {threshold} %"
    return f"лучший шанс упал до {_pct(current)}, ниже {threshold} %"


def decide(store: Store, summary: Mapping[str, Any]) -> tuple[bool, str]:
    """Send when the numbers moved (PLAN §9d); the weekend plan always goes.

    With twenty locations the old "any verdict changed" rule fired almost
    every day, because a verdict is a coarse word.  A percentage moves
    smoothly, so the rule is about the size of the move: ten points
    anywhere, or the best location crossing 60 % in either direction.

    The comparison is always against the last report from an *earlier*
    date, so running the same mode twice on one day yields the same answer.
    """
    mode = str(summary["mode"])
    today: date = summary["date"]
    state = state_of(summary)

    if mode == WEEKEND:
        if state["error_class"] == "brief":
            return True, "плана нет: бриф не собрался"
        return True, "плановый прогноз на выходные"

    previous = store.last_report(mode, before=today)
    if previous is None:
        return True, "первый запуск, предыдущего отчёта нет"

    old_chances = json.loads(previous["chances_json"] or "{}")
    reasons: list[str] = []

    for item in summary["locations"]:
        name = item["short_name"]
        now = state["chances"][item["slug"]]
        before = old_chances.get(item["slug"])
        if before is None:
            if now is not None:
                reasons.append(f"{name}: новая локация, шанс {now} %")
            continue
        if now is None:
            reasons.append(f"{name}: шанс больше не считается")
        elif abs(now - int(before)) >= policy.CHANCE_MOVE_PCT:
            reasons.append(f"{name}: шанс {int(before)} % → {now} %")

    crossing = _crossing_reason(_best(old_chances), _best(state["chances"]))
    if crossing:
        reasons.append(crossing)

    if str(previous["error_class"]) != state["error_class"]:
        if state["error_class"] == "none":
            reasons.append("источники снова работают")
        else:
            reasons.append(
                "сбой данных: "
                + ("бриф не собрался" if state["error_class"] == "brief" else "станция ČHMÚ недоступна")
            )

    if reasons:
        return True, _reason(reasons)
    return False, "ничего не изменилось с прошлого отчёта"


# ----------------------------------------------------------------------
# rendering: the block the agent re-wraps, and nothing else
# ----------------------------------------------------------------------
def _header(summary: Mapping[str, Any]) -> str:
    if summary["mode"] == WEEKEND:
        saturday, sunday = summary["weekend"]
        if saturday.month == sunday.month:
            span = f"{saturday.day:02d}–{_dm(sunday)}"
        else:
            span = f"{_dm(saturday)}–{_dm(sunday)}"
        return f"🍄 Грибной прогноз на выходные {span}"
    return f"🍄 Грибной прогноз: {_dm(summary['date'])}"


def _chance_title(summary: Mapping[str, Any]) -> str:
    if summary["mode"] == WEEKEND:
        return "ШАНС на выходные"
    return f"ШАНС на {_dm(summary['date'])}"


def _chance_line(item: Mapping[str, Any], mode: str) -> str:
    """One location, one line -- twenty of these must still fit a message."""
    if mode == WEEKEND:
        days = ", ".join(
            f"{day['label']} {_pct(day['chance'])}" for day in item["weekend"]
        )
        return f"{item['short_name']} — {days}"
    return f"{item['short_name']} — {_pct(item['chance'])}"


def _rank(item: Mapping[str, Any], mode: str) -> int | None:
    """What ``ПОДРОБНО`` is chosen by: the best day shown for the location."""
    if mode == WEEKEND:
        values = [day["chance"] for day in item["weekend"] if day["chance"] is not None]
        return max(values) if values else None
    return item["chance"]


def _leaders(summary: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The best locations by chance; a tie is broken by the config order."""
    mode = str(summary["mode"])
    ordered = sorted(
        enumerate(summary["locations"]),
        key=lambda pair: (-(_rank(pair[1], mode) or -1), pair[0]),
    )
    return [item for _, item in ordered[:DETAIL_LOCATIONS]]


def _api30_fact(item: Mapping[str, Any]) -> str:
    """``API30 36 мм (расчёт), станция 24 мм`` -- both, never one as both.

    The chance uses the derived curve, so that number comes first and says
    so; the measurement follows, because it is the one the cap is about.
    One line either way.
    """
    used, measured = item["api30_mm"], item["api30_station_mm"]
    if used is None:
        return "API30 —" if measured is None else f"API30 {_mm(measured)} (станция)"
    if measured is None:
        return f"API30 {_mm(used)} (расчёт), станции нет"
    return f"API30 {_mm(used)} (расчёт), станция {_mm(measured)}"


def _facts(item: Mapping[str, Any]) -> str:
    bits = [_api30_fact(item)]
    houby = _level(item["houbymapa_level"])
    if houby and item["houbymapa_score"] is not None:
        bits.append(f"HoubyMapa {houby} ({item['houbymapa_score']:.2f})")
    elif houby:
        bits.append(f"HoubyMapa {houby}")
    level = _level(item["chmi_level"])
    if level:
        bits.append(f"карта ČHMÚ {level}")
    if item["rain_7d_mm"] is not None:
        bits.append(f"за 7 дней {_mm(item['rain_7d_mm'])}")
    return ", ".join(bits)


def _peak_tail(item: Mapping[str, Any], summary: Mapping[str, Any]) -> str:
    """``максимум 70 % 18.09`` -- the same curve, not a separate rule.

    Silent when the best day is already on the block (today, or one of the
    two weekend days) or is no better than what the block shows.
    """
    peak = item["chance_peak"]
    if not peak or peak[0] is None or peak[1] is None:
        return ""
    today: date = summary["date"]
    shown = {today}
    reference = item["chance"]
    if str(summary["mode"]) == WEEKEND:
        shown |= {day["date"] for day in item["weekend"]}
        values = [day["chance"] for day in item["weekend"] if day["chance"] is not None]
        reference = max(values) if values else reference
    if peak[0] in shown or (reference is not None and peak[1] <= reference):
        return ""
    return f"; максимум {peak[1]} % {_dm(peak[0])}{_vague((peak[0] - today).days)}"


def _detail_line(
    item: Mapping[str, Any], summary: Mapping[str, Any], headline_phase: str
) -> str:
    mode = str(summary["mode"])
    if mode == WEEKEND:
        best = max(
            item["weekend"],
            key=lambda day: (day["chance"] if day["chance"] is not None else -1),
        )
        head = f"{item['short_name']} {_pct(best['chance'])} ({best['label']})"
    else:
        head = f"{item['short_name']} {_pct(item['chance'])}"
    line = f"{head}: {_facts(item)}{_peak_tail(item, summary)}"
    own_phase = _phase_sentence(item)
    if own_phase != headline_phase:
        line += f"; фаза: {own_phase}"
    return line


def _detail_lines(summary: Mapping[str, Any]) -> list[str]:
    headline = str(summary["phase_text"])
    return [_detail_line(item, summary, headline) for item in _leaders(summary)]


def render(summary: Mapping[str, Any], *, send: bool, reason: str) -> str:
    """The whole block.  Every number and date in it is final."""
    lines = [
        f"ОТПРАВЛЯТЬ: {'да' if send else 'нет'}",
        f"ПРИЧИНА: {reason}",
        f"ЗАГОЛОВОК: {_header(summary)}",
    ]
    if summary["error_class"] == "brief":
        lines.append("ШАНС: данных нет, прогноз не собрался")
        lines.append("ФАЗА: данных нет")
        lines.append(f"ОГОВОРКИ: {summary['caveats']}")
        return "\n".join(lines) + "\n"

    mode = str(summary["mode"])
    lines.append(f"{_chance_title(summary)}:")
    for item in summary["locations"]:  # locations.yaml order, never sorted
        lines.append(f"  {_chance_line(item, mode)}")
    lines.append(f"ФАЗА: {summary['phase_text']}")
    details = _detail_lines(summary)
    if details:
        lines.append(
            f"ПОДРОБНО ({len(details)} {_locations_word(len(details))} с лучшим шансом):"
        )
        lines += [f"  {line}" for line in details]
    lines.append(f"ОГОВОРКИ: {summary['caveats']}")
    return "\n".join(lines) + "\n"


def to_json(
    summary: Mapping[str, Any], *, send: bool, reason: str
) -> dict[str, Any]:
    """The same fields, machine-readable, for tests and for debugging."""
    broken = summary["error_class"] == "brief"
    mode = str(summary["mode"])
    leaders = {item["slug"] for item in _leaders(summary)}
    payload = {
        "mode": summary["mode"],
        "date": summary["date"],
        "send": send,
        "reason": reason,
        "header": _header(summary),
        "chance_title": _chance_title(summary),
        "chances": (
            []
            if broken
            else [_chance_line(item, mode) for item in summary["locations"]]
        ),
        "detail": [] if broken else _detail_lines(summary),
        "phase_text": "данных нет" if broken else summary["phase_text"],
        "error_class": summary["error_class"],
        "rules_version": summary["rules_version"],
        "caveats": summary["caveats"],
        "earliest_candidate": summary["earliest_candidate"],
        "locations": [
            {
                "slug": item["slug"],
                "name": item["name"],
                "short_name": item["short_name"],
                "chance": item["chance"],
                "chance_peak": item["chance_peak"],
                "chance_capped": item["chance_capped"],
                "verdict": item["verdict"],
                "verdict_label": item["verdict_label"],
                "phase": item["phase"],
                "facts": _facts(item),
                "weekend": item["weekend"],
                "detailed": item["slug"] in leaders,
                "candidate_high_date": item["candidate_high_date"],
                "next_rain": item["next_rain"],
            }
            for item in summary["locations"]
        ],
        "text": render(summary, send=send, reason=reason),
    }
    return rules_lib._jsonable(payload)


def publish(store: Store, summary: Mapping[str, Any]) -> tuple[bool, str]:
    """Decide, persist the decision, and hand the answer back."""
    send, reason = decide(store, summary)
    state = state_of(summary)
    store.save_report(
        summary["date"],
        str(summary["mode"]),
        chances=state["chances"],
        verdicts=state["verdicts"],
        candidates=state["candidates"],
        events=state["events"],
        error_class=state["error_class"],
        rules_version=state["rules_version"],
        sent=send,
        reason=reason,
    )
    return send, reason
