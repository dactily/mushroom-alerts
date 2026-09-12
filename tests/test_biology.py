from __future__ import annotations

from datetime import date, timedelta

import pytest

from mushroom_alerts import biology, policy
from mushroom_alerts.base import DataQuality, SeriesPoint


TODAY = date(2026, 9, 12)


def _series(
    start: date,
    end: date,
    values: dict[date, float],
) -> dict[date, float]:
    return {
        start + timedelta(days=offset): values.get(
            start + timedelta(days=offset), 0.0
        )
        for offset in range((end - start).days + 1)
    }


def _episodes(*rain_days: tuple[int, float]) -> tuple[biology.RainEpisode, ...]:
    start = TODAY - timedelta(days=35)
    rain = _series(
        start,
        TODAY,
        {TODAY + timedelta(days=offset): amount for offset, amount in rain_days},
    )
    temperature = _series(start, TODAY, {})
    temperature = {day: 15.0 for day in temperature}
    return biology.detect_rain_episodes("forest", rain, temperature, TODAY)


def _episode(anchor_offset: int = -7) -> biology.RainEpisode:
    anchor = TODAY + timedelta(days=anchor_offset)
    return biology.RainEpisode(
        event_id=f"rain-{anchor.isoformat()}",
        location="forest",
        start=anchor - timedelta(days=2),
        end=anchor + timedelta(days=2),
        anchor=anchor,
        total_mm=24.0,
        covered_days=5,
        expected_days=5,
        quality=DataQuality.FRESH,
        lower_bound=False,
        temperature_mean_c=15.0,
        temperature_covered_days=5,
    )


def _assess(day: date, episodes: tuple[biology.RainEpisode, ...]):
    return biology.assess_day(
        day,
        episodes,
        api30_mm=30.0,
        api30_quality=DataQuality.FRESH,
        t_mean=15.0,
        t_min=8.0,
        forecast_fresh=True,
        history_sufficient=True,
        frost_known=True,
        frost_present=False,
        map_high=True,
        usable_input=True,
        threshold_mm=policy.API30_THRESHOLD_MM,
    )


def test_overlapping_rolling_windows_form_one_episode():
    episodes = _episodes((-8, 8.0), (-7, 15.0), (-6, 4.0))

    assert len(episodes) == 1
    assert episodes[0].anchor == TODAY - timedelta(days=7)
    assert episodes[0].total_mm == 27.0


def test_separated_rains_are_kept_as_distinct_episodes():
    episodes = _episodes((-18, 24.0), (-7, 22.0))

    assert len(episodes) == 2
    assert [event.anchor for event in episodes] == [
        TODAY - timedelta(days=18),
        TODAY - timedelta(days=7),
    ]
    assert episodes[0].event_id != episodes[1].event_id


def test_a_newer_rain_does_not_swallow_the_older_open_window():
    """The defect: five dry days between two rains merged them into one.

    Rolling windows reach two days back, so the 3-day windows of a rain on
    D-7 and of a rain on D-2 still touched and were grouped; the merged
    episode anchored on the *newer* rain and today's open window vanished.
    With 25 mm seven days ago the chance was 65 %, and adding 30 mm two days
    ago dropped it to 10 %.
    """
    episodes = _episodes((-7, 25.0), (-2, 30.0))

    assert len(episodes) == 2
    assert [event.anchor for event in episodes] == [
        TODAY - timedelta(days=7),
        TODAY - timedelta(days=2),
    ]
    older, newer = episodes
    assert older.primary_start <= TODAY <= older.primary_end
    assert (older.start, older.end) == (older.anchor, older.anchor)
    assert (newer.start, newer.end) == (newer.anchor, newer.anchor)


def test_one_dry_day_does_not_split_a_wet_spell():
    """A shower that pauses for a day is still one spell; a tie goes later."""
    episodes = _episodes((-8, 12.0), (-6, 12.0))

    assert len(episodes) == 1
    assert (episodes[0].start, episodes[0].end) == (
        TODAY - timedelta(days=8),
        TODAY - timedelta(days=6),
    )
    assert episodes[0].anchor == TODAY - timedelta(days=6)
    assert episodes[0].total_mm == 24.0


def test_two_dry_days_end_a_wet_spell():
    episodes = _episodes((-9, 22.0), (-6, 22.0))

    assert [event.anchor for event in episodes] == [
        TODAY - timedelta(days=9),
        TODAY - timedelta(days=6),
    ]


@pytest.mark.parametrize(
    ("drizzle", "expected"),
    [(policy.RAIN_WET_DAY_MM - 0.1, 2), (policy.RAIN_WET_DAY_MM, 1)],
)
def test_the_wet_day_threshold_decides_whether_a_gap_is_dry(drizzle, expected):
    episodes = _episodes(
        (-9, 22.0), (-8, drizzle), (-7, drizzle), (-6, 22.0)
    )

    assert len(episodes) == expected


def test_unknown_days_neither_split_a_spell_nor_extend_it():
    """A hole in the series is not evidence that the ground stayed dry."""
    start = TODAY - timedelta(days=35)
    rain: dict[date, float | SeriesPoint] = _series(
        start,
        TODAY,
        {TODAY - timedelta(days=9): 22.0, TODAY - timedelta(days=6): 4.0},
    )
    for offset in (8, 7, 5):  # two inside the spell, one just after it
        gap = TODAY - timedelta(days=offset)
        rain[gap] = SeriesPoint(gap, None, "chmi_station", DataQuality.MISSING)
    temperature = {day: 15.0 for day in _series(start, TODAY, {})}

    episodes = biology.detect_rain_episodes("forest", rain, temperature, TODAY)

    assert len(episodes) == 1  # two dry days would have split it
    assert (episodes[0].start, episodes[0].end) == (
        TODAY - timedelta(days=9),
        TODAY - timedelta(days=6),
    )
    # ...and the hole is reported rather than hidden
    assert (episodes[0].covered_days, episodes[0].expected_days) == (2, 4)
    assert episodes[0].lower_bound is True


def test_the_reported_span_describes_the_wet_days_not_the_window():
    (episode,) = _episodes((-8, 8.0), (-7, 15.0), (-6, 4.0))

    assert (episode.start, episode.end) == (
        TODAY - timedelta(days=8),
        TODAY - timedelta(days=6),
    )
    assert episode.total_mm == 27.0
    assert (episode.covered_days, episode.expected_days) == (3, 3)
    assert episode.temperature_mean_c == 15.0
    assert episode.temperature_covered_days == 3
    assert episode.lower_bound is False


def test_the_event_id_follows_the_first_wet_day():
    """Ids shift for spells whose start moved -- worth one extra send."""
    (plain,) = _episodes((-7, 25.0))
    (after_drizzle,) = _episodes((-8, 0.5), (-7, 25.0))
    (after_rain,) = _episodes((-8, 2.0), (-7, 25.0))

    assert plain.start == after_drizzle.start  # 0.5 mm is a dry day
    assert plain.event_id == after_drizzle.event_id
    assert after_rain.start == TODAY - timedelta(days=8)
    assert after_rain.event_id != plain.event_id


@pytest.mark.parametrize(
    ("day_offset", "phase", "verdict"),
    [
        (6, "waiting", "medium"),
        (7, "primary_window", "high"),
        (12, "primary_window", "high"),
        (13, "residual_window", "medium"),
        (21, "residual_window", "medium"),
        (22, "expired", "medium"),
    ],
)
def test_episode_phase_boundaries(day_offset: int, phase: str, verdict: str):
    event = _episode(anchor_offset=0)

    assessment = _assess(TODAY + timedelta(days=day_offset), (event,))

    assert assessment.phase == phase
    assert assessment.verdict == verdict


def test_expired_event_without_map_support_is_low():
    event = _episode(anchor_offset=-22)

    assessment = biology.assess_day(
        TODAY,
        (event,),
        api30_mm=30.0,
        api30_quality=DataQuality.FRESH,
        t_mean=15.0,
        t_min=8.0,
        forecast_fresh=True,
        history_sufficient=True,
        frost_known=True,
        frost_present=False,
        map_high=False,
        usable_input=True,
        threshold_mm=policy.API30_THRESHOLD_MM,
    )

    assert assessment.phase == "expired"
    assert assessment.verdict == "low"


def test_older_primary_window_beats_newer_waiting_event():
    active = _episode(-7)
    new_rain = _episode(-1)

    assessment = _assess(TODAY, (active, new_rain))

    assert assessment.phase == "primary_window"
    assert assessment.dominant_event_id == active.event_id
    assert assessment.upcoming_event_ids == (new_rain.event_id,)
    assert assessment.verdict == "high"


def test_older_residual_window_beats_newer_waiting_event():
    residual = _episode(-14)
    new_rain = _episode(-1)

    assessment = _assess(TODAY, (residual, new_rain))

    assert assessment.phase == "residual_window"
    assert assessment.dominant_event_id == residual.event_id
    assert assessment.verdict == "medium"


def test_high_requires_every_conservative_gate():
    event = _episode(-7)

    assessment = biology.assess_day(
        TODAY,
        (event,),
        api30_mm=30.0,
        api30_quality=DataQuality.FRESH,
        t_mean=15.0,
        t_min=8.0,
        forecast_fresh=True,
        history_sufficient=True,
        frost_known=True,
        frost_present=False,
        map_high=False,
        usable_input=True,
        threshold_mm=policy.API30_THRESHOLD_MM,
    )

    assert assessment.verdict == "medium"
    assert assessment.high_blockers == ("no_fresh_high_map_support",)


def test_event_id_is_deterministic_for_the_same_history():
    first = _episodes((-7, 22.0))
    second = _episodes((-7, 22.0))

    assert first[0].event_id == second[0].event_id


def test_stale_rain_does_not_form_an_episode():
    days = [TODAY - timedelta(days=offset) for offset in (2, 1, 0)]
    rain = {
        day: SeriesPoint(day, 10.0, "station", DataQuality.STALE)
        for day in days
    }
    temperature = {day: 15.0 for day in days}

    assert biology.detect_rain_episodes(
        "forest", rain, temperature, TODAY
    ) == ()


def test_incomplete_temperature_window_does_not_form_an_episode():
    rain = {
        TODAY - timedelta(days=2): 10.0,
        TODAY - timedelta(days=1): 10.0,
        TODAY: 10.0,
    }
    temperature = {
        TODAY - timedelta(days=2): 15.0,
        TODAY - timedelta(days=1): 15.0,
    }

    assert biology.detect_rain_episodes(
        "forest", rain, temperature, TODAY
    ) == ()


def test_todays_map_level_does_not_gate_future_days():
    """The maps publish no forecast, so today's level judges today only."""
    event = _episode(anchor_offset=0)
    future = TODAY + timedelta(days=8)

    assessment = biology.assess_horizon(
        TODAY,
        (event,),
        [TODAY, future],
        api30={TODAY: 30.0, future: 30.0},
        api30_quality={TODAY: DataQuality.FRESH, future: DataQuality.FRESH},
        t_mean={TODAY: 15.0, future: 15.0},
        t_min={TODAY: 8.0, future: 8.0},
        forecast_fresh=True,
        history_sufficient=True,
        frost_known=True,
        frost_present=False,
        map_high=False,
        usable_input=True,
        threshold_mm=policy.API30_THRESHOLD_MM,
    )

    today_day = assessment.outlook[0]
    future_day = assessment.outlook[-1]
    assert "no_fresh_high_map_support" in today_day.high_blockers
    assert today_day.verdict == "medium"
    assert future_day.high_blockers == ()
    assert future_day.verdict == "high"
    assert assessment.candidate_high_date == future


def test_todays_map_does_not_raise_future_days_to_medium():
    future = TODAY + timedelta(days=5)

    assessment = biology.assess_horizon(
        TODAY,
        (),
        [TODAY, future],
        api30={TODAY: 30.0, future: 30.0},
        api30_quality={TODAY: DataQuality.FRESH, future: DataQuality.FRESH},
        t_mean={TODAY: 15.0, future: 15.0},
        t_min={TODAY: 8.0, future: 8.0},
        forecast_fresh=True,
        history_sufficient=True,
        frost_known=True,
        frost_present=False,
        map_high=True,
        usable_input=True,
        threshold_mm=policy.API30_THRESHOLD_MM,
    )

    assert assessment.outlook[0].verdict == "medium"
    assert assessment.outlook[-1].verdict == "low"


def test_empty_series_point_inside_an_episode_is_skipped():
    """A point without a value is skipped, exactly as in calendar_window."""
    start = TODAY - timedelta(days=35)
    rain: dict[date, float | SeriesPoint] = _series(
        start, TODAY, {TODAY - timedelta(days=7): 24.0}
    )
    gap = TODAY - timedelta(days=6)
    rain[gap] = SeriesPoint(
        date=gap, value=None, source="chmi_station", quality=DataQuality.MISSING
    )
    temperature = {day: 15.0 for day in _series(start, TODAY, {})}

    episodes = biology.detect_rain_episodes("forest", rain, temperature, TODAY)

    assert len(episodes) == 1
    assert episodes[0].anchor == TODAY - timedelta(days=7)
