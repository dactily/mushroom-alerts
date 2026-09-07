"""``rules``: the five triggers, the antispam, and ``decide`` end to end.

Every trigger is tested twice -- once firing, once not -- and the ones that
depend on history get a real (temporary) :class:`Store` with synthetic
readings for "yesterday", because that is exactly what production does.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from mushroom_alerts import api30, rules
from mushroom_alerts.base import FetchResult, Location, Reading
from mushroom_alerts.store import Store

from .conftest import BYSTRICE, VALMEZ

TODAY = date(2026, 9, 7)
YESTERDAY = TODAY - timedelta(days=1)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
@pytest.fixture
def store(tmp_path) -> Store:
    with Store(tmp_path / "state.sqlite") as s:
        s.sync_location(VALMEZ)
        s.sync_location(BYSTRICE)
        yield s


@pytest.fixture(autouse=True)
def default_threshold(monkeypatch):
    monkeypatch.delenv(api30.THRESHOLD_ENV, raising=False)


def r(source, metric, value, day=TODAY, slug="valmez", **meta):
    return Reading(
        source=source, location=slug, date=day, metric=metric, value=value,
        meta=meta or None,
    )


def result(source, readings, ok=True, error=None):
    return FetchResult(source=source, ok=ok, readings=list(readings), error=error)


def station_history(store, slug="valmez", *, rain=(), t_mean=15.0, days=40, end=TODAY):
    """A dry-by-default station series ending on ``end``, plus wet days."""
    readings = []
    for n in range(days, -1, -1):
        day = end - timedelta(days=n)
        readings.append(r("chmi_station", "sra_mm", float(dict(rain).get(day, 0.0)), day, slug))
        readings.append(r("chmi_station", "t_mean", t_mean, day, slug))
    store.upsert_readings(readings)


def openmeteo_result(slug="valmez", *, rain=(), t_mean=15.0, t_min=8.0, days=16):
    """An Open-Meteo ``FetchResult`` for today .. today+days-1."""
    readings = []
    wet = dict(rain)
    for n in range(0, days):
        day = TODAY + timedelta(days=n)
        meta = {"issued": TODAY.isoformat()} if day > TODAY else {"provisional": True}
        for metric, value in (
            ("precip_mm", float(wet.get(day, 0.0))),
            ("t_mean", t_mean),
            ("t_min", t_min),
        ):
            readings.append(
                Reading("openmeteo", slug, day, metric, value, meta=dict(meta))
            )
    return result("openmeteo", readings)


# ----------------------------------------------------------------------
# trigger 1 -- ČHMÚ map
# ----------------------------------------------------------------------
def test_chmi_fires_on_level_4():
    signal = rules.chmi_map_signal(r("chmi_map", "level", 4.0), None)
    assert signal is not None and signal.trigger == "chmi_map"
    assert "ČHMÚ 4/5" in signal.text and "высокая" in signal.text


def test_chmi_fires_on_a_one_step_rise():
    signal = rules.chmi_map_signal(
        r("chmi_map", "level", 3.0),
        r("chmi_map", "level", 2.0, YESTERDAY),
    )
    assert signal is not None and "↑" in signal.text
    assert signal.data["rose"] is True


def test_chmi_silent_on_a_middling_level():
    assert rules.chmi_map_signal(r("chmi_map", "level", 3.0), None) is None
    assert (
        rules.chmi_map_signal(
            r("chmi_map", "level", 3.0), r("chmi_map", "level", 3.0, YESTERDAY)
        )
        is None
    )


def test_chmi_silent_on_a_fall():
    assert (
        rules.chmi_map_signal(
            r("chmi_map", "level", 2.0), r("chmi_map", "level", 3.0, YESTERDAY)
        )
        is None
    )


def test_chmi_ignores_a_stale_map():
    assert rules.chmi_map_signal(r("chmi_map", "level", 5.0, stale=True), None) is None


def test_chmi_silent_without_a_reading():
    assert rules.chmi_map_signal(None, r("chmi_map", "level", 1.0, YESTERDAY)) is None


# ----------------------------------------------------------------------
# trigger 2 -- HoubyMapa
# ----------------------------------------------------------------------
def test_houbymapa_fires_on_level_4():
    signal = rules.houbymapa_signal(4.0, 0.55, 3.0, 0.47)
    assert signal is not None and "4/5" in signal.text and "высокая" in signal.text


def test_houbymapa_fires_on_score_alone():
    signal = rules.houbymapa_signal(3.0, 0.63, 3.0, 0.47)
    assert signal is not None and "0.63" in signal.text


def test_houbymapa_silent_when_yesterday_was_already_good():
    assert rules.houbymapa_signal(4.0, 0.63, 4.0, 0.61) is None
    assert rules.houbymapa_signal(3.0, 0.62, 3.0, 0.60) is None


def test_houbymapa_silent_below_both_thresholds():
    assert rules.houbymapa_signal(3.0, 0.47, 2.0, 0.20) is None


def test_houbymapa_fires_on_the_first_ever_reading():
    assert rules.houbymapa_signal(4.0, 0.63, None, None) is not None


# ----------------------------------------------------------------------
# trigger 3 -- rain precursor and its window
# ----------------------------------------------------------------------
def _three_days(values, temps=15.0):
    sra = {TODAY - timedelta(days=2 - i): v for i, v in enumerate(values)}
    t = {d: temps for d in sra}
    return sra, t


def test_rain_fires_on_20_mm_at_the_right_temperature():
    sra, t = _three_days([2.0, 17.0, 3.0])
    signal = rules.rain_signal(sra, t, TODAY)
    assert signal is not None and signal.trigger == "rain_forecast"
    anchor = TODAY - timedelta(days=1)
    assert signal.data["anchor"] == anchor.isoformat()
    assert signal.data["window"] == [
        (anchor + timedelta(days=7)).isoformat(),
        (anchor + timedelta(days=12)).isoformat(),
    ]
    assert "окно" in signal.text and "22 mm" in signal.text


def test_rain_silent_below_the_sum():
    sra, t = _three_days([2.0, 15.0, 2.0])
    assert rules.rain_signal(sra, t, TODAY) is None


def test_rain_silent_outside_the_temperature_band():
    sra, _ = _three_days([2.0, 17.0, 3.0])
    for bad in (9.0, 25.0):
        _, t = _three_days([0, 0, 0], temps=bad)
        assert rules.rain_signal(sra, t, TODAY) is None
    assert rules.rain_signal(sra, {}, TODAY) is None  # no temperature at all


def test_rain_only_looks_at_the_last_three_days():
    sra = {TODAY - timedelta(days=n): 30.0 for n in (5, 6, 7)}
    sra.update({TODAY - timedelta(days=n): 1.0 for n in (0, 1, 2)})
    t = {d: 15.0 for d in sra}
    assert rules.rain_signal(sra, t, TODAY) is None


def test_rain_ignores_the_future():
    sra = {TODAY + timedelta(days=1): 50.0, TODAY: 1.0}
    assert rules.rain_signal(sra, {d: 15.0 for d in sra}, TODAY) is None


def test_rain_window_fires_inside_the_window_when_the_ground_stayed_wet():
    episode = {
        "anchor": (TODAY - timedelta(days=8)).isoformat(),
        "window": [
            (TODAY - timedelta(days=1)).isoformat(),
            (TODAY + timedelta(days=4)).isoformat(),
        ],
    }
    signal = rules.rain_window_signal(episode, 27.0, 25.0, TODAY)
    assert signal is not None and signal.trigger == "rain_window"
    assert "окно роста" in signal.text


def test_rain_window_silent_when_dry_or_out_of_range():
    window = [
        (TODAY - timedelta(days=1)).isoformat(),
        (TODAY + timedelta(days=4)).isoformat(),
    ]
    assert rules.rain_window_signal({"window": window}, 12.0, 25.0, TODAY) is None
    assert rules.rain_window_signal({"window": window}, None, 25.0, TODAY) is None
    past = [
        (TODAY - timedelta(days=9)).isoformat(),
        (TODAY - timedelta(days=4)).isoformat(),
    ]
    assert rules.rain_window_signal({"window": past}, 40.0, 25.0, TODAY) is None
    assert rules.rain_window_signal(None, 40.0, 25.0, TODAY) is None


# ----------------------------------------------------------------------
# trigger 4 -- the forecast API30 crossing
# ----------------------------------------------------------------------
def _curve(values):
    return [(TODAY + timedelta(days=i), v) for i, v in enumerate(values)]


def _temps(t_mean=15.0, t_min=8.0, days=10):
    return {
        TODAY + timedelta(days=i): {"t_mean": t_mean, "t_min": t_min}
        for i in range(days)
    }


def test_api30_fires_on_a_crossing_that_passes_the_temperature_gate():
    curve = _curve([10, 12, 20, 26, 30])
    signal = rules.api30_signal(curve, _temps(), 25.0, TODAY)
    assert signal is not None and signal.trigger == "api30_cross"
    assert signal.data["cross"] == (TODAY + timedelta(days=3)).isoformat()
    assert "API30 ≥ 25 mm с" in signal.text and "пик 30 mm" in signal.text
    assert "ориентировочно" not in signal.text  # 3 days ahead is not vague


def test_api30_names_the_rain_that_caused_it():
    curve = _curve([10, 12, 20, 26])
    rain = {TODAY + timedelta(days=2): 18.0, TODAY + timedelta(days=1): 1.0}
    signal = rules.api30_signal(curve, _temps(), 25.0, TODAY, rain=rain)
    assert "дождь 18 mm" in signal.text


def test_api30_marks_a_distant_crossing_as_vague():
    curve = _curve([10] * 9 + [30])
    signal = rules.api30_signal(curve, _temps(days=12), 25.0, TODAY)
    assert signal is not None and "ориентировочно" in signal.text
    assert signal.data["vague"] is True


def test_api30_silent_when_the_temperature_gate_fails():
    curve = _curve([10, 12, 20, 26])
    assert rules.api30_signal(curve, _temps(t_mean=25.0), 25.0, TODAY) is None
    assert rules.api30_signal(curve, _temps(t_min=1.0), 25.0, TODAY) is None
    assert rules.api30_signal(curve, {}, 25.0, TODAY) is None  # unknown = closed


def test_api30_silent_without_a_crossing():
    assert rules.api30_signal(_curve([10, 12, 14]), _temps(), 25.0, TODAY) is None
    assert rules.api30_signal(_curve([30, 32]), _temps(), 25.0, TODAY) is None
    assert rules.api30_signal([], _temps(), 25.0, TODAY) is None


# ----------------------------------------------------------------------
# derivation
# ----------------------------------------------------------------------
def test_derive_api30_writes_the_curve(store):
    station_history(store, rain={TODAY - timedelta(days=1): 30.0})
    curve = rules.derive_api30(
        store, VALMEZ, openmeteo_result(rain={TODAY + timedelta(days=3): 40.0}),
        today=TODAY, threshold=25.0,
    )
    assert curve and curve[0][0] == TODAY
    today_row = store.get_reading("api30_forecast", "valmez", "api30_mm", TODAY)
    assert today_row is not None and today_row.value == pytest.approx(30 * 0.93, abs=0.1)
    # future days are archived as forecasts, today's estimate is not
    issued = store.latest_issue("api30_forecast", "valmez", "api30_mm")
    assert issued == TODAY.isoformat()
    archived = store.forecast_series("api30_forecast", "valmez", "api30_mm", issued)
    assert [row["target_date"] for row in archived][0] == (TODAY + timedelta(days=1)).isoformat()


def test_derive_api30_skips_without_station_history(store):
    assert rules.derive_api30(
        store, VALMEZ, openmeteo_result(), today=TODAY, threshold=25.0
    ) == []
    assert store.latest_issue("api30_forecast", "valmez", "api30_mm") is None


def test_derive_api30_skips_without_a_forecast(store):
    station_history(store)
    assert rules.derive_api30(store, VALMEZ, None, today=TODAY, threshold=25.0) == []
    failed = FetchResult.failure("openmeteo", "offline")
    assert rules.derive_api30(store, VALMEZ, failed, today=TODAY, threshold=25.0) == []


# ----------------------------------------------------------------------
# decide()
# ----------------------------------------------------------------------
def _decide(store, results, locations=None, today=TODAY):
    return rules.decide(locations or [VALMEZ], list(results), store=store, today=today)


def _run(store, results, locations=None, today=TODAY):
    """What ``__main__.cmd_check`` does: upsert first, then decide."""
    for res in results:
        store.upsert_readings(res.readings)
    return _decide(store, results, locations, today)


def test_decide_is_silent_without_any_trigger(store):
    decision = _run(store, [result("chmi_map", [r("chmi_map", "level", 3.0)])])
    assert decision.exit_code == 0
    assert "Valašské Meziříčí" in decision.text  # still shows the snapshot


def test_decide_fires_and_names_only_the_triggered_location(store):
    store.upsert_readings([r("chmi_map", "level", 2.0, YESTERDAY)])
    results = [
        result(
            "chmi_map",
            [
                r("chmi_map", "level", 4.0),
                r("chmi_map", "level", 2.0, slug="valasska-bystrice"),
            ],
        )
    ]
    decision = _run(store, results, [VALMEZ, BYSTRICE])
    assert decision.exit_code == 10
    assert "Valašské Meziříčí" in decision.text
    assert "Valašská Bystřice" not in decision.text
    assert decision.data["locations"]["valmez"]["signals"][0]["trigger"] == "chmi_map"


def test_decide_notes_a_dead_source(store):
    results = [
        result("chmi_map", [r("chmi_map", "level", 4.0)]),
        FetchResult.failure("houbymapa", "502"),
    ]
    decision = _run(store, results)
    assert decision.exit_code == 10
    assert "(HoubyMapa недоступна)" in decision.text


def test_decide_uses_yesterdays_stored_state_for_houbymapa(store):
    store.upsert_readings(
        [
            r("houbymapa", "level", 4.0, YESTERDAY),
            r("houbymapa", "score", 0.63, YESTERDAY),
        ]
    )
    results = [
        result("houbymapa", [r("houbymapa", "level", 4.0), r("houbymapa", "score", 0.64)])
    ]
    assert _run(store, results).exit_code == 0  # good yesterday too -> not news


# -- antispam ----------------------------------------------------------
def test_rerunning_the_same_day_is_idempotent(store):
    results = [result("chmi_map", [r("chmi_map", "level", 4.0)])]
    first = _run(store, results)
    second = _run(store, results)
    assert first.exit_code == second.exit_code == 10
    assert first.text == second.text
    rows = store.conn.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"]
    assert rows == 1


def test_the_same_news_is_not_repeated_the_next_day(store):
    results = [result("chmi_map", [r("chmi_map", "level", 4.0)])]
    assert _run(store, results).exit_code == 10
    tomorrow = TODAY + timedelta(days=1)
    again = [result("chmi_map", [r("chmi_map", "level", 4.0, tomorrow)])]
    decision = _run(store, again, today=tomorrow)
    assert decision.exit_code == 0
    assert decision.data["locations"]["valmez"]["suppressed"] == ["chmi_map"]


def test_different_news_from_the_same_trigger_still_fires(store):
    assert _run(store, [result("chmi_map", [r("chmi_map", "level", 4.0)])]).exit_code == 10
    tomorrow = TODAY + timedelta(days=1)
    up = [result("chmi_map", [r("chmi_map", "level", 5.0, tomorrow)])]
    assert _run(store, up, today=tomorrow).exit_code == 10


def test_antispam_is_per_location(store):
    results = [
        result(
            "chmi_map",
            [
                r("chmi_map", "level", 4.0),
                r("chmi_map", "level", 4.0, slug="valasska-bystrice"),
            ],
        )
    ]
    decision = _run(store, results, [VALMEZ, BYSTRICE])
    assert decision.exit_code == 10
    triggers = {
        slug: [s["trigger"] for s in payload["signals"]]
        for slug, payload in decision.data["locations"].items()
    }
    assert triggers == {"valmez": ["chmi_map"], "valasska-bystrice": ["chmi_map"]}


# -- trigger 4 through decide(), including the date-shift dedupe -------
def _api30_run(store, cross_rain_day, today=TODAY, slug="valmez", location=VALMEZ):
    # 10 mm stays below the rain trigger, so only trigger 4 can fire here
    station_history(store, slug, end=today, rain={today - timedelta(days=1): 10.0})
    results = [openmeteo_result(slug, rain={cross_rain_day: 60.0})]
    return _run(store, results, [location], today=today)


def test_api30_trigger_end_to_end(store):
    decision = _api30_run(store, TODAY + timedelta(days=2))
    assert decision.exit_code == 10
    signals = decision.data["locations"]["valmez"]["signals"]
    assert [s["trigger"] for s in signals] == ["api30_cross"]
    assert signals[0]["data"]["cross"] == (TODAY + timedelta(days=3)).isoformat()
    assert "API30 ≥ 25 mm" in decision.text
    # the derived curve is in the store, ready for `status`
    assert store.latest_issue("api30_forecast", "valmez", "api30_mm") == TODAY.isoformat()


def test_api30_not_repeated_when_the_date_barely_moves(store):
    assert _api30_run(store, TODAY + timedelta(days=5)).exit_code == 10
    tomorrow = TODAY + timedelta(days=1)
    # the same rain, one day later in the run -> D moves by <= 2 days
    store.upsert_readings([r("chmi_station", "sra_mm", 0.0, tomorrow)])
    results = [
        FetchResult(
            source="openmeteo",
            ok=True,
            readings=[
                Reading(
                    "openmeteo", "valmez", TODAY + timedelta(days=n),
                    metric, value,
                    meta={"issued": tomorrow.isoformat()} if TODAY + timedelta(days=n) > tomorrow else {"provisional": True},
                )
                for n in range(1, 17)
                for metric, value in (
                    ("precip_mm", 60.0 if n == 5 else 0.0),
                    ("t_mean", 15.0),
                    ("t_min", 8.0),
                )
            ],
        )
    ]
    decision = _run(store, results, [VALMEZ], today=tomorrow)
    assert decision.exit_code == 0
    assert decision.data["locations"]["valmez"]["suppressed"] == ["api30_cross"]


def test_api30_fires_again_when_the_date_moves_more_than_two_days(store):
    assert _api30_run(store, TODAY + timedelta(days=2)).exit_code == 10
    row = store.last_notification("valmez", "api30_cross")
    # the rain of day D is only inside the API30 window on D+1
    assert json.loads(row["text"])["data"]["cross"] == (TODAY + timedelta(days=3)).isoformat()

    tomorrow = TODAY + timedelta(days=1)
    decision = _api30_run(store, tomorrow + timedelta(days=9), today=tomorrow)
    assert decision.exit_code == 10
    row = store.last_notification("valmez", "api30_cross")
    assert json.loads(row["text"])["data"]["cross"] == (tomorrow + timedelta(days=10)).isoformat()


def test_api30_fires_again_after_two_weeks(store):
    store.add_notification(
        TODAY - timedelta(days=15),
        "valmez",
        "api30_cross",
        json.dumps({"key": "x", "data": {"cross": (TODAY + timedelta(days=3)).isoformat()}}),
    )
    assert _api30_run(store, TODAY + timedelta(days=2)).exit_code == 10


def test_api30_threshold_can_be_raised_from_the_environment(store, monkeypatch):
    monkeypatch.setenv(api30.THRESHOLD_ENV, "500")
    decision = _api30_run(store, TODAY + timedelta(days=2))
    assert decision.exit_code == 0
    assert decision.data["threshold_mm"] == 500.0


# -- rain triggers through decide() ------------------------------------
def test_rain_trigger_end_to_end_then_the_window(store):
    wet = {TODAY - timedelta(days=1): 60.0}  # still >= 25 mm of API30 a week on
    station_history(store, rain=wet)
    decision = _decide(store, [result("chmi_station", [])])
    assert decision.exit_code == 10
    assert "дождь" in decision.text and "окно" in decision.text

    # ... and 8 days later, with the ground still wet, the window opens
    later = TODAY + timedelta(days=8)
    station_history(store, rain=wet, end=later)
    decision = _decide(store, [result("chmi_station", [])], today=later)
    assert decision.exit_code == 10
    assert "окно роста" in decision.text


def test_rain_window_needs_the_api30_to_hold(store):
    wet = {TODAY - timedelta(days=1): 30.0}
    station_history(store, rain=wet)
    assert _decide(store, [result("chmi_station", [])]).exit_code == 10
    later = TODAY + timedelta(days=8)  # API30 has decayed to ~15 mm by now
    station_history(store, rain=wet, end=later)
    decision = _decide(store, [result("chmi_station", [])], today=later)
    assert decision.exit_code == 0


# ----------------------------------------------------------------------
# snapshot / describe
# ----------------------------------------------------------------------
def test_describe_is_one_compact_line(store):
    station_history(store, rain={TODAY - timedelta(days=1): 12.0})
    store.upsert_readings(
        [
            r("chmi_map", "level", 3.0),
            r("houbymapa", "level", 3.0),
            r("houbymapa", "score", 0.47),
            r("chmi_station", "api30_mm", 20.3, YESTERDAY),
        ]
    )
    rules.derive_api30(
        store, VALMEZ, openmeteo_result(rain={TODAY + timedelta(days=2): 9.0}),
        today=TODAY, threshold=25.0,
    )
    line = rules.describe(store, VALMEZ, TODAY)
    assert line.count("\n") == 0
    assert line.startswith("🍄 Valašské Meziříčí:")
    for fragment in ("ČHMÚ 3/5", "HoubyMapa 3/5 (0.47)", "станция API30 20 mm",
                     "SRA 3d 12 mm", "T 15.0 °C", "прогноз:", "порог 25 mm"):
        assert fragment in line, line


def test_describe_without_any_data(store):
    assert rules.describe(store, BYSTRICE, TODAY).endswith("нет данных")


def test_decide_survives_a_location_with_no_data_at_all(store):
    decision = _run(store, [FetchResult.failure("chmi_map", "down")], [VALMEZ, BYSTRICE])
    assert decision.exit_code == 0
    assert decision.text.count("🍄") == 2


def test_a_total_blackout_does_not_burn_the_antispam_slot(store):
    """Every source dead is exit 1 in the CLI: the message would be lost."""
    station_history(store, rain={TODAY - timedelta(days=1): 40.0})
    dead = [FetchResult.failure("chmi_station", "offline")]
    decision = _decide(store, dead)
    assert decision.exit_code == 0
    assert store.last_notification("valmez") is None
    # ... and once a source is back, the same rain is still news
    alive = dead + [result("chmi_map", [r("chmi_map", "level", 3.0)])]
    assert _decide(store, alive).exit_code == 10
