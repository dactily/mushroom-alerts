from __future__ import annotations

from datetime import date, timedelta

from mushroom_alerts.base import DataQuality, Reading, SeriesPoint
from mushroom_alerts.quality import calendar_window, point_from_reading

TODAY = date(2026, 9, 7)


def test_calendar_window_uses_dates_not_last_available_rows():
    values = {
        TODAY - timedelta(days=20): 10.0,
        TODAY - timedelta(days=10): 10.0,
        TODAY - timedelta(days=1): 10.0,
    }
    aggregate = calendar_window(values, TODAY, 3)
    assert aggregate.total == 10.0
    assert aggregate.covered_days == 1
    assert aggregate.expected_days == 3
    assert aggregate.lower_bound is True
    assert aggregate.quality is DataQuality.PARTIAL


def test_calendar_window_propagates_stale_quality():
    values = {
        TODAY - timedelta(days=n): SeriesPoint(
            TODAY - timedelta(days=n), 1.0, "source", DataQuality.STALE if n == 1 else DataQuality.FRESH
        )
        for n in range(3)
    }
    aggregate = calendar_window(values, TODAY, 3)
    assert aggregate.total == 3.0
    assert aggregate.quality is DataQuality.STALE
    assert aggregate.lower_bound is False


def test_point_quality_from_metadata():
    partial = Reading(
        "chmi_station",
        "valmez",
        TODAY,
        "sra_mm",
        1.2,
        meta={"provisional": True, "complete": False, "covered_slots": 30, "expected_slots": 144},
    )
    point = point_from_reading(partial, TODAY)
    assert point.quality is DataQuality.PARTIAL
    assert point.covered == 30 and point.expected == 144

    stale = Reading("houbymapa", "valmez", TODAY, "level", 5.0, meta={"stale": True})
    assert point_from_reading(stale, TODAY).quality is DataQuality.STALE
