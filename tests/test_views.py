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
    assert bio["guidance"]["scope"] == (
        "site_conditions_not_observed_fruit_bodies"
    )


def test_rain_does_not_become_high_before_growth_window(tmp_path):
    readings = []
    rain = {-3: 9.2, -2: 2.0, -1: 14.6}
    for offset in range(-30, 1):
        day = TODAY + timedelta(days=offset)
        readings += [
            Reading("chmi_station", VALMEZ.slug, day, "sra_mm", rain.get(offset, 0.0)),
            Reading("chmi_station", VALMEZ.slug, day, "t_mean", 15.0),
        ]
        if offset >= -7:
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
        if offset >= -7:
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

    assert guidance["phase"] == "primary_window"
    assert guidance["verdict"] == "high"
    assert guidance["high_blockers"] == []


def test_frost_history_uses_seven_completed_station_days(tmp_path):
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
        if -7 <= offset <= -1:
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
        bio = location_snapshot(store, VALMEZ, TODAY)["biological"]

    assert bio["frost"]["start"] == TODAY - timedelta(days=7)
    assert bio["frost"]["end"] == TODAY - timedelta(days=1)
    assert bio["frost"]["covered_days"] == 7
    assert bio["guidance"]["verdict"] == "high"


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


# ----------------------------------------------------------------------
# the comparable chance (PLAN §9b)
# ----------------------------------------------------------------------
def _primary_window_readings(*, station_until: int = 0) -> list[Reading]:
    """A wet spell eight days ago: today sits inside D+7...D+12."""
    readings = []
    for offset in range(-30, station_until + 1):
        day = TODAY + timedelta(days=offset)
        readings += [
            Reading(
                "chmi_station",
                VALMEZ.slug,
                day,
                "sra_mm",
                22.0 if offset == -8 else 0.0,
            ),
            Reading("chmi_station", VALMEZ.slug, day, "t_mean", 15.0),
        ]
        if offset >= -7:
            readings.append(Reading("chmi_station", VALMEZ.slug, day, "t_min", 8.0))
    return readings


def test_the_snapshot_carries_a_chance_next_to_the_verdict(tmp_path):
    maps = [
        Reading("chmi_map", VALMEZ.slug, TODAY, "level", 3.0),
        Reading("houbymapa", VALMEZ.slug, TODAY, "level", 4.0),
        Reading("houbymapa", VALMEZ.slug, TODAY, "score", 0.90),
    ]
    api_curve = [
        Reading(
            "api30_forecast",
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            "api30_mm",
            30.0,
            meta={
                "quality": "fresh",
                **({"issued": TODAY.isoformat()} if offset else {}),
            },
        )
        for offset in (0, 1)
    ]
    temperatures = [
        Reading(
            "openmeteo",
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            metric,
            value,
            meta={"issued": TODAY.isoformat()} if offset else None,
        )
        for offset in (0, 1)
        for metric, value in (("t_mean", 14.0), ("t_min", 7.0))
    ]

    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings(_primary_window_readings() + maps, retrieved_at=stamp(5))
        store.upsert_readings(temperatures, retrieved_at=stamp(6))
        store.upsert_readings(api_curve, retrieved_at=stamp(7))
        bio = location_snapshot(store, VALMEZ, TODAY)["biological"]

    chance = bio["chance"]
    # eight days after the rain the phase ramp is on its plateau:
    # 0.60 x 1.15 (API30 30 mm) x 1.12 (HoubyMapa 0.90) x 0.90 (ČHMÚ 3) = 69.6 %
    assert chance["today"] == 70
    # the maps correct the place, so tomorrow carries them too, and one day
    # out the lead-time damping is still 1.0: the same conditions, the same
    # number
    assert chance["curve"][TODAY + timedelta(days=1)] == 70
    assert chance["peak"] == (TODAY, 70)
    assert chance["capped"] is False
    assert bio["guidance"]["verdict"] == "high"  # the verdict is untouched


def test_a_poor_map_lowers_the_forecast_days_too(tmp_path):
    """The maps are a property of the place (the verdict still drops them).

    While they counted for today only, a location with ČHMÚ 1/5 scored 55 %
    today and 70 % tomorrow on identical conditions, and the difference was
    arithmetic, not weather.
    """
    api_curve = [
        Reading(
            "api30_forecast",
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            "api30_mm",
            30.0,
            meta={
                "quality": "fresh",
                **({"issued": TODAY.isoformat()} if offset else {}),
            },
        )
        for offset in (0, 1)
    ]
    temperatures = [
        Reading(
            "openmeteo",
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            metric,
            value,
            meta={"issued": TODAY.isoformat()} if offset else None,
        )
        for offset in (0, 1)
        for metric, value in (("t_mean", 14.0), ("t_min", 7.0))
    ]
    poor_map = [Reading("chmi_map", VALMEZ.slug, TODAY, "level", 1.0)]

    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings(
            _primary_window_readings() + poor_map, retrieved_at=stamp(5)
        )
        store.upsert_readings(temperatures, retrieved_at=stamp(6))
        store.upsert_readings(api_curve, retrieved_at=stamp(7))
        bio = location_snapshot(store, VALMEZ, TODAY)["biological"]

    # 0.60 x 1.15 x 0.80 (ČHMÚ 1) = 55.2 %, today and tomorrow alike
    assert bio["chance"]["today"] == 55
    assert bio["chance"]["curve"][TODAY + timedelta(days=1)] == 55
    # the verdict keeps its today-only map gate: no fresh high map blocks
    # today, while tomorrow is judged without any map at all
    assert bio["guidance"]["verdict"] != "high"
    assert "no_fresh_high_map_support" in bio["guidance"]["high_blockers"]
    assert bio["guidance"]["outlook"][1]["verdict"] == "high"


def _rain_history(
    rain: dict[int, float], *, api30_mm: float | None = None
) -> list[Reading]:
    """Station history at a steady 15 °C, with rain only on the named days."""
    rows: list[Reading] = []
    for offset in range(-35, 1):
        day = TODAY + timedelta(days=offset)
        rows += [
            Reading(
                "chmi_station", VALMEZ.slug, day, "sra_mm", rain.get(offset, 0.0)
            ),
            Reading("chmi_station", VALMEZ.slug, day, "t_mean", 15.0),
        ]
        if offset >= -7:
            rows.append(Reading("chmi_station", VALMEZ.slug, day, "t_min", 8.0))
    if api30_mm is not None:
        rows.append(
            Reading("chmi_station", VALMEZ.slug, TODAY, "api30_mm", api30_mm)
        )
    return rows


def _flat_curve(
    mm: float, days: int = 12, *, issued: date | None = None
) -> list[Reading]:
    issued = TODAY if issued is None else issued
    return [
        Reading(
            "api30_forecast",
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            "api30_mm",
            mm,
            meta={
                "quality": "fresh",
                **({"issued": issued.isoformat()} if offset else {}),
            },
        )
        for offset in range(days)
    ]


def _flat_weather(days: int = 12, *, issued: date | None = None) -> list[Reading]:
    issued = TODAY if issued is None else issued
    return [
        Reading(
            "openmeteo",
            VALMEZ.slug,
            TODAY + timedelta(days=offset),
            metric,
            value,
            meta={"issued": issued.isoformat()} if offset else None,
        )
        for offset in range(days)
        for metric, value in (("t_mean", 15.0), ("t_min", 8.0))
    ]


def _chance_of(tmp_path, rain: dict[int, float], name: str) -> dict:
    with Store(tmp_path / f"{name}.sqlite") as store:
        store.upsert_readings(_rain_history(rain), retrieved_at=stamp(5))
        store.upsert_readings(_flat_weather(), retrieved_at=stamp(6))
        store.upsert_readings(_flat_curve(28.0), retrieved_at=stamp(7))
        return location_snapshot(store, VALMEZ, TODAY)["biological"]


def test_a_second_rain_does_not_hide_an_open_growth_window(tmp_path):
    """More rain must never mean a lower chance (the merging defect).

    25 mm seven days ago put today inside D+7..D+12 and scored 65 %.  Adding
    30 mm two days ago used to merge both rains into one episode anchored on
    the newer one, which is still waiting: 10 %, for strictly more water.
    """
    one = _chance_of(tmp_path, {-7: 25.0}, "one")
    two = _chance_of(tmp_path, {-7: 25.0, -2: 30.0}, "two")

    assert one["chance"]["today"] == 65
    assert two["chance"]["today"] == 65
    assert len(one["rain_episodes"]) == 1
    assert [item["date"] for item in two["rain_episodes"]] == [
        TODAY - timedelta(days=7),
        TODAY - timedelta(days=2),
    ]
    # the verdict reads the same two episodes: the open window dominates
    assert two["guidance"]["phase"] == "primary_window"
    assert one["guidance"]["dominant_event_id"] == two["guidance"][
        "dominant_event_id"
    ]


# ----------------------------------------------------------------------
# no usable moisture number, no percentage
# ----------------------------------------------------------------------
OPEN_WINDOW = {-8: 25.0}
CHMI_3 = [Reading("chmi_map", VALMEZ.slug, TODAY, "level", 3.0)]


def test_losing_the_api30_takes_the_number_away_instead_of_raising_it(tmp_path):
    """45 % with 20 mm, and 55 % with nothing -- ten points for lost data."""
    with Store(tmp_path / "with.sqlite") as store:
        store.upsert_readings(_rain_history(OPEN_WINDOW) + CHMI_3, retrieved_at=stamp(5))
        store.upsert_readings(_flat_weather(), retrieved_at=stamp(6))
        store.upsert_readings(_flat_curve(20.0), retrieved_at=stamp(7))
        measured = location_snapshot(store, VALMEZ, TODAY)["biological"]["chance"]

    with Store(tmp_path / "without.sqlite") as store:
        store.upsert_readings(_rain_history(OPEN_WINDOW) + CHMI_3, retrieved_at=stamp(5))
        store.upsert_readings(_flat_weather(), retrieved_at=stamp(6))
        blind = location_snapshot(store, VALMEZ, TODAY)["biological"]["chance"]

    assert measured["today"] == 45  # 0.60 x 0.8 (20 mm) x 0.9 (ČHMÚ 3/5)
    assert blind["today"] is None
    assert blind["peak"] is None
    assert blind["capped"] is False


def test_today_falls_back_to_the_station_when_the_run_is_stale(tmp_path):
    """A three-day-old release scored its days ``fresh`` and stayed high.

    ``api30_quality_by_date`` describes a value inside its release; the age
    of the release never reached the chance, so a stale run kept producing
    numbers while the verdict refused ``high`` on ``forecast_not_fresh``.
    The forecast days now have no number, and today uses what the station
    measured this morning instead of going blind with them.
    """
    issued = TODAY - timedelta(days=3)
    with Store(tmp_path / "stale.sqlite") as store:
        store.upsert_readings(
            _rain_history(OPEN_WINDOW, api30_mm=20.0) + CHMI_3, retrieved_at=stamp(5)
        )
        store.upsert_readings(
            _flat_weather(issued=issued), retrieved_at=stamp(6) - timedelta(days=3)
        )
        store.upsert_readings(
            _flat_curve(28.0, issued=issued), retrieved_at=stamp(7) - timedelta(days=3)
        )
        snap = location_snapshot(store, VALMEZ, TODAY)

    bio = snap["biological"]
    assert snap["forecast"]["api30_run_quality"] == "stale"
    # the station's own 20 mm, not the stale curve's 28 mm (which scored 60 %)
    assert bio["chance"]["today"] == 45
    assert set(bio["chance"]["curve"].values()) == {45, None}
    assert bio["chance"]["curve"][TODAY + timedelta(days=1)] is None
    assert bio["chance"]["peak"] == (TODAY, 45)
    assert "forecast_not_fresh" in bio["guidance"]["high_blockers"]


def test_an_empty_database_has_no_number_to_report(tmp_path):
    with Store(tmp_path / "empty.sqlite") as store:
        snap = location_snapshot(store, VALMEZ, TODAY)

    assert snap["biological"]["chance"] == {
        "today": None,
        "curve": {TODAY: None},
        "peak": None,
        "capped": False,
    }


def test_a_stale_station_caps_the_chance(tmp_path):
    api_curve = [
        Reading(
            "api30_forecast", VALMEZ.slug, TODAY, "api30_mm", 30.0,
            meta={"quality": "fresh"},
        )
    ]
    temperatures = [
        Reading("openmeteo", VALMEZ.slug, TODAY, metric, value)
        for metric, value in (("t_mean", 14.0), ("t_min", 7.0))
    ]
    with Store(tmp_path / "state.sqlite") as store:
        store.upsert_readings(
            _primary_window_readings(station_until=-3), retrieved_at=stamp(5)
        )
        store.upsert_readings(temperatures, retrieved_at=stamp(6))
        store.upsert_readings(api_curve, retrieved_at=stamp(7))
        snap = location_snapshot(store, VALMEZ, TODAY)

    assert snap["source_status"]["chmi_station"]["quality"] == "stale"
    assert snap["biological"]["chance"] == {
        "today": 50,
        "curve": {TODAY: 50},
        "peak": (TODAY, 50),
        "capped": True,
    }
