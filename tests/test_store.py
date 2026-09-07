from __future__ import annotations

from datetime import date

import pytest

from mushroom_alerts.base import Location, Reading
from mushroom_alerts.store import Store, db_path


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.sqlite") as s:
        yield s


def r(day: int, value: float, *, metric="level", issued=None, source="chmi_map"):
    meta = {"issued": issued} if issued else {"px": [1, 2]}
    return Reading(
        source=source,
        location="valmez",
        date=date(2026, 9, day),
        metric=metric,
        value=value,
        text="ne 6. 9.",
        meta=meta,
    )


def test_db_path_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MUSHROOM_DB", raising=False)
    assert db_path().name == "state.sqlite"
    monkeypatch.setenv("MUSHROOM_DB", str(tmp_path / "x.sqlite"))
    assert db_path() == tmp_path / "x.sqlite"


def test_upsert_is_idempotent(store):
    assert store.upsert_readings([r(6, 3)]) == 1
    store.upsert_readings([r(6, 3)])
    store.upsert_readings([r(6, 3)])
    rows = store.conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
    assert rows == 1


def test_upsert_overwrites_the_value(store):
    store.upsert_readings([r(6, 3)])
    store.upsert_readings([r(6, 4)])
    got = store.get_reading("chmi_map", "valmez", "level", date(2026, 9, 6))
    assert got is not None and got.value == 4.0
    assert got.meta == {"px": [1, 2]}


def test_metrics_and_days_are_separate_rows(store):
    store.upsert_readings([r(6, 3), r(7, 4), r(6, 0.47, metric="score")])
    assert store.conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"] == 3


def test_forecast_and_observation_coexist(store):
    store.upsert_readings([r(10, 3), r(10, 5, issued="2026-09-07")])
    assert store.conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"] == 2
    # the observation is what get_reading/latest see
    assert store.get_reading("chmi_map", "valmez", "level", date(2026, 9, 10)).value == 3


def test_forecasts_are_mirrored(store):
    store.upsert_readings([r(10, 5, issued="2026-09-07")])
    store.upsert_readings([r(10, 6, issued="2026-09-07")])  # same issue -> update
    rows = store.forecast_series("chmi_map", "valmez", "level", "2026-09-07")
    assert len(rows) == 1 and rows[0]["value"] == 6.0
    assert rows[0]["target_date"] == "2026-09-10"


def test_latest_and_before(store):
    store.upsert_readings([r(4, 1), r(5, 2), r(6, 3)])
    assert store.latest("chmi_map", "valmez", "level").value == 3
    assert store.latest("chmi_map", "valmez", "level", before=date(2026, 9, 6)).value == 2
    assert store.latest("chmi_map", "valmez", "level", before=date(2026, 9, 1)) is None
    assert store.latest("chmi_map", "nowhere", "level") is None


def test_series(store):
    store.upsert_readings([r(4, 1), r(5, 2), r(6, 3)])
    values = [x.value for x in store.series("chmi_map", "valmez", "level", since=date(2026, 9, 5))]
    assert values == [2, 3]
    assert len(store.series("chmi_map", "valmez", "level")) == 3
    assert store.series("chmi_map", "valmez", "level", until=date(2026, 9, 4))[0].value == 1


def test_latest_snapshot_takes_the_newest_of_each_metric(store):
    store.upsert_readings(
        [
            r(5, 2),
            r(6, 3),
            r(6, 0.47, metric="score", source="houbymapa"),
            r(4, 0.10, metric="score", source="houbymapa"),
        ]
    )
    snap = {(x.source, x.metric): x.value for x in store.latest_snapshot("valmez")}
    assert snap == {("chmi_map", "level"): 3.0, ("houbymapa", "score"): 0.47}


def test_params_roundtrip_and_merge(store):
    loc = Location(name="Valašské Meziříčí", lat=49.4718, lon=17.9711, slug="valmez")
    store.sync_location(loc)
    assert store.get_params("valmez") == {}
    store.set_params("valmez", {"chmi_px": [2308, 967]})
    store.set_params("valmez", {"houbymapa_cell": [49.55, 17.89]})
    assert store.get_params("valmez") == {
        "chmi_px": [2308, 967],
        "houbymapa_cell": [49.55, 17.89],
    }
    store.invalidate_params("valmez")
    assert store.get_params("valmez") == {}


def test_moving_a_location_drops_its_derived_params(store):
    loc = Location(name="X", lat=49.0, lon=18.0, slug="x")
    store.sync_location(loc)
    store.set_params("x", {"chmi_px": [1, 1]})
    store.sync_location(loc)  # same coords -> cache kept
    assert store.get_params("x") == {"chmi_px": [1, 1]}
    store.sync_location(Location(name="X", lat=49.5, lon=18.0, slug="x"))
    assert store.get_params("x") == {}


def test_set_params_without_a_location_row(store):
    store.set_params("ghost", {"a": 1})
    assert store.get_params("ghost") == {"a": 1}


def test_forget_location_keeps_readings(store):
    store.sync_location(Location(name="V", lat=49.0, lon=18.0, slug="valmez"))
    store.upsert_readings([r(6, 3)])
    store.forget_location("valmez")
    assert store.get_params("valmez") == {}
    assert store.latest("chmi_map", "valmez", "level") is not None


def test_notifications(store):
    assert store.last_notification("valmez") is None
    store.add_notification(date(2026, 9, 5), "valmez", "chmi_level", "old")
    store.add_notification(date(2026, 9, 7), "valmez", "chmi_level", "new")
    store.add_notification(date(2026, 9, 6), "valmez", "rain", "rain fell")
    assert store.last_notification("valmez")["text"] == "new"
    assert store.last_notification("valmez", "rain")["text"] == "rain fell"
    assert store.last_notification("valmez", "nothing") is None
    assert store.last_notification("elsewhere") is None


def test_upsert_forecasts_ignores_observations(store):
    assert store.upsert_forecasts([r(6, 3)]) == 0
    assert store.upsert_readings([]) == 0
