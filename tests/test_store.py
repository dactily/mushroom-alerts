from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from mushroom_alerts.base import Location, Reading
from mushroom_alerts.store import SCHEMA_VERSION, Store, db_path


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


def test_forecast_runs_keep_two_runs_from_the_same_day(store):
    first = Reading(
        "openmeteo",
        "valmez",
        date(2026, 9, 7),
        "precip_mm",
        1.0,
        meta={"provisional": True, "model": "best_match"},
    )
    second = Reading(
        "openmeteo",
        "valmez",
        date(2026, 9, 7),
        "precip_mm",
        2.0,
        meta={"provisional": True, "model": "best_match"},
    )
    store.upsert_readings(
        [first], retrieved_at=datetime(2026, 9, 7, 6, tzinfo=timezone.utc)
    )
    store.upsert_readings(
        [second], retrieved_at=datetime(2026, 9, 7, 17, tzinfo=timezone.utc)
    )
    runs = store.conn.execute(
        "SELECT * FROM forecast_runs WHERE source='openmeteo' ORDER BY retrieved_at"
    ).fetchall()
    assert len(runs) == 2
    assert runs[0]["run_id"] != runs[1]["run_id"]
    first_points = store.forecast_run_points(runs[0]["run_id"], location="valmez")
    second_points = store.forecast_run_points(runs[1]["run_id"], location="valmez")
    assert first_points[0]["value"] == 1.0
    assert second_points[0]["value"] == 2.0
    assert store.latest_forecast_run("openmeteo", "valmez")["run_id"] == runs[1]["run_id"]


def test_identical_forecast_release_reuses_the_archived_run(store):
    first = Reading(
        "openmeteo",
        "valmez",
        date(2026, 9, 7),
        "precip_mm",
        1.0,
        meta={"provisional": True, "model": "best_match"},
    )
    second = Reading(
        "openmeteo",
        "valmez",
        date(2026, 9, 7),
        "precip_mm",
        1.0,
        meta={"provisional": True, "model": "best_match"},
    )

    store.upsert_readings(
        [first], retrieved_at=datetime(2026, 9, 7, 6, tzinfo=timezone.utc)
    )
    store.upsert_readings(
        [second], retrieved_at=datetime(2026, 9, 7, 17, tzinfo=timezone.utc)
    )

    runs = store.conn.execute(
        "SELECT * FROM forecast_runs WHERE source='openmeteo'"
    ).fetchall()
    assert len(runs) == 1
    assert runs[0]["content_hash"]
    assert first.meta["run_id"] == second.meta["run_id"] == runs[0]["run_id"]
    assert second.meta["retrieved_at"] == "2026-09-07T06:00:00+00:00"


def test_reupserting_the_same_mutated_reading_is_idempotent(store):
    point = Reading(
        "api30_forecast",
        "valmez",
        date(2026, 9, 8),
        "api30_mm",
        28.0,
        meta={"issued": "2026-09-07", "calculation_version": "test"},
    )

    store.upsert_readings([point], input_run_ids=["input-1"])
    first_run_id = point.meta["run_id"]
    store.upsert_readings([point], input_run_ids=["input-1"])

    assert point.meta["run_id"] == first_run_id
    assert store.conn.execute(
        "SELECT COUNT(*) FROM forecast_runs WHERE source='api30_forecast'"
    ).fetchone()[0] == 1


def test_run_level_calculation_version_does_not_change_content_identity(store):
    point = Reading(
        "api30_forecast",
        "valmez",
        date(2026, 9, 8),
        "api30_mm",
        28.0,
        meta={"issued": "2026-09-07"},
    )

    store.upsert_readings([point], calculation_version="test")
    store.upsert_readings([point], calculation_version="test")

    assert store.conn.execute(
        "SELECT COUNT(*) FROM forecast_runs WHERE source='api30_forecast'"
    ).fetchone()[0] == 1


def test_migration_backfills_legacy_forecasts(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE forecasts (
            id INTEGER PRIMARY KEY,
            issued TEXT NOT NULL,
            source TEXT NOT NULL,
            location TEXT NOT NULL,
            target_date TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            UNIQUE (issued, source, location, target_date, metric)
        );
        INSERT INTO forecasts
            (issued, source, location, target_date, metric, value)
        VALUES ('2026-09-07', 'openmeteo', 'valmez', '2026-09-08', 'precip_mm', 4.2);
        """
    )
    conn.close()

    with Store(path) as migrated:
        assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {
            row["name"]
            for row in migrated.conn.execute("PRAGMA table_info(forecast_runs)")
        }
        assert "content_hash" in columns
        run = migrated.latest_forecast_run("openmeteo", "valmez", "precip_mm")
        assert run is not None
        assert run["run_id"] == "legacy:openmeteo:2026-09-07"
        points = migrated.forecast_run_points(run["run_id"], location="valmez")
        assert len(points) == 1 and points[0]["value"] == 4.2


def test_v3_forecast_runs_accept_the_v2_insert_shape(store):
    store.conn.execute(
        """INSERT INTO forecast_runs
           (run_id, source, retrieved_at, status, input_run_ids_json, created_at)
           VALUES ('old-writer', 'openmeteo', '2026-09-07T00:00:00+00:00',
                   'ok', '[]', '2026-09-07T00:00:00+00:00')"""
    )

    row = store.get_forecast_run("old-writer")
    assert row is not None and row["content_hash"] is None


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
    assert store.last_emission("valmez")["text_json"] == "new"


def test_v2_migration_copies_legacy_notifications(tmp_path):
    path = tmp_path / "v1.sqlite"
    with Store(path) as current:
        current.add_notification(
            date(2026, 9, 7),
            "valmez",
            "chmi_map",
            '{"key":"level:4","text":"signal","data":{}}',
        )
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE signal_emissions")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()

    with Store(path) as migrated:
        row = migrated.last_emission("valmez", "chmi_map")
        assert row["emission_key"] == "level:4"
        assert migrated.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_a_report_round_trips_a_location_without_a_number(store):
    """``chances_json`` must keep ``null`` apart from "not in the report".

    The send rule reads both: a slug that is absent is a new location, one
    stored as ``null`` was there and had no usable API30.
    """
    store.save_report(
        date(2026, 9, 12),
        "daily",
        chances={"valmez": None, "bystrice": 40},
        verdicts={"valmez": "insufficient"},
        candidates={},
        events={},
        error_class="none",
        rules_version="6",
        sent=True,
        reason="тест",
    )

    row = store.last_report("daily")
    assert json.loads(row["chances_json"]) == {"valmez": None, "bystrice": 40}
    assert "katerinice" not in json.loads(row["chances_json"])


def test_upsert_forecasts_ignores_observations(store):
    assert store.upsert_forecasts([r(6, 3)]) == 0
    assert store.upsert_readings([]) == 0
