from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from mushroom_alerts.base import FetchResult, Reading
from mushroom_alerts.store import Store
from mushroom_alerts.views import forecast_bundle, location_snapshot

from .conftest import BYSTRICE, VALMEZ


TODAY = date(2026, 9, 7)


def forecast(source: str, metric: str, values: list[tuple[int, float]]) -> list[Reading]:
    return [
        Reading(
            source,
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            metric,
            value,
            meta={"issued": TODAY.isoformat()} if offset else None,
        )
        for offset, value in values
    ]


def stamp(hour: int) -> datetime:
    return datetime(2026, 9, 7, hour, tzinfo=UTC)


def test_forecast_metrics_never_mix_runs(tmp_path):
    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings(
            forecast("openmeteo", "precip_mm", [(0, 1.0), (1, 2.0)])
            + forecast("openmeteo", "t_mean", [(0, 14.0), (1, 15.0)]),
            retrieved_at=stamp(6),
        )
        store.upsert_readings(
            forecast("openmeteo", "precip_mm", [(0, 7.0), (1, 8.0)]),
            retrieved_at=stamp(17),
        )

        bundle = forecast_bundle(store, VALMEZ.slug, TODAY)

    assert bundle["rain"][TODAY] == 7.0
    assert bundle["t_mean"] == {}


def test_api30_uses_the_openmeteo_run_it_references(tmp_path):
    with Store(tmp_path / "state.sqlite") as store:
        first = forecast("openmeteo", "precip_mm", [(0, 1.0), (1, 2.0)])
        store.upsert_readings(first, retrieved_at=stamp(6))
        first_run = first[0].meta["run_id"]
        store.upsert_readings(
            forecast("api30_forecast", "api30_mm", [(0, 20.0), (1, 22.0)]),
            retrieved_at=stamp(7),
            input_run_ids=[first_run],
        )
        store.upsert_readings(
            forecast("openmeteo", "precip_mm", [(0, 9.0), (1, 10.0)]),
            retrieved_at=stamp(17),
        )

        bundle = forecast_bundle(store, VALMEZ.slug, TODAY)

    assert bundle["openmeteo_run_id"] == first_run
    assert bundle["rain"][TODAY] == 1.0


def test_old_forecast_run_is_stale(tmp_path):
    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings(
            forecast("openmeteo", "precip_mm", [(0, 1.0)]),
            retrieved_at=stamp(6) - timedelta(days=1),
        )
        bundle = forecast_bundle(store, VALMEZ.slug, TODAY)
    assert bundle["openmeteo_quality"] == "stale"


def test_partial_location_failure_is_visible_in_shared_view(tmp_path):
    row = Reading("openmeteo", VALMEZ.slug, TODAY, "precip_mm", 2.0)
    result = FetchResult.success(
        "openmeteo",
        [row],
        "valasska-bystrice: timeout",
        {BYSTRICE.slug: "timeout"},
    )
    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings([row], retrieved_at=stamp(6))
        valmez = location_snapshot(store, VALMEZ, TODAY, results=[result])
        bystrice = location_snapshot(store, BYSTRICE, TODAY, results=[result])

    assert valmez["source_status"]["openmeteo"]["current_run_ok"] is True
    assert bystrice["source_status"]["openmeteo"] == {
        "quality": "missing",
        "run_id": None,
        "retrieved_at": None,
        "error": "timeout",
    }
