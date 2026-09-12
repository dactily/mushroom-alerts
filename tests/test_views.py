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


def test_biological_features_include_conservative_guidance(tmp_path):
    readings = []
    for offset in range(-30, 1):
        day = TODAY + timedelta(days=offset)
        readings += [
            Reading("chmi_station", VALMEZ.slug, day, "sra_mm", 0.0),
            Reading("chmi_station", VALMEZ.slug, day, "t_mean", 14.0),
        ]
    readings += [
        Reading("chmi_station", VALMEZ.slug, TODAY - timedelta(days=2), "sra_mm", 22.0),
        Reading("chmi_station", VALMEZ.slug, TODAY - timedelta(days=1), "t_min", -1.0),
        Reading("chmi_station", VALMEZ.slug, TODAY - timedelta(days=3), "api30_mm", 18.0),
        Reading("chmi_station", VALMEZ.slug, TODAY, "api30_mm", 21.0),
    ]
    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings(readings)
        bio = location_snapshot(store, VALMEZ, TODAY)["biological"]

    assert bio["temperature_7d"]["mean_c"] == 14.0
    assert bio["temperature_7d"]["covered_days"] == 7
    assert bio["frost"]["present"] is True
    assert bio["rain_episode"]["date"] == TODAY - timedelta(days=2)
    assert bio["rain_episode"]["growth_window"] == [
        TODAY + timedelta(days=5),
        TODAY + timedelta(days=10),
    ]
    assert bio["api30_dynamics"]["delta_3d_mm"] == 3.0
    assert bio["history"]["sufficient"] is True
    assert bio["guidance"]["verdict"] == "medium"
    assert bio["guidance"]["phase"] == "waiting"


def test_rain_does_not_become_high_before_growth_window(tmp_path):
    readings = []
    rain = {-3: 9.2, -2: 2.0, -1: 14.6}
    for offset in range(-30, 1):
        day = TODAY + timedelta(days=offset)
        readings += [
            Reading("chmi_station", VALMEZ.slug, day, "sra_mm", rain.get(offset, 0.0)),
            Reading("chmi_station", VALMEZ.slug, day, "t_mean", 15.0),
        ]
        if offset >= -6:
            readings.append(
                Reading("chmi_station", VALMEZ.slug, day, "t_min", 8.0)
            )
    maps = [
        Reading("chmi_map", VALMEZ.slug, TODAY, "level", 3.0),
        Reading("houbymapa", VALMEZ.slug, TODAY, "level", 4.0),
        Reading("houbymapa", VALMEZ.slug, TODAY, "score", 0.70),
    ]
    api_curve = [
        Reading(
            "api30_forecast",
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            "api30_mm",
            35.0,
            meta={
                "quality": "fresh",
                **({"issued": TODAY.isoformat()} if offset > 0 else {}),
            },
        )
        for offset in range(13)
    ]
    temperatures = []
    for offset in range(13):
        meta = {"issued": TODAY.isoformat()}
        temperatures += [
            Reading(
                "openmeteo",
                VALMEZ.slug,
                TODAY + timedelta(days=offset),
                "t_mean",
                14.0,
                meta=meta,
            ),
            Reading(
                "openmeteo",
                VALMEZ.slug,
                TODAY + timedelta(days=offset),
                "t_min",
                7.0,
                meta=meta,
            ),
        ]

    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings(readings + maps, retrieved_at=stamp(5))
        store.upsert_readings(temperatures, retrieved_at=stamp(6))
        store.upsert_readings(api_curve, retrieved_at=stamp(7))
        bio = location_snapshot(store, VALMEZ, TODAY)["biological"]

    assert bio["rain_episode"]["total_mm"] == 25.8
    assert bio["rain_episode"]["date"] == TODAY - timedelta(days=1)
    assert bio["rain_episode"]["growth_window"] == [
        TODAY + timedelta(days=6),
        TODAY + timedelta(days=11),
    ]
    guidance = bio["guidance"]
    assert guidance["verdict"] == "medium"
    assert guidance["phase"] == "waiting"
    assert guidance["candidate_high_date"] == TODAY + timedelta(days=6)
    assert "growth_window_not_started" in guidance["high_blockers"]
    assert guidance["outlook"][0]["verdict"] == "medium"
    assert next(
        row for row in guidance["outlook"] if row["date"] == TODAY + timedelta(days=6)
    )["verdict"] == "high"


def test_high_requires_an_active_growth_window(tmp_path):
    readings = []
    for offset in range(-30, 1):
        day = TODAY + timedelta(days=offset)
        readings += [
            Reading(
                "chmi_station",
                VALMEZ.slug,
                day,
                "sra_mm",
                22.0 if offset == -7 else 0.0,
            ),
            Reading("chmi_station", VALMEZ.slug, day, "t_mean", 15.0),
        ]
        if offset >= -6:
            readings.append(
                Reading("chmi_station", VALMEZ.slug, day, "t_min", 8.0)
            )
    maps = [Reading("houbymapa", VALMEZ.slug, TODAY, "level", 4.0)]
    api_curve = [
        Reading(
            "api30_forecast",
            VALMEZ.slug,
            TODAY,
            "api30_mm",
            30.0,
            meta={"quality": "fresh"},
        )
    ]
    temperatures = [
        Reading(
            "openmeteo",
            VALMEZ.slug,
            TODAY,
            metric,
            value,
            meta={"issued": TODAY.isoformat()},
        )
        for metric, value in (("t_mean", 14.0), ("t_min", 7.0))
    ]

    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings(readings + maps, retrieved_at=stamp(5))
        store.upsert_readings(temperatures, retrieved_at=stamp(6))
        store.upsert_readings(api_curve, retrieved_at=stamp(7))
        guidance = location_snapshot(store, VALMEZ, TODAY)["biological"]["guidance"]

    assert guidance["phase"] == "growth_window"
    assert guidance["verdict"] == "high"
    assert guidance["high_blockers"] == []


def test_rain_episode_requires_complete_temperature_window(tmp_path):
    readings = [
        Reading(
            "chmi_station",
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            "sra_mm",
            10.0,
        )
        for offset in (-2, -1, 0)
    ]
    readings += [
        Reading(
            "chmi_station",
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            "t_mean",
            15.0,
        )
        for offset in (-2, -1)
    ]

    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings(readings, retrieved_at=stamp(5))
        bio = location_snapshot(store, VALMEZ, TODAY)["biological"]

    assert bio["rain_episode"] is None
    assert bio["guidance"]["verdict"] == "insufficient"
    assert "no_qualified_rain_episode" in bio["guidance"]["high_blockers"]
