"""``report``: the human block and the send/silent decision.

The decision used to live in an LLM prompt and a durable notepad. Here it is
ordinary code, so it can be pinned down: what makes a message, what keeps
quiet, and what a second run on the same day must not change.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from mushroom_alerts import report as report_lib
from mushroom_alerts.store import Store

TODAY = date(2026, 9, 12)  # a Saturday; the weekend is today and tomorrow
FRIDAY = date(2026, 9, 11)

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ----------------------------------------------------------------------
# a brief payload, reduced to the fields ``report`` reads
# ----------------------------------------------------------------------
def location(
    slug="valmez",
    name="Valašské Meziříčí",
    *,
    verdict="medium",
    phase="waiting",
    candidate=None,
    forecast_verdicts=("средняя", "средняя"),
    today=TODAY,
):
    anchor = today - timedelta(days=1)
    days = [
        {
            "date": today + timedelta(days=n),
            "offset": n,
            "precip_mm": 6.0 if n in (5, 6) else 0.0,
            "verdict": "средняя",
        }
        for n in range(0, 17)
    ]
    for offset, label in enumerate(forecast_verdicts):
        # today and tomorrow are the weekend in these fixtures
        days[offset]["verdict"] = label
    return {
        "slug": slug,
        "name": name,
        "chmi": {"level": 3.0},
        "houbymapa": {"level": 4.0, "score": 0.63},
        "station": {
            "api30_mm": 23.7,
            "sra": [
                {"window_days": 1, "mm": 0.0},
                {"window_days": 7, "mm": 25.6},
            ],
        },
        "forecast": {
            "days": days,
            "today_mm": 23.7,
            "next_rain": (today + timedelta(days=5), 27.4),
        },
        "biological": {
            "rain_episode": {
                "date": anchor,
                "growth_window": [anchor + timedelta(days=7), anchor + timedelta(days=12)],
                "residual_window_end": anchor + timedelta(days=21),
            },
            "guidance": {
                "verdict": verdict,
                "verdict_label": {
                    "high": "высокая",
                    "medium": "средняя",
                    "low": "низкая",
                    "insufficient": "недостаточно данных",
                }[verdict],
                "phase": phase,
                "dominant_event_id": "rain-abc",
                "candidate_high_date": candidate,
            },
        },
    }


def brief(*locations, notes=(), failed=()):
    return {
        "locations": list(locations) or [location()],
        "notes": list(notes),
        "failed_sources": list(failed),
    }


def summary(mode="daily", *, today=TODAY, payload=None, **kw):
    return report_lib.summarize(
        payload or brief(), mode=mode, today=today, **kw
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s.sqlite") as db:
        yield db


# ----------------------------------------------------------------------
# the decision
# ----------------------------------------------------------------------
def test_first_run_always_sends(store):
    send, reason = report_lib.publish(store, summary())
    assert send is True
    assert "первый запуск" in reason


def test_an_unchanged_day_stays_silent(store):
    report_lib.publish(store, summary(today=FRIDAY))
    send, reason = report_lib.publish(store, summary(today=TODAY))
    assert send is False
    assert reason == "ничего не изменилось с прошлого отчёта"


def test_a_changed_verdict_sends(store):
    report_lib.publish(store, summary(today=FRIDAY))
    changed = summary(
        today=TODAY, payload=brief(location(verdict="high", phase="primary_window"))
    )
    send, reason = report_lib.publish(store, changed)
    assert send is True
    assert "средняя → высокая" in reason


def test_a_one_day_shift_of_the_window_is_not_news(store):
    report_lib.publish(
        store,
        summary(
            today=FRIDAY,
            payload=brief(location(candidate=FRIDAY + timedelta(days=5), today=FRIDAY)),
        ),
    )
    send, _ = report_lib.publish(
        store,
        summary(
            today=TODAY, payload=brief(location(candidate=FRIDAY + timedelta(days=6)))
        ),
    )
    assert send is False


def test_a_three_day_shift_of_the_window_sends(store):
    report_lib.publish(
        store,
        summary(
            today=FRIDAY,
            payload=brief(location(candidate=FRIDAY + timedelta(days=5), today=FRIDAY)),
        ),
    )
    send, reason = report_lib.publish(
        store,
        summary(
            today=TODAY, payload=brief(location(candidate=FRIDAY + timedelta(days=8)))
        ),
    )
    assert send is True
    assert "сдвинулась на 3 дня" in reason


def test_a_window_entering_the_next_week_sends(store):
    report_lib.publish(
        store,
        summary(
            today=FRIDAY,
            payload=brief(location(candidate=FRIDAY + timedelta(days=14), today=FRIDAY)),
        ),
    )
    send, reason = report_lib.publish(
        store,
        summary(
            today=TODAY, payload=brief(location(candidate=TODAY + timedelta(days=6)))
        ),
    )
    assert send is True
    assert "вошла в ближайшие 7 дней" in reason


def test_a_new_error_class_sends_once(store):
    report_lib.publish(store, summary(today=FRIDAY))
    broken = brief(location(), failed=["chmi_station"], notes=["станция: 502"])
    send, reason = report_lib.publish(
        store, summary(today=TODAY, payload=broken)
    )
    assert send is True and "станция ČHMÚ недоступна" in reason

    tomorrow = TODAY + timedelta(days=1)
    still = report_lib.summarize(broken, mode="daily", today=tomorrow)
    send, _ = report_lib.publish(store, still)
    assert send is False


def test_a_dead_map_is_a_caveat_not_a_reason_to_write(store):
    report_lib.publish(store, summary(today=FRIDAY))
    payload = brief(location(), failed=["houbymapa"], notes=["HoubyMapa: 502"])
    item = report_lib.summarize(payload, mode="daily", today=TODAY)
    send, _ = report_lib.publish(store, item)
    assert send is False
    assert item["error_class"] == "none"
    assert item["caveats"] == "HoubyMapa недоступна"


def test_rerunning_the_same_day_is_idempotent(store):
    report_lib.publish(store, summary(today=FRIDAY))
    first = report_lib.publish(store, summary(today=TODAY))
    second = report_lib.publish(store, summary(today=TODAY))
    assert first == second
    rows = store.conn.execute(
        "SELECT COUNT(*) FROM reports WHERE mode='daily'"
    ).fetchone()[0]
    assert rows == 2  # Friday and today, not three


def test_the_weekend_plan_always_goes_out(store):
    for day in (FRIDAY, TODAY):
        send, reason = report_lib.publish(
            store, summary("weekend", today=day, payload=brief(location(today=day)))
        )
        assert send is True
        assert reason == "плановый прогноз на выходные"


def test_the_weekend_plan_reports_a_broken_brief(store):
    item = report_lib.summarize(
        brief(location(), failed=["chmi_station", "openmeteo"]),
        mode="weekend",
        today=TODAY,
        all_failed=True,
    )
    send, reason = report_lib.publish(store, item)
    assert send is True and "бриф не собрался" in reason
    text = report_lib.render(item, send=send, reason=reason)
    assert "ИТОГ: данных нет" in text
    assert "ЛОКАЦИЯ" not in text


def test_the_daily_and_weekend_states_do_not_mix(store):
    report_lib.publish(store, summary("weekend", today=FRIDAY))
    send, reason = report_lib.publish(store, summary("daily", today=TODAY))
    assert send is True and "первый запуск" in reason


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        report_lib.summarize(brief(), mode="hourly", today=TODAY)


# ----------------------------------------------------------------------
# the block
# ----------------------------------------------------------------------
def test_the_daily_block_has_every_field_and_no_iso_dates():
    item = summary(
        payload=brief(
            location(candidate=TODAY + timedelta(days=6)),
            location(
                slug="valasska-bystrice",
                name="Valašská Bystřice",
                candidate=TODAY + timedelta(days=6),
            ),
        )
    )
    text = report_lib.render(item, send=True, reason="тест")
    assert text.startswith("ОТПРАВЛЯТЬ: да\nПРИЧИНА: тест\n")
    assert "ЗАГОЛОВОК: 🍄 Грибной прогноз: 12.09" in text
    assert "ИТОГ: Valmez — средняя, Bystřice — средняя; высокая ожидается с 18.09" in text
    assert "ФАЗА: дождь прошёл 11.09, условия для роста ожидаются с 18.09" in text
    assert "ЛОКАЦИЯ Valašské Meziříčí:" in text
    assert (
        "  сегодня: средняя — API30 24 мм, карта ČHMÚ 3/5, HoubyMapa 4/5 (0.63), "
        "за 7 дней 26 мм" in text
    )
    assert "  перспектива: высокая через 6 дней (18.09)" in text
    assert "  дождь: ближайший ≥ 5 мм — 27 мм 17.09" in text
    assert "ОГОВОРКИ: нет" in text
    assert not ISO_DATE.search(text)
    assert text.count("🍄") == 1


def test_a_far_window_is_marked_as_a_guess():
    item = summary(payload=brief(location(candidate=TODAY + timedelta(days=9))))
    text = report_lib.render(item, send=True, reason="тест")
    assert "высокая ожидается с 21.09 (ориентировочно)" in text
    assert "перспектива: высокая через 9 дней (21.09) (ориентировочно)" in text


def test_no_window_says_so_in_plain_words():
    text = report_lib.render(summary(), send=True, reason="тест")
    assert "окна в горизонте нет" in text
    assert "перспектива: нет в горизонте 16 дней" in text


def test_the_weekend_block_speaks_about_both_days():
    item = summary(
        "weekend",
        payload=brief(
            location(forecast_verdicts=("средняя", "низкая")),
            location(
                slug="valasska-bystrice",
                name="Valašská Bystřice",
                forecast_verdicts=("высокая", "средняя"),
            ),
        ),
    )
    text = report_lib.render(item, send=True, reason="тест")
    assert "ЗАГОЛОВОК: 🍄 Грибной прогноз на выходные 12–13.09" in text
    assert "ИТОГ: сб: Valmez — средняя, Bystřice — высокая; вс: " in text
    assert "лучше: Bystřice, суббота" in text
    assert "  сб 12.09: средняя; вс 13.09: низкая — API30 24 мм" in text
    assert "дождь на выходные: не ожидается" in text
    assert not ISO_DATE.search(text)


def test_every_phase_has_human_wording():
    def phase_text(phase):
        return summary(payload=brief(location(phase=phase)))["phase_text"]

    assert phase_text("waiting").startswith("дождь прошёл 11.09")
    assert phase_text("primary_window") == "расчётное окно роста идёт, стоит проверить лес"
    assert "остаточная вероятность держится до 02.10" in phase_text("residual_window")
    assert phase_text("expired") == "окно закончилось, ждём следующего дождя"
    assert phase_text("no_episode") == "подходящего дождя не было"


def test_the_block_fits_a_telegram_message():
    item = summary(
        payload=brief(
            location(),
            location(slug="valasska-bystrice", name="Valašská Bystřice"),
        )
    )
    text = report_lib.render(item, send=True, reason="первый запуск")
    assert len(text.encode("utf-8")) <= 1536, len(text.encode("utf-8"))


def test_json_carries_the_same_fields(store):
    item = summary(payload=brief(location(candidate=TODAY + timedelta(days=6))))
    send, reason = report_lib.publish(store, item)
    payload = report_lib.to_json(item, send=send, reason=reason)
    assert payload["mode"] == "daily"
    assert payload["date"] == "2026-09-12"
    assert payload["send"] is True
    assert payload["header"] == "🍄 Грибной прогноз: 12.09"
    assert payload["phase_text"].startswith("дождь прошёл 11.09")
    assert payload["error_class"] == "none"
    assert payload["rules_version"] == "3"
    assert payload["locations"][0]["verdict"] == "medium"
    assert payload["locations"][0]["prospect"] == "высокая через 6 дней (18.09)"
    assert payload["text"] == report_lib.render(item, send=send, reason=reason)


def test_the_stored_state_is_what_the_next_run_compares(store):
    item = summary(payload=brief(location(candidate=TODAY + timedelta(days=6))))
    report_lib.publish(store, item)
    row = store.last_report("daily")
    assert row["date"] == "2026-09-12"
    assert row["sent"] == 1
    assert '"valmez": "medium"' in row["verdicts_json"]
    assert "2026-09-18" in row["candidates_json"]
    assert row["rules_version"] == "3"
    assert store.last_report("daily", before=TODAY) is None


@pytest.mark.parametrize(
    "day, expected",
    [
        (date(2026, 9, 11), (date(2026, 9, 12), date(2026, 9, 13))),  # Friday
        (date(2026, 9, 12), (date(2026, 9, 12), date(2026, 9, 13))),  # Saturday
        (date(2026, 9, 13), (date(2026, 9, 12), date(2026, 9, 13))),  # Sunday
        (date(2026, 9, 14), (date(2026, 9, 19), date(2026, 9, 20))),  # Monday
    ],
)
def test_the_weekend_is_the_upcoming_one(day, expected):
    assert report_lib._weekend_days(day) == expected


def test_a_weekend_across_a_month_boundary_prints_both_months():
    item = report_lib.summarize(
        brief(location(today=date(2026, 10, 2))),
        mode="weekend",
        today=date(2026, 10, 2),
    )
    assert "30" not in report_lib._header(item)
    assert report_lib._header(item).endswith("03–04.10")
    item = report_lib.summarize(
        brief(location(today=date(2026, 10, 31))),
        mode="weekend",
        today=date(2026, 10, 31),
    )
    assert report_lib._header(item).endswith("31.10–01.11")
