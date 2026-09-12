from __future__ import annotations

import json
import sys
import types
from datetime import date
from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest

from mushroom_alerts import __main__ as cli
from mushroom_alerts.base import Decision, FetchResult, Reading

SHIPPED = Path(__file__).resolve().parent.parent / "locations.yaml"
TODAY = date(2026, 9, 7)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every CLI test gets its own DB and its own copy of locations.yaml."""
    locations = tmp_path / "locations.yaml"
    locations.write_text(SHIPPED.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("MUSHROOM_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("MUSHROOM_LOCATIONS", str(locations))
    monkeypatch.delenv("MUSHROOM_RECORD", raising=False)
    monkeypatch.setattr(cli, "Http", lambda **kw: _NullHttp())
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(cli, "date", FrozenDate)
    sys.modules.pop("mushroom_alerts.rules", None)
    yield tmp_path


class _NullHttp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def fake_module(source: str, *, ok=True, readings=None, error=None, boom=False):
    mod = types.ModuleType(f"fake_{source}")
    mod.SOURCE = source

    def fetch(locations, *, http, today):
        if boom:
            raise RuntimeError("kaboom")
        return FetchResult(source=source, ok=ok, readings=list(readings or []), error=error)

    mod.fetch = fetch
    return mod


def reading(source, slug, metric, value, **meta):
    return Reading(
        source=source, location=slug, date=TODAY, metric=metric, value=value, meta=meta or None
    )


def use_fetchers(monkeypatch, modules):
    monkeypatch.setattr(cli, "discover_fetchers", lambda only=None: list(modules))


def install_rules(monkeypatch, decide):
    mod = types.ModuleType("mushroom_alerts.rules")
    mod.__spec__ = ModuleSpec("mushroom_alerts.rules", None)
    mod.decide = decide
    monkeypatch.setitem(sys.modules, "mushroom_alerts.rules", mod)
    return mod


# ----------------------------------------------------------------------
# exit codes
# ----------------------------------------------------------------------
def test_check_returns_10_when_rules_signal(monkeypatch, capsys):
    use_fetchers(monkeypatch, [fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 4)])])
    install_rules(monkeypatch, lambda locs, res, *, store, today: Decision(10, "🍄 signal!"))
    assert cli.main(["check"]) == 10
    assert "signal!" in capsys.readouterr().out


def test_check_returns_1_when_every_source_fails(monkeypatch, capsys):
    use_fetchers(
        monkeypatch,
        [fake_module("chmi_map", ok=False, error="down"), fake_module("houbymapa", boom=True)],
    )
    assert cli.main(["check"]) == 1
    err = capsys.readouterr().err
    assert "down" in err and "kaboom" in err


def test_one_dead_source_is_not_an_error(monkeypatch, capsys):
    use_fetchers(
        monkeypatch,
        [
            fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 3)]),
            fake_module("houbymapa", ok=False, error="502"),
        ],
    )
    assert cli.main(["check"]) == 0
    captured = capsys.readouterr()
    assert "ČHMÚ 3/5" in captured.out
    assert "HoubyMapa: 502" in captured.err


def test_all_sources_failing_overrides_a_rules_signal(monkeypatch):
    use_fetchers(monkeypatch, [fake_module("chmi_map", ok=False, error="down")])
    install_rules(monkeypatch, lambda locs, res, *, store, today: Decision(10, "stale signal"))
    assert cli.main(["check"]) == 1


def test_broken_rules_module_is_an_error(monkeypatch, capsys):
    use_fetchers(monkeypatch, [fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 3)])])

    def explode(locs, res, *, store, today):
        raise ZeroDivisionError

    install_rules(monkeypatch, explode)
    assert cli.main(["check"]) == 1


def test_rules_returning_junk_is_an_error(monkeypatch):
    use_fetchers(monkeypatch, [fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 3)])])
    install_rules(monkeypatch, lambda locs, res, *, store, today: "not a Decision")
    assert cli.main(["check"]) == 1


def test_no_fetchers_at_all_is_an_error(monkeypatch):
    use_fetchers(monkeypatch, [])
    assert cli.main(["check"]) == 1


def test_no_locations_is_an_error(monkeypatch, tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("[]\n", encoding="utf-8")
    monkeypatch.setenv("MUSHROOM_LOCATIONS", str(empty))
    assert cli.main(["check"]) == 1


def test_a_crash_in_the_cli_itself_is_exit_1(monkeypatch):
    monkeypatch.setattr(cli, "load_locations", lambda *a, **k: 1 / 0)
    assert cli.main(["check"]) == 1


# ----------------------------------------------------------------------
# behaviour
# ----------------------------------------------------------------------
def test_check_persists_readings_and_caches_params(monkeypatch):
    use_fetchers(
        monkeypatch,
        [
            fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 3, px=[2308, 967])]),
            fake_module(
                "houbymapa",
                readings=[reading("houbymapa", "valmez", "score", 0.47, cell=[49.55, 17.89], distance_km=10.5)],
            ),
        ],
    )
    assert cli.main(["check"]) == 0
    from mushroom_alerts.store import Store

    with Store() as store:
        assert store.latest("chmi_map", "valmez", "level").value == 3
        params = store.get_params("valmez")
        assert params["chmi_px"] == [2308, 967]
        assert params["houbymapa_cell"] == [49.55, 17.89]


def test_check_is_idempotent_across_runs(monkeypatch):
    use_fetchers(monkeypatch, [fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 3)])])
    cli.main(["check"])
    cli.main(["check"])
    from mushroom_alerts.store import Store

    with Store() as store:
        assert store.conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"] == 1


def test_check_json(monkeypatch, capsys):
    use_fetchers(
        monkeypatch,
        [
            fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 3)]),
            fake_module("houbymapa", ok=False, error="502"),
        ],
    )
    assert cli.main(["check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert payload["notes"] == ["HoubyMapa: 502"]
    sources = {s["source"]: s for s in payload["sources"]}
    assert sources["chmi_map"]["readings"][0]["value"] == 3
    assert sources["houbymapa"]["ok"] is False


def test_only_filters_sources(monkeypatch):
    seen: list[str] = []

    def spy(only=None):
        seen.append(only)
        return []

    monkeypatch.setattr(cli, "discover_fetchers", spy)
    cli.main(["check", "--only", "chmi_map,houbymapa"])
    assert seen == [["chmi_map", "houbymapa"]]


def test_discover_fetchers_finds_the_real_modules():
    sources = {m.SOURCE for m in cli.discover_fetchers()}
    assert {"chmi_map", "houbymapa"} <= sources
    assert {m.SOURCE for m in cli.discover_fetchers(["houbymapa"])} == {"houbymapa"}
    assert cli.discover_fetchers(["no_such_source"]) == []
    # module-name and bare-name forms both work
    assert {m.SOURCE for m in cli.discover_fetchers(["fetch_chmi_map"])} == {"chmi_map"}


def test_discover_fetchers_reports_import_failure(monkeypatch):
    real_import = cli.importlib.import_module

    def broken_import(name):
        if name == "mushroom_alerts.fetch_houbymapa":
            raise ImportError("missing parser")
        return real_import(name)

    monkeypatch.setattr(cli.importlib, "import_module", broken_import)
    modules = cli.discover_fetchers(["houbymapa"])
    assert len(modules) == 1
    result = modules[0].fetch([], http=None, today=TODAY)
    assert result.ok is False
    assert result.source == "houbymapa"
    assert result.error == "import failed: ImportError: missing parser"


def test_status_reads_the_store_without_fetching(monkeypatch, capsys):
    use_fetchers(monkeypatch, [fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 4)])])
    cli.main(["check"])
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "ČHMÚ 4/5" in out
    assert "Valašská Bystřice: нет данных" in out


def test_status_json(monkeypatch, capsys):
    use_fetchers(monkeypatch, [fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 4)])])
    cli.main(["check"])
    capsys.readouterr()
    cli.main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["locations"][0]["location"]["slug"] == "valmez"
    assert payload["locations"][0]["readings"][0]["value"] == 4


def test_add_list_del(capsys, isolated):
    assert cli.main(["add", "Rožnov pod Radhoštěm", "49.4586", "18.1436"]) == 0
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    assert "roznov-pod-radhostem" in capsys.readouterr().out
    assert cli.main(["del", "roznov"]) == 1  # partial names are not matched
    capsys.readouterr()
    assert cli.main(["del", "Rožnov pod Radhoštěm"]) == 0
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    assert "roznov" not in capsys.readouterr().out


def test_add_duplicate_is_an_error(capsys):
    assert cli.main(["add", "Valašské Meziříčí", "49.4718", "17.9711"]) == 1


def test_del_invalidates_the_params_cache(monkeypatch):
    from mushroom_alerts.store import Store

    with Store() as store:
        store.set_params("valmez", {"chmi_px": [1, 2]})
    assert cli.main(["del", "valmez"]) == 0
    with Store() as store:
        assert store.get_params("valmez") == {}


def test_add_invalidates_a_stale_cache_for_the_same_slug():
    from mushroom_alerts.store import Store

    with Store() as store:
        store.set_params("kelc", {"chmi_px": [9, 9]})
    assert cli.main(["add", "Kelč", "49.4899", "17.8069"]) == 0
    with Store() as store:
        assert store.get_params("kelc") == {}


# ----------------------------------------------------------------------
# report shape
# ----------------------------------------------------------------------
def multi_day(source, slug, metric, values, meta=None):
    """``values`` are ``(day offset from TODAY, value)`` pairs."""
    from datetime import timedelta

    return [
        Reading(
            source=source,
            location=slug,
            date=TODAY + timedelta(days=offset),
            metric=metric,
            value=value,
            meta=dict(meta or {}, **({"issued": TODAY.isoformat()} if offset > 0 else {})),
        )
        for offset, value in values
    ]


def test_check_collapses_multi_day_sources(monkeypatch, capsys):
    """35 days x 9 metrics must not end up in a Telegram message."""
    station = (
        multi_day("chmi_station", "valmez", "sra_mm", [(-2, 17.2), (-1, 0.2), (0, 2.3)])
        + multi_day("chmi_station", "valmez", "t_mean", [(-2, 16.0), (-1, 15.0), (0, 14.2)])
        + multi_day("chmi_station", "valmez", "api30_mm", [(-2, 5.0), (-1, 20.3)])
        + multi_day("chmi_station", "valmez", "rh", [(-1, 81.0)])
    )
    forecast = multi_day(
        "openmeteo", "valmez", "precip_mm", [(0, 0.0), (1, 1.0), (3, 9.8), (5, 0.4)]
    ) + multi_day("api30_forecast", "valmez", "api30_mm", [(0, 19.0), (2, 24.0), (4, 31.0)])
    use_fetchers(
        monkeypatch,
        [
            fake_module("chmi_station", readings=station),
            fake_module("openmeteo", readings=forecast[:4]),
            fake_module("api30_forecast", readings=forecast[4:]),
        ],
    )
    assert cli.main(["check"]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if "Meziříčí" in l)
    assert len(line) < 250, line
    assert "станция API30 20 mm, SRA 3d 20 mm, T 14.2 °C" in line
    assert "дождь 9.8 mm" in line
    assert "API30 сегодня 19 mm, max 31 mm" in line and "порог 25 mm" in line


def test_status_renders_the_rules_line(monkeypatch, capsys):
    use_fetchers(
        monkeypatch,
        [
            fake_module("chmi_map", readings=[reading("chmi_map", "valmez", "level", 3)]),
            fake_module(
                "chmi_station",
                readings=multi_day(
                    "chmi_station", "valmez", "sra_mm", [(-2, 17.2), (-1, 0.2), (0, 2.3)]
                )
                + multi_day("chmi_station", "valmez", "api30_mm", [(-1, 20.3)]),
            ),
        ],
    )
    cli.main(["check"])
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "ČHMÚ 3/5" in out and "станция API30 20 mm" in out and "SRA 3d 20 mm" in out


def test_status_json_serializes_the_populated_shared_view(monkeypatch, capsys):
    use_fetchers(
        monkeypatch,
        [
            fake_module(
                "chmi_station",
                readings=multi_day(
                    "chmi_station", "valmez", "sra_mm", [(-1, 1.0), (0, 2.0)]
                )
                + multi_day("chmi_station", "valmez", "t_mean", [(-1, 14.0), (0, 15.0)]),
            )
        ],
    )
    assert cli.main(["check"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    view = payload["locations"][0]["view"]
    assert view["station"]["series_points"]["sra_mm"][TODAY.isoformat()]["value"] == 2.0
    assert view["biological"]["rules_version"] == "2"


def test_status_weekly_does_not_crash(capsys):
    assert cli.main(["status", "--weekly"]) == 0
    out = capsys.readouterr().out
    assert "Valašské Meziříčí" in out and "TODO" not in out
