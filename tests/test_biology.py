from __future__ import annotations

from datetime import date, timedelta

import pytest

from mushroom_alerts import biology, policy
from mushroom_alerts.base import DataQuality, SeriesPoint


TODAY = date(2026, 9, 12)


def test_phase_context_preserves_old_and_new_rain_and_soak():
    day = date(2026, 9, 15)
    rain = {date(2026, 8, 28): 22.0, date(2026, 9, 11): 25.0}
    points = _series(date(2026, 8, 1), day, rain)
    episodes = biology.detect_rain_episodes("test", points, dict.fromkeys(points, 15.0), day)
    soak = biology.SoakEpisode("soak", "test", date(2026, 8, 29), date(2026, 9, 5), 8, 32)
    result = biology.phase_context(episodes, [soak], day)
    assert [event["phase"] for event in result["events"]] == [
        "residual_window", "waiting", "primary_window"
    ]
    assert "дождь 28.08: остаточное расчётное окно до 18.09" in result["text"]
    assert "дождь 11.09: новое расчётное окно 18.09–23.09" in result["text"]
    assert "длительное увлажнение 29.08–05.09" in result["text"]
    assert "вероятность держится" not in result["text"]


def test_phase_context_does_not_call_an_expired_window_current():
    episodes = _episodes((-30, 25.0))
    result = biology.phase_context(episodes, [], TODAY)
    assert result["events"][0]["phase"] == "expired"
    assert result["text"].startswith("активных расчётных окон нет")


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


# ----------------------------------------------------------------------
# the second, gentler episode type: a soak
# ----------------------------------------------------------------------
def _soak_run(length: int, *, ending: int = 0, level: float = 25.0):
    """``length`` days at ``level``, the last of them ``ending`` days ago."""
    series = {TODAY - timedelta(days=n): 5.0 for n in range(0, 36)}
    for n in range(ending, ending + length):
        series[TODAY - timedelta(days=n)] = level
    return series


def test_a_slow_soaking_that_never_formed_a_pulse_is_still_an_episode():
    """The Liberec case, 2026-09-13: API30 26–32 mm out of pure drizzle.

    The best three-day window there held 7 mm, so ``detect_rain_episodes``
    found nothing at all and the chance sat at its floor while both maps
    called the place excellent.
    """
    drizzle = {TODAY - timedelta(days=n): 0.8 for n in range(0, 36)}
    temperature = {day: 15.0 for day in drizzle}
    assert biology.detect_rain_episodes("liberec", drizzle, temperature, TODAY) == ()

    soaks = biology.detect_soak_episodes("liberec", _soak_run(14, level=28.0), TODAY)
    assert len(soaks) == 1
    assert soaks[0].days == 14
    assert soaks[0].peak_mm == 28.0


@pytest.mark.parametrize(
    "length, found",
    [(policy.RAIN_SOAK_DAYS - 1, 0), (policy.RAIN_SOAK_DAYS, 1), (20, 1)],
)
def test_a_soak_needs_its_full_run_of_days(length, found):
    soaks = biology.detect_soak_episodes("forest", _soak_run(length), TODAY)
    assert len(soaks) == found


def test_the_soak_line_is_where_policy_puts_it():
    line = policy.RAIN_SOAK_API30_MM
    assert biology.detect_soak_episodes(
        "forest", _soak_run(14, level=line - 0.1), TODAY
    ) == ()
    assert len(biology.detect_soak_episodes(
        "forest", _soak_run(14, level=line), TODAY
    )) == 1
    # and it is deliberately below the pulse threshold: a soak never spikes
    assert line < policy.API30_THRESHOLD_MM


def test_a_soak_anchors_every_day_from_its_nth_on():
    """One anchor per day it went on being wet, starting at the qualifying one.

    A pulse has a single trigger day.  A soak has none, so each day of the
    run opens its own growth window and the chance takes the maximum: while
    the ground stays wet some window is always open.
    """
    soak = biology.detect_soak_episodes("forest", _soak_run(10), TODAY)[0]
    assert soak.start == TODAY - timedelta(days=9)
    assert soak.end == TODAY
    assert soak.anchor == soak.start + timedelta(days=policy.RAIN_SOAK_DAYS - 1)
    assert soak.anchors[0] == soak.anchor
    assert soak.anchors[-1] == TODAY
    assert len(soak.anchors) == 10 - policy.RAIN_SOAK_DAYS + 1
    # this run's anchors are 0..3 days old, so its oldest window is still
    # three days away -- the number climbs into it, it does not switch on
    assert [(TODAY - anchor).days for anchor in soak.anchors] == [3, 2, 1, 0]
    # a soak that has run for weeks always has one anchor inside the window
    longer = biology.detect_soak_episodes("forest", _soak_run(25), TODAY)[0]
    assert any(
        policy.GROWTH_WINDOW_FROM_DAYS
        <= (TODAY - anchor).days
        <= policy.GROWTH_WINDOW_TO_DAYS
        for anchor in longer.anchors
    )


def test_the_first_anchor_of_a_soak_is_a_day_old_so_nothing_jumps_overnight():
    """Why the anchors start at the ``RAIN_SOAK_DAYS``-th day of the run.

    Handing over the whole run would bring an anchor already a week old the
    night a soak first qualifies, and the phase term would jump from its
    floor to its plateau between two daily reports -- a ten-point move the
    send rule would announce, caused by nothing happening.
    """
    just_qualified = biology.detect_soak_episodes(
        "forest", _soak_run(policy.RAIN_SOAK_DAYS), TODAY
    )[0]
    assert just_qualified.anchors == (TODAY,)


def test_a_soak_that_ended_keeps_its_last_anchor_and_ages_out():
    soak = biology.detect_soak_episodes(
        "forest", _soak_run(14, ending=10), TODAY
    )[0]
    assert soak.end == TODAY - timedelta(days=10)
    assert max(soak.anchors) == soak.end


def test_two_soaks_separated_by_a_dry_spell_stay_two():
    series = {TODAY - timedelta(days=n): 5.0 for n in range(0, 36)}
    for n in range(0, 8):
        series[TODAY - timedelta(days=n)] = 30.0
    for n in range(20, 30):
        series[TODAY - timedelta(days=n)] = 30.0

    soaks = biology.detect_soak_episodes("forest", series, TODAY)

    assert [soak.days for soak in soaks] == [10, 8]
    assert soaks[0].end < soaks[1].start


def test_a_hole_in_api30_neither_breaks_a_soak_nor_counts_as_a_day():
    """API30 is a thirty-day integral: one missing day is not a drought."""
    series: dict[date, float | SeriesPoint | None] = dict(_soak_run(14))
    gap = TODAY - timedelta(days=5)
    series[gap] = SeriesPoint(
        date=gap, value=None, source="chmi_station", quality=DataQuality.MISSING
    )

    soaks = biology.detect_soak_episodes("forest", series, TODAY)

    assert len(soaks) == 1
    assert soaks[0].days == 13  # the hole carried no value, so it is not one
    assert soaks[0].start == TODAY - timedelta(days=13)
    assert soaks[0].end == TODAY


def test_a_soak_never_forms_in_the_forecast():
    """Only days up to today count, exactly as for a rain pulse."""
    series = {TODAY - timedelta(days=n): 5.0 for n in range(0, 36)}
    series.update({TODAY + timedelta(days=n): 40.0 for n in range(1, 16)})

    assert biology.detect_soak_episodes("forest", series, TODAY) == ()


def test_the_soak_event_id_is_stable_and_distinct_from_a_pulse():
    first = biology.detect_soak_episodes("forest", _soak_run(14), TODAY)[0]
    again = biology.detect_soak_episodes("forest", _soak_run(14), TODAY)[0]
    assert first.event_id == again.event_id
    assert first.event_id.startswith("rain-")
    pulses = _episodes((-14, 12.0), (-13, 12.0))
    assert all(pulse.event_id != first.event_id for pulse in pulses)


def test_a_soak_does_not_reach_the_categorical_verdict():
    """The safety gate is untouched: it still wants a real rain episode.

    ``assess_day`` takes the episodes it is given, and ``views`` gives it
    only the pulses -- this is the contract that keeps soaks in the chance
    and out of «высокая».
    """
    soaks = biology.detect_soak_episodes("forest", _soak_run(25), TODAY)
    assert soaks and not isinstance(soaks[0], biology.RainEpisode)

    assessment = _assess(TODAY, ())
    assert assessment.phase == "no_episode"
    assert "no_qualified_rain_episode" in assessment.high_blockers
