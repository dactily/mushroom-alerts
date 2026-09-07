from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from mushroom_alerts import __main__ as cli
from mushroom_alerts import calibration
from mushroom_alerts.base import Reading
from mushroom_alerts.store import Store

from .conftest import VALMEZ


ISSUED = date(2026, 9, 7)
RETRIEVED = datetime(2026, 9, 7, 6, tzinfo=UTC)


def prediction(source: str, metric: str, horizon: int, value: float) -> Reading:
    return Reading(
        source,
        VALMEZ.slug,
        ISSUED + timedelta(days=horizon),
        metric,
        value,
        meta={"issued": ISSUED.isoformat(), "horizon_days": horizon},
    )


def actual(metric: str, horizon: int, value: float) -> Reading:
    return Reading(
        "chmi_station", VALMEZ.slug, ISSUED + timedelta(days=horizon), metric, value
    )


def seed(store: Store) -> None:
    store.upsert_readings(
        [
            prediction("openmeteo", "precip_mm", 1, 10.0),
            prediction("openmeteo", "precip_mm", 5, 3.0),
            prediction("openmeteo", "precip_mm", 10, 4.0),
        ],
        retrieved_at=RETRIEVED,
    )
    store.upsert_readings(
        [
            prediction("api30_forecast", "api30_mm", 2, 25.0),
            prediction("api30_forecast", "api30_mm", 6, 30.0),
            prediction("api30_forecast", "api30_mm", 12, 20.0),
        ],
        retrieved_at=RETRIEVED,
    )
    store.upsert_readings(
        [
            actual("sra_mm", 1, 6.0),
            actual("sra_mm", 5, 5.0),
            actual("sra_mm", 10, 4.0),
            actual("api30_mm", 2, 20.0),
            actual("api30_mm", 6, 32.0),
            actual("api30_mm", 12, 22.0),
        ]
    )


def test_calibration_reports_n_bias_and_mae_by_horizon(tmp_path):
    with Store(tmp_path / "state.sqlite") as store:
        seed(store)
        report = calibration.build(store, [VALMEZ])

    metrics = report["locations"][VALMEZ.slug]
    assert metrics["precipitation"] == {
        "1-3": {"n": 1, "bias": 4.0, "mae": 4.0},
        "4-7": {"n": 1, "bias": -2.0, "mae": 2.0},
        "8-16": {"n": 1, "bias": 0.0, "mae": 0.0},
    }
    assert metrics["api30"]["1-3"] == {"n": 1, "bias": 5.0, "mae": 5.0}
    assert metrics["api30"]["4-7"] == {"n": 1, "bias": -2.0, "mae": 2.0}
    assert report["automatic_adjustment"] is False


def test_calibration_cli_json_is_read_only(tmp_path, monkeypatch, capsys):
    db = tmp_path / "state.sqlite"
    with Store(db) as store:
        seed(store)
        before = store.conn.total_changes
    monkeypatch.setenv("MUSHROOM_DB", str(db))

    assert cli.main(["calibration", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["locations"][VALMEZ.slug]["precipitation"]["1-3"]["n"] == 1
    with Store(db) as store:
        assert store.conn.total_changes == 0
