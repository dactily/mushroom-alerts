"""``report``: the human block and the send/silent decision.

The decision used to live in an LLM prompt and a durable notepad. Here it is
ordinary code, so it can be pinned down: what makes a message, what keeps
quiet, and what a second run on the same day must not change.

Since PLAN §9 the block is a list of "location — chance, %" in the order of
``locations.yaml``. Two things are therefore worth as much as the decision
itself: that the order survives, and that the script never advises where to
drive -- it only picks which two locations get a technical line.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from mushroom_alerts import report as report_lib
from mushroom_alerts.store import Store

TODAY = date(2026, 9, 12)  # a Saturday; the weekend is today and tomorrow
FRIDAY = date(2026, 9, 11)

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

HORIZON = 16


def chance_block(today, value, *, curve=None, capped=False):
    """``views`` hands the report this shape; ``curve`` overrides by offset.

    ``value=None`` is a location with no usable API30: the whole curve is
    ``None`` and there is no peak, exactly as ``ChanceOutlook`` renders it.
    """
    days = {
        today + timedelta(days=n): (curve or {}).get(n, value)
        for n in range(0, HORIZON + 1)
    }
    known = {day: number for day, number in days.items() if number is not None}
    peak = min(known.items(), key=lambda pair: (-pair[1], pair[0])) if known else None
    return {
        "today": days[today],
        "curve": days,
        "peak": peak,
        "capped": capped,
    }


# ----------------------------------------------------------------------
# a brief payload, reduced to the fields ``report`` reads
# ----------------------------------------------------------------------
def location(
    slug="valmez",
    name="Valašské Meziříčí",
    short="Valmez",
    *,
    chance=40,
    curve=None,
    capped=False,
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
        for n in range(0, HORIZON + 1)
    ]
    for offset, label in enumerate(forecast_verdicts):
        # today and tomorrow are the weekend in these fixtures
        days[offset]["verdict"] = label
    return {
        "slug": slug,
        "name": name,
        "short_name": short or name,
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
            # the derived curve for today -- the number the chance used, and
            # deliberately not the station's 23.7 mm
            "today_mm": 35.9,
            "api30_quality": "fresh",
            "next_rain": (today + timedelta(days=5), 27.4),
        },
        "biological": {
            "chance": chance_block(today, chance, curve=curve, capped=capped),
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


def four_locations(today=TODAY, **chances):
    """Config order that matches neither the alphabet nor the chance."""
    spec = [
        ("valmez", "Valašské Meziříčí", "Valmez", 40),
        ("bystrice-pod-hostynem", "Bystřice pod Hostýnem", None, 50),
        ("rajnochovice", "Rajnochovice", None, 55),
        ("katerinice", "Kateřinice", None, 35),
    ]
    return brief(
        *(
            location(
                slug=slug,
                name=name,
                short=short,
                chance=chances.get(slug.replace("-", "_"), value),
                today=today,
            )
            for slug, name, short, value in spec
        )
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s.sqlite") as db:
        yield db


# ----------------------------------------------------------------------
# the decision (PLAN §9d)
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


@pytest.mark.parametrize(
    "before, now, sends",
    [
        (40, 40, False),
        (40, 49, False),  # nine points is noise
        (40, 31, False),
        (40, 50, True),  # ten points is news
        (40, 30, True),
        (55, 60, True),  # five points, but the 60 % line was crossed
        (60, 55, True),
        (61, 95, True),
        (65, 62, False),  # both sides of the line, no big move
    ],
)
def test_the_send_rule_is_about_the_size_of_the_move(store, before, now, sends):
    report_lib.publish(
        store, summary(today=FRIDAY, payload=brief(location(chance=before, today=FRIDAY)))
    )
    send, reason = report_lib.publish(
        store, summary(today=TODAY, payload=brief(location(chance=now)))
    )
    assert send is sends, reason


def test_a_move_at_any_single_location_is_enough(store):
    report_lib.publish(store, summary(today=FRIDAY, payload=four_locations(FRIDAY)))
    send, reason = report_lib.publish(
        store, summary(today=TODAY, payload=four_locations(katerinice=15))
    )
    assert send is True
    assert "Kateřinice: шанс 35 % → 15 %" in reason


def test_a_new_location_is_news(store):
    report_lib.publish(store, summary(today=FRIDAY))
    send, reason = report_lib.publish(
        store,
        summary(
            today=TODAY,
            payload=brief(location(), location(slug="rajnochovice", name="Rajnochovice", short=None)),
        ),
    )
    assert send is True
    assert "Rajnochovice: новая локация, шанс 40 %" in reason


def test_the_crossing_names_the_threshold(store):
    report_lib.publish(
        store, summary(today=FRIDAY, payload=brief(location(chance=55, today=FRIDAY)))
    )
    _, reason = report_lib.publish(
        store, summary(today=TODAY, payload=brief(location(chance=60)))
    )
    assert reason == "лучший шанс 60 %, выше 60 %"

    _, reason = report_lib.publish(
        store,
        report_lib.summarize(
            brief(location(chance=55, today=TODAY + timedelta(days=1))),
            mode="daily",
            today=TODAY + timedelta(days=1),
        ),
    )
    assert reason == "лучший шанс упал до 55 %, ниже 60 %"


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
    assert "ШАНС: данных нет, прогноз не собрался" in text
    assert "ПОДРОБНО" not in text


def test_the_daily_and_weekend_states_do_not_mix(store):
    report_lib.publish(store, summary("weekend", today=FRIDAY))
    send, reason = report_lib.publish(store, summary("daily", today=TODAY))
    assert send is True and "первый запуск" in reason


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        report_lib.summarize(brief(), mode="hourly", today=TODAY)


# ----------------------------------------------------------------------
# the block (PLAN §9c)
# ----------------------------------------------------------------------
def test_the_daily_block_has_every_field_and_no_iso_dates():
    item = summary(
        payload=brief(
            location(curve={6: 70}),
            location(
                slug="valasska-bystrice",
                name="Valašská Bystřice",
                short="Bystřice",
                chance=35,
            ),
        )
    )
    text = report_lib.render(item, send=True, reason="тест")
    assert text.startswith("ОТПРАВЛЯТЬ: да\nПРИЧИНА: тест\n")
    assert "ЗАГОЛОВОК: 🍄 Грибной прогноз: 12.09" in text
    assert "ШАНС на 12.09:\n  Valmez — 40 %\n  Bystřice — 35 %\n" in text
    assert "ФАЗА: дождь прошёл 11.09, условия для роста ожидаются с 18.09" in text
    assert "ПОДРОБНО (2 локации с лучшим шансом):" in text
    assert (
        "  Valmez 40 %: API30 36 мм (расчёт), станция 24 мм, "
        "HoubyMapa 4/5 (0.63), карта ČHMÚ 3/5, "
        "за 7 дней 26 мм; максимум 70 % 18.09\n" in text
    )
    assert "  Bystřice 35 %: API30 36 мм (расчёт), станция 24 мм" in text
    assert "ОГОВОРКИ: нет" in text
    assert not ISO_DATE.search(text)
    assert text.count("🍄") == 1


def test_the_chance_list_is_in_the_config_order_never_sorted():
    """The user keeps his own order; the script must not touch it."""
    text = report_lib.render(
        summary(payload=four_locations()), send=True, reason="тест"
    )
    listed = [
        line.strip().split(" — ")[0]
        for line in text.splitlines()
        if line.startswith("  ") and " — " in line
    ]
    assert listed == [
        "Valmez",
        "Bystřice pod Hostýnem",
        "Rajnochovice",
        "Kateřinice",
    ]
    assert listed != sorted(listed)  # not alphabetical
    assert listed[0] != "Rajnochovice"  # not by chance either


def test_a_location_without_a_short_name_keeps_its_full_name():
    """Truncating to the last word would print "Hostýnem" (PLAN §9f)."""
    text = report_lib.render(
        summary(payload=four_locations()), send=True, reason="тест"
    )
    assert "  Bystřice pod Hostýnem — 50 %" in text
    assert "Hostýnem —" not in text.replace("Bystřice pod Hostýnem —", "")


def test_only_the_two_best_locations_get_a_technical_line():
    text = report_lib.render(
        summary(payload=four_locations()), send=True, reason="тест"
    )
    details = [line for line in text.splitlines() if line.startswith("  ") and ":" in line]
    assert len(details) == 2
    assert details[0].startswith("  Rajnochovice 55 %:")
    assert details[1].startswith("  Bystřice pod Hostýnem 50 %:")


def test_a_tie_for_the_detail_lines_is_broken_by_the_config_order():
    text = report_lib.render(
        summary(payload=four_locations(valmez=55, rajnochovice=55, katerinice=55)),
        send=True,
        reason="тест",
    )
    details = [line for line in text.splitlines() if line.startswith("  ") and ":" in line]
    assert [line.split()[1] for line in details] == ["55", "55"]
    assert details[0].startswith("  Valmez 55 %:")
    assert details[1].startswith("  Rajnochovice 55 %:")


def test_a_detail_line_carries_its_own_phase_when_it_differs():
    payload = brief(
        location(phase="primary_window"),
        location(
            slug="rajnochovice",
            name="Rajnochovice",
            short=None,
            chance=55,
            phase="waiting",
        ),
    )
    text = report_lib.render(summary(payload=payload), send=True, reason="тест")
    assert "ФАЗА: расчётное окно роста идёт, стоит проверить лес" in text
    assert (
        "; фаза: дождь прошёл 11.09, условия для роста ожидаются с 18.09"
        in text.split("ПОДРОБНО")[1]
    )
    # the headline location does not repeat the headline phase
    assert text.count("расчётное окно роста идёт") == 1


def test_a_capped_location_is_named_in_the_caveats_not_in_the_list():
    item = summary(
        payload=brief(
            location(chance=50, capped=True),
            location(slug="rajnochovice", name="Rajnochovice", short=None, chance=40),
        )
    )
    text = report_lib.render(item, send=True, reason="тест")
    assert "  Valmez — 50 %\n" in text
    assert "ОГОВОРКИ: без свежей станции шанс ограничен 50 %: Valmez" in text


def test_the_detail_line_prints_the_api30_the_chance_was_computed_from():
    """Two numbers exist for today; the line used to print the other one.

    The chance comes off the derived forecast curve (36 mm here), the
    station reports what it measured (24 mm).  Printing only the station
    number made the percentage impossible to reproduce.
    """
    payload = brief(location())
    text = report_lib.render(summary(payload=payload), send=True, reason="тест")
    assert "API30 36 мм (расчёт), станция 24 мм" in text

    without_curve = brief(location())
    without_curve["locations"][0]["forecast"]["today_mm"] = None
    text = report_lib.render(
        summary(payload=without_curve), send=True, reason="тест"
    )
    assert "API30 24 мм (станция)" in text

    without_station = brief(location())
    without_station["locations"][0]["station"]["api30_mm"] = None
    text = report_lib.render(
        summary(payload=without_station), send=True, reason="тест"
    )
    assert "API30 36 мм (расчёт), станции нет" in text


def test_a_stale_release_makes_the_line_print_the_station_number():
    """``views`` falls back to the station for today, so the line does too.

    Printing "API30 28 мм (расчёт)" under a percentage computed from the
    station's 21 mm would be the same unreproducible line in a new disguise.
    """
    stale = brief(location())
    stale["locations"][0]["forecast"]["api30_quality"] = "stale"
    text = report_lib.render(summary(payload=stale), send=True, reason="тест")

    assert "API30 24 мм (станция)" in text
    assert "36 мм" not in text


def test_a_location_without_a_chance_says_so():
    payload = brief(location())
    payload["locations"][0]["biological"].pop("chance")
    text = report_lib.render(summary(payload=payload), send=True, reason="тест")
    assert "  Valmez — нет данных" in text


# ----------------------------------------------------------------------
# «нет данных»: a location whose API30 is missing or stale (PLAN §9b)
# ----------------------------------------------------------------------
def test_a_location_without_a_number_is_not_detailed_but_is_explained():
    """No number is not a low number: it is not ranked and it is named."""
    item = summary(
        payload=brief(
            location(chance=None),
            location(
                slug="rajnochovice", name="Rajnochovice", short=None, chance=40
            ),
        )
    )
    text = report_lib.render(item, send=True, reason="тест")

    assert "ШАНС на 12.09:\n  Valmez — нет данных\n  Rajnochovice — 40 %\n" in text
    assert "ПОДРОБНО (1 локация с лучшим шансом):" in text
    assert "  Rajnochovice 40 %:" in text
    assert text.count("Valmez") == 2  # the chance line and the caveat, no detail
    assert "ОГОВОРКИ: нет свежего API30: Valmez" in text


def test_a_block_where_nothing_has_a_number_still_says_so_once():
    item = summary(
        payload=brief(
            location(chance=None),
            location(
                slug="rajnochovice", name="Rajnochovice", short=None, chance=None
            ),
        )
    )
    text = report_lib.render(item, send=True, reason="тест")

    assert "ПОДРОБНО: ни у одной локации нет числа" in text
    assert text.count("ПОДРОБНО") == 1
    assert "ОГОВОРКИ: нет свежего API30: Valmez, Rajnochovice" in text
    assert report_lib.to_json(item, send=True, reason="тест")["detail"] == []


def test_the_weekend_block_says_it_for_both_days():
    item = summary("weekend", payload=brief(location(chance=None)))
    text = report_lib.render(item, send=True, reason="тест")

    assert "  Valmez — сб нет данных, вс нет данных\n" in text
    assert "ПОДРОБНО: ни у одной локации нет числа" in text


def test_the_missing_and_the_capped_are_both_named_in_the_caveats():
    item = summary(
        payload=brief(
            location(chance=None),
            location(
                slug="rajnochovice",
                name="Rajnochovice",
                short=None,
                chance=50,
                capped=True,
            ),
        )
    )
    assert item["caveats"] == (
        "нет свежего API30: Valmez, "
        "без свежей станции шанс ограничен 50 %: Rajnochovice"
    )


def test_many_blind_locations_are_counted_not_listed():
    payload = brief(
        *(
            location(slug=f"loc-{n}", name=f"Location {n}", short=None, chance=None)
            for n in range(5)
        )
    )
    assert summary(payload=payload)["caveats"] == "нет свежего API30: 5 локаций"


@pytest.mark.parametrize(
    "before, now, sends, needle",
    [
        (40, None, True, "Valmez: данные пропали"),
        (None, 40, True, "Valmez: данные появились, шанс 40 %"),
        (None, None, False, "ничего не изменилось с прошлого отчёта"),
    ],
)
def test_a_number_appearing_or_disappearing_is_news(store, before, now, sends, needle):
    """The block the user reads changed, so say it -- but silence stays silent."""
    report_lib.publish(
        store,
        summary(today=FRIDAY, payload=brief(location(chance=before, today=FRIDAY))),
    )
    send, reason = report_lib.publish(
        store, summary(today=TODAY, payload=brief(location(chance=now)))
    )

    assert send is sends
    assert needle in reason


def test_a_new_location_without_a_number_is_not_news(store):
    """"New" and "was there, said nothing" are different rows, not one ``None``."""
    report_lib.publish(store, summary(today=FRIDAY))
    send, reason = report_lib.publish(
        store,
        summary(
            today=TODAY,
            payload=brief(
                location(),
                location(
                    slug="rajnochovice",
                    name="Rajnochovice",
                    short=None,
                    chance=None,
                ),
            ),
        ),
    )

    assert send is False, reason


def test_the_stored_state_keeps_a_missing_number_as_null(store):
    item = summary(payload=brief(location(chance=None)))
    report_lib.publish(store, item)

    row = store.last_report("daily")
    assert json.loads(row["chances_json"]) == {"valmez": None}


def test_the_weekend_block_speaks_about_both_days():
    item = summary(
        "weekend",
        payload=brief(
            location(curve={0: 55, 1: 60}),
            location(
                slug="valasska-bystrice",
                name="Valašská Bystřice",
                short="Bystřice",
                chance=35,
                curve={0: 35, 1: 30},
            ),
        ),
    )
    text = report_lib.render(item, send=True, reason="тест")
    assert "ЗАГОЛОВОК: 🍄 Грибной прогноз на выходные 12–13.09" in text
    assert "ШАНС на выходные:\n  Valmez — сб 55 %, вс 60 %\n  Bystřice — сб 35 %, вс 30 %\n" in text
    assert "  Valmez 60 % (вс): API30 36 мм (расчёт), станция 24 мм" in text
    assert not ISO_DATE.search(text)


def test_a_far_peak_is_marked_as_a_guess():
    item = summary(payload=brief(location(curve={9: 70})))
    text = report_lib.render(item, send=True, reason="тест")
    assert "максимум 70 % 21.09 (ориентировочно)" in text


def test_a_peak_that_is_not_better_than_today_is_not_mentioned():
    text = report_lib.render(summary(), send=True, reason="тест")
    assert "максимум" not in text


def test_every_phase_has_human_wording():
    def phase_text(phase):
        return summary(payload=brief(location(phase=phase)))["phase_text"]

    assert phase_text("waiting").startswith("дождь прошёл 11.09")
    assert phase_text("primary_window") == "расчётное окно роста идёт, стоит проверить лес"
    assert "остаточная вероятность держится до 02.10" in phase_text("residual_window")
    assert phase_text("expired") == "окно закончилось, ждём следующего дождя"
    assert phase_text("no_episode") == "подходящего дождя не было"


def test_twenty_locations_still_fit_a_telegram_message():
    """PLAN §9: the whole point is a list that scales to ~20 forests."""
    payload = brief(
        *(
            location(
                slug=f"loc-{n}",
                name=f"Valašská Location {n}",
                short=None,
                chance=5 + (n % 19) * 5,
            )
            for n in range(20)
        )
    )
    text = report_lib.render(summary(payload=payload), send=True, reason="первый запуск")
    assert len(text.splitlines()) == 20 + 9  # 20 chances + 9 fixed lines
    assert len(text.encode("utf-8")) <= 2048, len(text.encode("utf-8"))


def test_json_carries_the_same_fields(store):
    item = summary(payload=brief(location(curve={6: 70})))
    send, reason = report_lib.publish(store, item)
    payload = report_lib.to_json(item, send=send, reason=reason)
    assert payload["mode"] == "daily"
    assert payload["date"] == "2026-09-12"
    assert payload["send"] is True
    assert payload["header"] == "🍄 Грибной прогноз: 12.09"
    assert payload["chance_title"] == "ШАНС на 12.09"
    assert payload["chances"] == ["Valmez — 40 %"]
    assert payload["detail"][0].startswith(
        "Valmez 40 %: API30 36 мм (расчёт), станция 24 мм"
    )
    assert payload["phase_text"].startswith("дождь прошёл 11.09")
    assert payload["error_class"] == "none"
    assert payload["rules_version"] == "6"
    assert payload["locations"][0]["chance"] == 40
    assert payload["locations"][0]["chance_peak"] == ["2026-09-18", 70]
    assert payload["locations"][0]["verdict"] == "medium"
    assert payload["locations"][0]["detailed"] is True
    assert payload["text"] == report_lib.render(item, send=send, reason=reason)


def test_the_stored_state_is_what_the_next_run_compares(store):
    item = summary(payload=brief(location(candidate=TODAY + timedelta(days=6))))
    report_lib.publish(store, item)
    row = store.last_report("daily")
    assert row["date"] == "2026-09-12"
    assert row["sent"] == 1
    assert '"valmez": 40' in row["chances_json"]
    assert '"valmez": "medium"' in row["verdicts_json"]
    assert "2026-09-18" in row["candidates_json"]
    assert row["rules_version"] == "6"
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


def test_many_capped_locations_are_counted_not_listed():
    payload = brief(
        *(
            location(slug=f"loc-{n}", name=f"Location {n}", short=None, capped=True)
            for n in range(7)
        )
    )
    item = summary(payload=payload)
    assert item["caveats"] == "без свежей станции шанс ограничен 50 %: 7 локаций"


def test_a_long_list_of_reasons_is_trimmed(store):
    """ПРИЧИНА explains the block; with twenty locations it must not bury it."""
    payload = brief(
        *(
            location(slug=f"loc-{n}", name=f"Location {n}", short=None, chance=40)
            for n in range(8)
        )
    )
    report_lib.publish(store, report_lib.summarize(payload, mode="daily", today=FRIDAY))
    moved = brief(
        *(
            location(slug=f"loc-{n}", name=f"Location {n}", short=None, chance=10)
            for n in range(8)
        )
    )
    send, reason = report_lib.publish(
        store, report_lib.summarize(moved, mode="daily", today=TODAY)
    )
    assert send is True
    assert reason.count(";") == 4
    assert reason.endswith("ещё изменений: 4")
