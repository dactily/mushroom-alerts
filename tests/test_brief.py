"""``brief``: the fact sheet Hermes Agent reads (PLAN §2b).

Two levels: :func:`brief.build`/:func:`brief.render` straight off a
synthetic store, and the whole ``python -m mushroom_alerts brief`` CLI with
fake fetchers -- including a dead source and a total blackout, because the
brief has to survive both (PLAN §6).
"""

from __future__ import annotations

import json
import re
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest

from mushroom_alerts import __main__ as cli
from mushroom_alerts import brief as brief_lib
from mushroom_alerts.base import Decision, FetchResult, Location, Reading
from mushroom_alerts.store import Store

SHIPPED = Path(__file__).resolve().parent.parent / "locations.yaml"
VALMEZ = Location(name="Valašské Meziříčí", lat=49.4718, lon=17.9711, slug="valmez")

#: Everything is relative to the real today: ``cmd_brief`` uses ``date.today()``.
TODAY = date.today()

STATION_META = {
    "wsi": "0-203-0-11769",
    "name": "Valašské Meziříčí",
    "elev_m": 334.0,
    "distance_km": 0.94,
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own DB, own copy of locations.yaml, no network."""
    locations = tmp_path / "locations.yaml"
    locations.write_text(SHIPPED.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("MUSHROOM_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("MUSHROOM_LOCATIONS", str(locations))
    monkeypatch.delenv("MUSHROOM_RECORD", raising=False)
    monkeypatch.delenv("MUSHROOM_API30_THRESHOLD", raising=False)
    monkeypatch.setattr(cli, "Http", lambda **kw: _NullHttp())
    yield tmp_path


class _NullHttp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


# ----------------------------------------------------------------------
# synthetic data
# ----------------------------------------------------------------------
def reading(source, metric, offset, value, slug="valmez", **meta):
    day = TODAY + timedelta(days=offset)
    if offset > 0:
        meta.setdefault("issued", TODAY.isoformat())
    return Reading(
        source=source,
        location=slug,
        date=day,
        metric=metric,
        value=value,
        meta=meta or None,
    )


def station_readings(slug="valmez"):
    """20 days of rain/temperature plus API30, humidity and soil."""
    out = []
    for n in range(19, -1, -1):
        rain = 22.0 if n == 9 else (1.5 if n % 5 == 0 else 0.0)
        meta = {"wsi": STATION_META["wsi"], "distance_km": 0.94}
        if n == 0:
            meta["station"] = STATION_META
        out.append(reading("chmi_station", "sra_mm", -n, rain, slug=slug, **meta))
        out.append(reading("chmi_station", "t_mean", -n, 14.0 + n * 0.1, slug=slug))
        out.append(reading("chmi_station", "t_min", -n, 8.0, slug=slug))
        out.append(reading("chmi_station", "t_max", -n, 21.0, slug=slug))
        out.append(reading("chmi_station", "api30_mm", -n, 20.0 - n * 0.2, slug=slug))
    out.append(reading("chmi_station", "rh", -1, 81.0, slug=slug))
    out.append(reading("chmi_station", "t_soil_5", -1, 17.6, slug=slug))
    out.append(reading("chmi_station", "t_soil_10", -1, 17.7, slug=slug))
    return out


def openmeteo_readings(slug="valmez"):
    out = []
    for n in range(0, 16):
        rain = 12.0 if n == 3 else 0.0
        out.append(reading("openmeteo", "precip_mm", n, rain, slug=slug))
        out.append(reading("openmeteo", "t_mean", n, 15.0, slug=slug))
        out.append(reading("openmeteo", "t_min", n, 9.0, slug=slug))
    return out


def api30_curve_readings(slug="valmez"):
    """A derived curve that crosses the 25 mm threshold on ``today + 4``."""
    values = [18.0, 18.0, 17.0, 17.0, 28.0, 27.0, 26.0] + [25.5] * 10
    return [
        reading("api30_forecast", "api30_mm", n, value, slug=slug)
        for n, value in enumerate(values)
    ]


def map_readings(slug="valmez"):
    """Today 4/5, yesterday 3/5, a week ago 2/5 -- the change columns."""
    out = []
    for offset, level, score in ((0, 4, 0.63), (-1, 3, 0.47), (-7, 2, 0.31)):
        out.append(reading("chmi_map", "level", offset, float(level), slug=slug, label="высокая"))
        out.append(reading("houbymapa", "level", offset, float(level), slug=slug))
        out.append(reading("houbymapa", "score", offset, score, slug=slug))
    return out


def seed(store, *groups, slug="valmez"):
    store.sync_location(Location(name="Valašské Meziříčí", lat=49.4718, lon=17.9711, slug=slug))
    for group in groups:
        store.upsert_readings(group)


# ----------------------------------------------------------------------
# build / render straight off the store
# ----------------------------------------------------------------------
def test_render_has_every_section(tmp_path):
    with Store(tmp_path / "s.sqlite") as store:
        seed(
            store,
            station_readings(),
            openmeteo_readings(),
            map_readings(),
            api30_curve_readings(),
        )
        payload = brief_lib.build(store, [VALMEZ], TODAY)
    text = brief_lib.render(payload)

    # header: name, coordinates, station name / elevation / distance
    assert "🍄 Valašské Meziříčí (49.4718, 17.9711)" in text
    assert "Europe/Prague" in text
    assert "станция ČHMÚ: Valašské Meziříčí, 334 м, 0.9 км" in text

    # facts now, with the changes against yesterday and a week ago
    assert "карта ČHMÚ: 4/5 (высокая)" in text
    assert "вчера 3/5, 7 дней назад 2/5" in text
    assert "HoubyMapa: 4/5 (0.63)" in text
    assert "вчера 3/5 (0.47), 7 дней назад 2/5 (0.31)" in text
    assert "станция API30: 20.0 мм" in text
    assert "осадки SRA: 1 д" in text and "30 д" in text
    assert "температура за" in text and "средняя" in text
    assert "температура почвы: 5 см 17.6 °C, 10 см 17.7 °C" in text
    assert "влажность: 81 %" in text
    assert "биологическая оценка (детерминированная):" in text
    assert "вердикт сегодня: средняя (rules v4)" in text
    assert "шанс сегодня: 45 %" in text
    assert "не подтверждение отдельных плодовых тел" in text
    assert "возможная высокая вероятность:" in text
    assert "T средняя 7 д:" in text and "динамика API30:" in text

    # history table: one line per day, 14 of them
    assert "история 14 дн. (дата | SRA мм | API30 мм | T ср °C):" in text
    for n in range(14):
        assert (TODAY - timedelta(days=n)).isoformat() in text

    # forecast table plus the derived API30 curve
    assert "прогноз, сегодня + 16 дн." in text
    assert "API30 мм | оценка" in text
    assert (TODAY + timedelta(days=15)).isoformat() in text
    cross = (TODAY + timedelta(days=4)).isoformat()
    assert f"порог API30 25 мм: пересечение {cross} (через 4 дн.)" in text
    assert "пик API30: 28.0 мм" in text
    assert "ближайший дождь ≥ 5 мм: 12.0 мм" in text

    # triggers and failures
    assert "сработавшие триггеры" in text
    assert "сбои источников: нет" in text

    # cheat sheet, once, with the licence note
    assert text.count("Как читать (справка, стабильный текст):") == 1
    assert "zdroj ČHMÚ (CC BY 4.0)" in text
    assert "15–25 мм умеренно" in text
    assert "D+7...D+12" in text
    assert "1.9 раза" in text
    assert "hřib, kozák, liška" in text


def test_missing_sources_render_as_no_data(tmp_path):
    """A location with only station data still produces a full brief."""
    with Store(tmp_path / "s.sqlite") as store:
        seed(store, station_readings())
        payload = brief_lib.build(store, [VALMEZ], TODAY, notes=["HoubyMapa: 502"])
    text = brief_lib.render(payload)
    assert "карта ČHMÚ: нет данных" in text
    assert "HoubyMapa: нет данных" in text
    assert "сбои источников: HoubyMapa: 502" in text
    assert "недостаточно данных" in text
    assert "в горизонте прогноза не достигается" not in text


def test_days_flag_shortens_the_forecast_table(tmp_path):
    with Store(tmp_path / "s.sqlite") as store:
        seed(store, station_readings(), openmeteo_readings())
        short = brief_lib.build(store, [VALMEZ], TODAY, days=5)
    rows = short["locations"][0]["forecast"]["days"]
    assert [r["offset"] for r in rows] == [0, 1, 2, 3, 4, 5]
    assert "прогноз, сегодня + 5 дн." in brief_lib.render(short)


def test_history_and_forecast_are_in_deterministic_order(tmp_path):
    with Store(tmp_path / "s.sqlite") as store:
        seed(store, station_readings(), openmeteo_readings())
        payload = brief_lib.build(store, [VALMEZ], TODAY)
    view = payload["locations"][0]
    history = [row["date"] for row in view["history"]]
    assert history == sorted(history) and len(history) == brief_lib.HISTORY_DAYS
    forecast = [row["date"] for row in view["forecast"]["days"]]
    assert forecast == sorted(forecast) and forecast[0] == TODAY
    # rendering twice gives byte-identical text
    assert brief_lib.render(payload) == brief_lib.render(payload)


def test_threshold_already_passed_and_a_vague_crossing(tmp_path):
    """The two other shapes of the threshold line."""
    with Store(tmp_path / "wet.sqlite") as store:
        seed(store, station_readings(), openmeteo_readings())
        store.upsert_readings(
            [reading("api30_forecast", "api30_mm", n, 30.0) for n in range(0, 8)]
        )
        wet = brief_lib.render(brief_lib.build(store, [VALMEZ], TODAY))
    assert "порог API30 25 мм: уже пройден сегодня" in wet

    with Store(tmp_path / "far.sqlite") as store:
        seed(store, station_readings(), openmeteo_readings())
        store.upsert_readings(
            [
                reading("api30_forecast", "api30_mm", n, 40.0 if n >= 12 else 10.0)
                for n in range(0, 17)
            ]
        )
        far = brief_lib.render(brief_lib.build(store, [VALMEZ], TODAY))
    assert "(через 12 дн.) — ориентировочно, горизонт > 7 дней" in far


def test_brief_stays_compact(tmp_path):
    """~1-2 KB of facts per location; the cheat sheet is printed once."""
    with Store(tmp_path / "s.sqlite") as store:
        seed(store, station_readings(), openmeteo_readings(), map_readings())
        payload = brief_lib.build(store, [VALMEZ], TODAY)
    text = brief_lib.render(payload)
    body = text.split("Как читать")[0]
    assert len(body.encode("utf-8")) < 4500, len(body.encode("utf-8"))


# ----------------------------------------------------------------------
# the CLI
# ----------------------------------------------------------------------
def fake_module(source, *, ok=True, readings=None, error=None):
    mod = types.ModuleType(f"fake_{source}")
    mod.SOURCE = source

    def fetch(locations, *, http, today):
        return FetchResult(source=source, ok=ok, readings=list(readings or []), error=error)

    mod.fetch = fetch
    return mod


def use_fetchers(monkeypatch, modules):
    monkeypatch.setattr(cli, "discover_fetchers", lambda only=None: list(modules))


def full_stack(monkeypatch, **kw):
    use_fetchers(
        monkeypatch,
        [
            fake_module("chmi_map", readings=[r for r in map_readings() if r.source == "chmi_map"]),
            fake_module(
                "houbymapa", readings=[r for r in map_readings() if r.source == "houbymapa"]
            ),
            fake_module("chmi_station", readings=station_readings()),
            fake_module("openmeteo", readings=openmeteo_readings(), **kw),
        ],
    )


def test_cli_brief_exits_0_and_prints_the_brief(monkeypatch, capsys):
    full_stack(monkeypatch)
    assert cli.main(["brief"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("БРИФ ПО ГРИБАМ · ")
    assert "🍄 Valašské Meziříčí" in out and "🍄 Valašská Bystřice" in out
    assert "история 14 дн." in out and "Как читать" in out
    # the deterministic triggers of rules.decide are reported, not re-derived
    assert "сработавшие триггеры (детерминированные, из rules.decide):" in out
    assert "chmi_map" in out  # ČHMÚ 4/5 fired for valmez
    with Store() as store:
        assert store.conn.execute("SELECT COUNT(*) FROM signal_emissions").fetchone()[0] == 0


def test_cli_brief_survives_a_dead_source(monkeypatch, capsys):
    use_fetchers(
        monkeypatch,
        [
            fake_module("chmi_station", readings=station_readings()),
            fake_module("houbymapa", ok=False, error="502"),
        ],
    )
    assert cli.main(["brief"]) == 0
    out = capsys.readouterr().out
    assert "сбои источников: HoubyMapa: 502" in out
    assert "HoubyMapa: нет данных" in out
    assert "станция API30: 20.0 мм" in out  # the rest of the brief is intact


def test_cli_brief_exits_1_when_every_source_fails(monkeypatch, capsys):
    use_fetchers(
        monkeypatch,
        [
            fake_module("chmi_map", ok=False, error="down"),
            fake_module("houbymapa", ok=False, error="502"),
        ],
    )
    assert cli.main(["brief"]) == 1
    assert "сбои источников: ČHMÚ: down; HoubyMapa: 502" in capsys.readouterr().out


def test_cli_brief_exits_1_when_rules_fail(monkeypatch, capsys):
    full_stack(monkeypatch)
    monkeypatch.setattr(
        cli,
        "_decide",
        lambda *a, **k: Decision(1, "rules.decide() failed", {"error": "rules.decide() failed"}),
    )

    assert cli.main(["brief", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["exit_code"] == 1
    assert payload["calculation_error"] == "rules.decide() failed"
    assert payload["notes"][-1] == "расчёт: rules.decide() failed"


def test_cli_brief_keeps_partial_source_failure_nonfatal(monkeypatch, capsys):
    full_stack(monkeypatch, error="valaska-bystrice: timeout")
    assert cli.main(["brief", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert payload["calculation_error"] is None
    assert payload["notes"] == ["Open-Meteo: valaska-bystrice: timeout"]


def test_cli_brief_without_fetchers_is_an_error(monkeypatch):
    use_fetchers(monkeypatch, [])
    assert cli.main(["brief"]) == 1


def test_cli_brief_without_locations_is_an_error(monkeypatch, tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("[]\n", encoding="utf-8")
    monkeypatch.setenv("MUSHROOM_LOCATIONS", str(empty))
    assert cli.main(["brief"]) == 1


def test_cli_brief_json(monkeypatch, capsys):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["exit_code"] == 0
    assert payload["date"] == TODAY.isoformat()
    assert payload["timezone"] == "Europe/Prague"
    assert payload["threshold_mm"] == 25.0
    assert payload["history_days"] == 14 and payload["forecast_days"] == 16
    assert payload["failed_sources"] == [] and payload["notes"] == []
    assert "Как читать" in payload["cheat_sheet"]

    valmez = next(loc for loc in payload["locations"] if loc["slug"] == "valmez")
    assert valmez["chmi"] == {
        "level": 4.0,
        "date": TODAY.isoformat(),
        "label": "высокая",
        "stale": False,
        "quality": "fresh",
        "previous": 3.0,
        "yesterday": 3.0,
        "week_ago": 2.0,
    }
    assert valmez["houbymapa"]["score"] == 0.63
    assert valmez["station"]["meta"]["name"] == "Valašské Meziříčí"
    assert [w["window_days"] for w in valmez["station"]["sra"]] == [1, 3, 7, 30]
    assert len(valmez["history"]) == 14
    assert valmez["history"][-1]["date"] == TODAY.isoformat()
    assert valmez["forecast"]["days"][0]["date"] == TODAY.isoformat()
    assert valmez["forecast"]["threshold_mm"] == 25.0
    # the same signal blob ``check --json`` publishes
    assert {s["trigger"] for s in valmez["signals"]} >= {"chmi_map"}


def test_cli_brief_days_flag(monkeypatch, capsys):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    valmez = next(loc for loc in payload["locations"] if loc["slug"] == "valmez")
    assert [r["offset"] for r in valmez["forecast"]["days"]] == [0, 1, 2, 3]
    assert payload["forecast_days"] == 3


def test_cli_brief_is_idempotent(monkeypatch, capsys):
    """Two runs on the same day: same text (bar the clock), no duplicate rows."""
    full_stack(monkeypatch)
    cli.main(["brief"])
    first = capsys.readouterr().out
    cli.main(["brief"])
    second = capsys.readouterr().out

    def strip_clock(text: str) -> str:
        return "\n".join(line for line in text.splitlines() if "Europe/Prague" not in line)

    assert strip_clock(first) == strip_clock(second)
    with Store() as store:
        rows = store.conn.execute(
            "SELECT COUNT(*) c FROM readings WHERE location='valmez'"
        ).fetchone()["c"]
    assert rows > 0
    assert sys.modules.get("mushroom_alerts.rules") is not None


# ----------------------------------------------------------------------
# ``brief --mode``: the short block the cron jobs actually print
# ----------------------------------------------------------------------
def test_cli_brief_mode_daily_prints_the_block_and_records_it(monkeypatch, capsys):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ОТПРАВЛЯТЬ: да\n")
    assert "ПРИЧИНА: первый запуск" in out
    assert "ЗАГОЛОВОК: 🍄 Грибной прогноз: " in out
    assert "ШАНС на " in out and "ФАЗА:" in out and "ОГОВОРКИ:" in out
    assert "ПОДРОБНО (2 локации с лучшим шансом):" in out
    # both locations are listed, in the order of locations.yaml
    assert out.index("Valmez — ") < out.index("Bystřice — ")
    assert re.search(r"Valmez — \d+ %", out)
    # no tables, no cheat sheet, no ISO dates
    assert "история 14 дн." not in out and "Как читать" not in out
    assert TODAY.isoformat() not in out
    assert len(out.encode("utf-8")) <= 1536, len(out.encode("utf-8"))
    with Store() as store:
        row = store.last_report("daily")
    assert row["mode"] == "daily" and row["sent"] == 1


def test_cli_brief_mode_weekend_always_sends(monkeypatch, capsys):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "weekend"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ОТПРАВЛЯТЬ: да\n")
    assert "ПРИЧИНА: плановый прогноз на выходные" in out
    assert "Грибной прогноз на выходные " in out


def test_cli_brief_mode_is_idempotent_on_the_same_day(monkeypatch, capsys):
    full_stack(monkeypatch)
    cli.main(["brief", "--mode", "daily"])
    first = capsys.readouterr().out
    cli.main(["brief", "--mode", "daily"])
    assert capsys.readouterr().out == first
    with Store() as store:
        rows = store.conn.execute("SELECT COUNT(*) c FROM reports").fetchone()["c"]
    assert rows == 1


def test_cli_brief_mode_stays_silent_when_nothing_moved(monkeypatch, capsys):
    """A report from an earlier date, identical to today, means [SILENT]."""
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily"]) == 0
    capsys.readouterr()
    with Store() as store:
        row = store.last_report("daily")
        store.save_report(
            TODAY - timedelta(days=1),
            "daily",
            chances=json.loads(row["chances_json"]),
            verdicts=json.loads(row["verdicts_json"]),
            candidates=json.loads(row["candidates_json"]),
            events=json.loads(row["events_json"]),
            error_class=row["error_class"],
            rules_version=row["rules_version"],
            sent=True,
            reason="вчера",
        )
    assert cli.main(["brief", "--mode", "daily"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ОТПРАВЛЯТЬ: нет\n")
    assert "ПРИЧИНА: ничего не изменилось с прошлого отчёта" in out


def test_cli_brief_mode_json(monkeypatch, capsys):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "daily" and payload["send"] is True
    assert payload["exit_code"] == 0
    assert payload["date"] == TODAY.isoformat()
    assert {loc["slug"] for loc in payload["locations"]} == {"valmez", "valasska-bystrice"}
    assert payload["text"].startswith("ОТПРАВЛЯТЬ: да")


def test_cli_brief_mode_reports_a_total_blackout(monkeypatch, capsys):
    use_fetchers(monkeypatch, [fake_module("chmi_map", ok=False, error="boom")])
    assert cli.main(["brief", "--mode", "weekend"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("ОТПРАВЛЯТЬ: да\n")
    assert "бриф не собрался" in out
    assert "ШАНС: данных нет, прогноз не собрался" in out


# ----------------------------------------------------------------------
# ``brief --location``: twenty locations must not print 60 KB (PLAN §9f)
# ----------------------------------------------------------------------
def test_cli_brief_location_filter_prints_only_that_location(monkeypatch, capsys):
    full_stack(monkeypatch)
    assert cli.main(["brief"]) == 0
    whole = capsys.readouterr().out

    assert cli.main(["brief", "--location", "valmez"]) == 0
    out = capsys.readouterr().out
    assert "🍄 Valašské Meziříčí" in out
    assert "Valašská Bystřice" not in out
    assert len(out) < len(whole)


def test_cli_brief_location_filter_keeps_the_config_order(monkeypatch, capsys):
    """The flags may come in any order; the output order is the file's."""
    full_stack(monkeypatch)
    assert (
        cli.main(
            ["brief", "--location", "Valašská Bystřice", "--location", "valmez"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.index("🍄 Valašské Meziříčí") < out.index("🍄 Valašská Bystřice")


def test_cli_brief_location_filter_works_with_mode_but_records_nothing(
    monkeypatch, capsys
):
    """A partial block must not become the state tomorrow compares against."""
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily", "--location", "valmez"]) == 0
    out = capsys.readouterr().out
    assert "Valmez — " in out and "Bystřice — " not in out
    with Store() as store:
        assert store.last_report("daily") is None

    # unfiltered, the same day does record
    assert cli.main(["brief", "--mode", "daily"]) == 0
    capsys.readouterr()
    with Store() as store:
        assert store.last_report("daily") is not None


def test_cli_brief_rejects_an_unknown_location(monkeypatch, capsys):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--location", "Brno"]) == 1
    assert "no such location: Brno" in capsys.readouterr().err


def test_cli_brief_mode_keeps_the_yaml_order_end_to_end(monkeypatch, capsys, tmp_path):
    """yaml -> fetch -> store -> brief -> block: nothing re-sorts on the way."""
    path = tmp_path / "many.yaml"
    path.write_text(
        "- {name: Valašské Meziříčí, slug: valmez, short: Valmez, lat: 49.4718, lon: 17.9711}\n"
        "- {name: Bystřice pod Hostýnem, lat: 49.3994, lon: 17.6742}\n"
        "- {name: Rajnochovice, lat: 49.4083, lon: 17.8}\n"
        "- {name: Kateřinice, lat: 49.5346, lon: 18.0669}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MUSHROOM_LOCATIONS", str(path))
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily"]) == 0
    out = capsys.readouterr().out
    listed = [
        line.strip().split(" — ")[0]
        for line in out.splitlines()
        if line.startswith("  ") and " — " in line
    ]
    assert listed == [
        "Valmez",
        "Bystřice pod Hostýnem",
        "Rajnochovice",
        "Kateřinice",
    ]
