"""The ready-to-send report: everything the messenger agent must not compute.

Why this exists
---------------
``brief`` prints every fact plus two tables; that is a debugging view.  The
Hermes Agent used to read it, apply the biology cheat sheet, compare today
against a durable notepad, and decide whether to write to Telegram at all.
All of that is deterministic work, so it belongs here:

* the verdicts, the phase and the outlook come out of the shared read model
  (``views``/``biology``) exactly as ``brief`` shows them;
* the send/silent decision is taken against the previous report stored in
  SQLite (``reports``), not against an agent's memory;
* the result is one short Russian block, already worded for a human.

The agent only obeys ``ОТПРАВЛЯТЬ`` and re-wraps the block as a Telegram
message.  It adds no number, no date and no interpretation.
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
    "SHIFT_DAYS",
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

#: A candidate high-probability date this close counts as "почти сейчас".
NEAR_DAYS = 7

#: A candidate date moving more than this many days is news.
SHIFT_DAYS = 2

#: Long location names do not fit a one-line summary.
SHORT_NAMES = {"valmez": "Valmez", "valasska-bystrice": "Bystřice"}

VERDICT_RANK = {"insufficient": 0, "low": 1, "medium": 2, "high": 3}

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


def short_name(slug: str, name: str) -> str:
    return SHORT_NAMES.get(slug) or name.split()[-1]


def _weekend_days(today: date) -> tuple[date, date]:
    """The upcoming Saturday and Sunday (today's own, if it is a weekend)."""
    weekday = today.weekday()
    if weekday == 6:  # Sunday: the weekend that is running now
        saturday = today - timedelta(days=1)
    else:
        saturday = today + timedelta(days=(5 - weekday) % 7)
    return saturday, saturday + timedelta(days=1)


def _days(count: int) -> str:
    """``1 день`` / ``3 дня`` / ``6 дней`` -- the block is read by a human."""
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return f"{count} дней"
    tail %= 10
    if tail == 1:
        return f"{count} день"
    if 2 <= tail <= 4:
        return f"{count} дня"
    return f"{count} дней"


def _vague(days: int | None) -> str:
    """Past a week the forecast is not trustworthy -- say so, every time."""
    return " (ориентировочно)" if days is not None and days > NEAR_DAYS else ""


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
    station = view.get("station") or {}
    forecast = view.get("forecast") or {}
    chmi = view.get("chmi") or {}
    houby = view.get("houbymapa") or {}

    api30 = station.get("api30_mm")
    if api30 is None:
        api30 = forecast.get("today_mm")

    candidate = guidance.get("candidate_high_date")
    if isinstance(candidate, str):
        candidate = date.fromisoformat(candidate)

    rows = {row["date"]: row for row in forecast.get("days") or []}
    weekend_verdicts = [
        {
            "date": day,
            "label": WEEKDAY_NAMES[index],
            "verdict_label": (rows.get(day) or {}).get("verdict")
            or policy.BIOLOGICAL_VERDICT_LABELS["insufficient"],
            "precip_mm": (rows.get(day) or {}).get("precip_mm"),
        }
        for index, day in enumerate(weekend)
    ]

    next_rain = forecast.get("next_rain")
    episode = bio.get("rain_episode") or {}
    growth = episode.get("growth_window") or [None, None]

    return {
        "slug": view["slug"],
        "name": view["name"],
        "short_name": short_name(str(view["slug"]), str(view["name"])),
        "verdict": guidance.get("verdict", "insufficient"),
        "verdict_label": guidance.get(
            "verdict_label", policy.BIOLOGICAL_VERDICT_LABELS["insufficient"]
        ),
        "phase": guidance.get("phase", "no_episode"),
        "event_id": guidance.get("dominant_event_id"),
        "episode": {
            "anchor": episode.get("date"),
            "primary_start": growth[0],
            "residual_end": episode.get("residual_window_end"),
        },
        "api30_mm": api30,
        "chmi_level": chmi.get("level"),
        "houbymapa_level": houby.get("level"),
        "houbymapa_score": houby.get("score"),
        "rain_7d_mm": _window_mm(station, 7),
        "candidate_high_date": candidate,
        "candidate_in_days": None if candidate is None else (candidate - today).days,
        "next_rain": next_rain,
        "weekend": weekend_verdicts,
        "weekend_rain_mm": (
            None
            if all(item["precip_mm"] is None for item in weekend_verdicts)
            else sum(item["precip_mm"] or 0.0 for item in weekend_verdicts)
        ),
        "horizon_days": max(len(rows) - 1, 0),
    }


def _phase_sentence(summaries: Sequence[Mapping[str, Any]]) -> str:
    """One plain sentence for the whole report: the strongest phase wins."""
    if not summaries:
        return "данных нет"
    best = max(summaries, key=lambda item: _PHASE_RANK.get(item["phase"], 0))
    phase = best["phase"]
    episode = best.get("episode") or {}
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


def _caveats(
    notes: Sequence[str], failed_sources: Sequence[str], error_class: str
) -> str:
    words = [SOURCE_WORDS.get(source, f"{source}: нет данных") for source in failed_sources]
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
    """Everything the block prints, computed once, in the script."""
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
        "phase_text": _phase_sentence(summaries),
        "earliest_candidate": min(candidates) if candidates else None,
        "error_class": klass,
        "rules_version": policy.RULES_VERSION,
        "caveats": _caveats(
            brief.get("notes") or (), failed_sources, klass
        ),
    }


def state_of(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a report that the next run compares itself against."""
    return {
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
def _candidate_reason(
    slug_name: str,
    previous_iso: str | None,
    previous_date: date,
    current: date | None,
    today: date,
) -> str | None:
    previous = None if not previous_iso else date.fromisoformat(str(previous_iso))
    previous_horizon = None if previous is None else (previous - previous_date).days
    horizon = None if current is None else (current - today).days
    if horizon is not None and horizon <= NEAR_DAYS and (
        previous_horizon is None or previous_horizon > NEAR_DAYS
    ):
        return f"{slug_name}: высокая вероятность вошла в ближайшие {_days(NEAR_DAYS)}"
    if previous_horizon is not None and previous_horizon <= NEAR_DAYS and horizon is None:
        return f"{slug_name}: окно высокой вероятности пропало из прогноза"
    if (
        previous is not None
        and current is not None
        and abs((current - previous).days) > SHIFT_DAYS
    ):
        return f"{slug_name}: дата окна сдвинулась на {_days(abs((current - previous).days))}"
    return None


def decide(
    store: Store, summary: Mapping[str, Any]
) -> tuple[bool, str]:
    """Two rules, both about change (PLAN §3); the weekend plan always goes.

    The comparison is always against the last report from an *earlier* date,
    so running the same mode twice on one day yields the same answer.
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

    previous_date = date.fromisoformat(str(previous["date"]))
    old_verdicts = json.loads(previous["verdicts_json"] or "{}")
    old_candidates = json.loads(previous["candidates_json"] or "{}")
    reasons: list[str] = []

    for item in summary["locations"]:
        slug = item["slug"]
        name = item["short_name"]
        before = old_verdicts.get(slug)
        now = state["verdicts"][slug]
        if before is not None and before != now:
            reasons.append(
                f"{name}: вердикт {policy.BIOLOGICAL_VERDICT_LABELS.get(before, before)}"
                f" → {policy.BIOLOGICAL_VERDICT_LABELS.get(now, now)}"
            )
        elif before is None:
            reasons.append(f"{name}: новая локация")

    for item in summary["locations"]:
        reason = _candidate_reason(
            item["short_name"],
            old_candidates.get(item["slug"]),
            previous_date,
            item["candidate_high_date"],
            today,
        )
        if reason:
            reasons.append(reason)

    if str(previous["error_class"]) != state["error_class"]:
        if state["error_class"] == "none":
            reasons.append("источники снова работают")
        else:
            reasons.append(
                "сбой данных: "
                + ("бриф не собрался" if state["error_class"] == "brief" else "станция ČHMÚ недоступна")
            )

    if reasons:
        return True, "; ".join(reasons)
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


def _outlook_tail(summary: Mapping[str, Any]) -> str:
    candidate = summary["earliest_candidate"]
    if candidate is None:
        return "окна в горизонте нет"
    days = (candidate - summary["date"]).days
    return f"высокая ожидается с {_dm(candidate)}{_vague(days)}"


def _best_weekend(summary: Mapping[str, Any]) -> str:
    """Name a best day only when one is actually better than the rest."""
    scored = [
        (
            VERDICT_RANK.get(_code_of(day["verdict_label"]), 0),
            item["short_name"],
            "суббота" if index == 0 else "воскресенье",
        )
        for item in summary["locations"]
        for index, day in enumerate(item["weekend"])
    ]
    if not scored:
        return "лучшего дня нет: данных нет"
    top = max(rank for rank, _, _ in scored)
    if top <= VERDICT_RANK["low"]:
        return "лучшего дня нет, везде слабо"
    leaders = [entry for entry in scored if entry[0] == top]
    if len(leaders) == len(scored):
        return "оба дня и обе локации одинаковы"
    if len(leaders) > 1:
        return "лучше: " + ", ".join(f"{name} ({day})" for _, name, day in leaders)
    return f"лучше: {leaders[0][1]}, {leaders[0][2]}"


def _code_of(label: str) -> str:
    for code, text in policy.BIOLOGICAL_VERDICT_LABELS.items():
        if text == label:
            return code
    return "insufficient"


def _summary_line(summary: Mapping[str, Any]) -> str:
    if summary["mode"] == WEEKEND:
        parts = []
        for index in (0, 1):
            day_label = "сб" if index == 0 else "вс"
            verdicts = ", ".join(
                f"{item['short_name']} — {item['weekend'][index]['verdict_label']}"
                for item in summary["locations"]
            )
            parts.append(f"{day_label}: {verdicts}")
        parts.append(_best_weekend(summary))
        return "; ".join(parts)
    verdicts = ", ".join(
        f"{item['short_name']} — {item['verdict_label']}"
        for item in summary["locations"]
    )
    return f"{verdicts}; {_outlook_tail(summary)}"


def _facts(item: Mapping[str, Any]) -> str:
    bits = [f"API30 {_mm(item['api30_mm'])}"]
    level = _level(item["chmi_level"])
    if level:
        bits.append(f"карта ČHMÚ {level}")
    houby = _level(item["houbymapa_level"])
    if houby and item["houbymapa_score"] is not None:
        bits.append(f"HoubyMapa {houby} ({item['houbymapa_score']:.2f})")
    elif houby:
        bits.append(f"HoubyMapa {houby}")
    if item["rain_7d_mm"] is not None:
        bits.append(f"за 7 дней {_mm(item['rain_7d_mm'])}")
    return ", ".join(bits)


def _prospect(item: Mapping[str, Any]) -> str:
    days = item["candidate_in_days"]
    if item["candidate_high_date"] is None:
        return f"нет в горизонте {_days(item['horizon_days'])}"
    if days is not None and days <= 0:
        return "высокая уже сегодня"
    return f"высокая через {_days(days)} ({_dm(item['candidate_high_date'])}){_vague(days)}"


def _location_lines(item: Mapping[str, Any], mode: str) -> list[str]:
    out = [f"ЛОКАЦИЯ {item['name']}:"]
    if mode == WEEKEND:
        days = "; ".join(
            f"{day['label']} {_dm(day['date'])}: {day['verdict_label']}"
            for day in item["weekend"]
        )
        out.append(f"  {days} — {_facts(item)}")
        out.append(f"  перспектива: {_prospect(item)}")
        rain = item["weekend_rain_mm"]
        if rain is None:
            out.append("  дождь на выходные: данных нет")
        elif rain < 1.0:
            out.append("  дождь на выходные: не ожидается")
        else:
            detail = ", ".join(
                f"{day['label']} {_mm(day['precip_mm'])}"
                for day in item["weekend"]
                if (day["precip_mm"] or 0.0) >= 1.0
            )
            out.append(f"  дождь на выходные: {_mm(rain)} ({detail})")
        return out
    out.append(f"  сегодня: {item['verdict_label']} — {_facts(item)}")
    out.append(f"  перспектива: {_prospect(item)}")
    if item["next_rain"]:
        day, value = item["next_rain"]
        out.append(
            f"  дождь: ближайший ≥ {policy.NEXT_RAIN_MM:.0f} мм — {_mm(value)} {_dm(day)}"
        )
    else:
        out.append(
            f"  дождь: ближайший ≥ {policy.NEXT_RAIN_MM:.0f} мм не ожидается"
        )
    return out


def render(summary: Mapping[str, Any], *, send: bool, reason: str) -> str:
    """The whole block.  Every number and date in it is final."""
    lines = [
        f"ОТПРАВЛЯТЬ: {'да' if send else 'нет'}",
        f"ПРИЧИНА: {reason}",
        f"ЗАГОЛОВОК: {_header(summary)}",
    ]
    if summary["error_class"] == "brief":
        lines.append("ИТОГ: данных нет, прогноз не собрался")
        lines.append("ФАЗА: данных нет")
        lines.append(f"ОГОВОРКИ: {summary['caveats']}")
        return "\n".join(lines) + "\n"
    lines.append(f"ИТОГ: {_summary_line(summary)}")
    lines.append(f"ФАЗА: {summary['phase_text']}")
    for item in summary["locations"]:
        lines += _location_lines(item, str(summary["mode"]))
    lines.append(f"ОГОВОРКИ: {summary['caveats']}")
    return "\n".join(lines) + "\n"


def to_json(
    summary: Mapping[str, Any], *, send: bool, reason: str
) -> dict[str, Any]:
    """The same fields, machine-readable, for tests and for debugging."""
    payload = {
        "mode": summary["mode"],
        "date": summary["date"],
        "send": send,
        "reason": reason,
        "header": _header(summary),
        "summary": (
            "данных нет, прогноз не собрался"
            if summary["error_class"] == "brief"
            else _summary_line(summary)
        ),
        "phase_text": summary["phase_text"],
        "error_class": summary["error_class"],
        "rules_version": summary["rules_version"],
        "caveats": summary["caveats"],
        "earliest_candidate": summary["earliest_candidate"],
        "locations": [
            {
                "slug": item["slug"],
                "name": item["name"],
                "short_name": item["short_name"],
                "verdict": item["verdict"],
                "verdict_label": item["verdict_label"],
                "facts": _facts(item),
                "prospect": _prospect(item),
                "candidate_high_date": item["candidate_high_date"],
                "next_rain": item["next_rain"],
                "weekend": item["weekend"],
                "lines": _location_lines(item, str(summary["mode"]))[1:],
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
        verdicts=state["verdicts"],
        candidates=state["candidates"],
        events=state["events"],
        error_class=state["error_class"],
        rules_version=state["rules_version"],
        sent=send,
        reason=reason,
    )
    return send, reason
