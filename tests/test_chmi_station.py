"""Offline tests for the ČHMÚ open-data station fetcher.

The ``.raw`` fixtures were recorded from the live server on 2026-09-07 with
``MUSHROOM_RECORD=1`` and then trimmed to the elements the fetcher reads
(the wrapper, the header and every VTYPE of the kept elements are intact,
so VTYPE selection is still exercised against real data).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from mushroom_alerts import fetch_chmi_station as m
from mushroom_alerts.http import Http

from .conftest import BYSTRICE, FIXTURES, FakeHttp, VALMEZ

TODAY = date(2026, 9, 7)

VALMEZ_WSI = "0-203-0-11769"
BYSTRICE_WSI = "0-203-0-41101084001"
ROZNOV_WSI = "0-203-0-11786"


@pytest.fixture
def stations() -> list[m.Station]:
    payload = json.loads(
        (
            FIXTURES
            / Http.fixture_name(
                "https://opendata.chmi.cz/meteorology/climate/recent/metadata/meta1-20260906.json"
            )
        ).read_bytes()
    )
    return m.stations_from_payload(payload)


@pytest.fixture
def valmez_september() -> dict:
    return json.loads(
        (
            FIXTURES
            / Http.fixture_name(
                "https://opendata.chmi.cz/meteorology/climate/recent/data/daily/"
                f"dly-{VALMEZ_WSI}-202609.json"
            )
        ).read_bytes()
    )


# ----------------------------------------------------------------------
# helpers for synthetic payloads
# ----------------------------------------------------------------------
def _doc(header: str, values: list[list]) -> dict:
    """The ČHMÚ ``DataCollection`` wrapper around one table."""
    return {
        "zaznamID": "test",
        "datovyZdrojID": "meteorologie",
        "data": {"type": "DataCollection", "data": {"header": header, "values": values}},
    }


def meta_doc(rows: list[list]) -> dict:
    return _doc("WSI,GH_ID,FULL_NAME,GEOGR1,GEOGR2,ELEVATION,BEGIN_DATE", rows)


def daily_doc(wsi: str, rows: list[tuple]) -> dict:
    """``rows`` are ``(element, vtype, "YYYY-MM-DD", value)``."""
    return _doc(
        "STATION,ELEMENT,VTYPE,DT,VAL,FLAG,QUALITY",
        [[wsi, el, vt, f"{day}T06:00:00Z", val, "", 0.0] for el, vt, day, val in rows],
    )


def ten_min_doc(wsi: str, rows: list[tuple]) -> dict:
    """``rows`` are ``(element, "YYYY-MM-DDTHH:MM:SSZ", value)``."""
    return _doc(
        "STATION,ELEMENT,DT,VAL,FLAG,QUALITY",
        [[wsi, el, when, val, "", 0.0] for el, when, val in rows],
    )


class StubHttp:
    """Serves documents whose key is a substring of the requested URL."""

    def __init__(self, docs: dict[str, object]) -> None:
        self.docs = docs
        self.calls: list[str] = []

    def get_json(self, url: str, params=None):
        self.calls.append(url)
        for key, doc in self.docs.items():
            if key in url:
                return doc
        raise FileNotFoundError(f"404 {url}")


# ----------------------------------------------------------------------
# URLs and month arithmetic
# ----------------------------------------------------------------------
def test_meta_urls_start_at_yesterday_and_end_at_the_archive():
    urls = m.meta_urls(TODAY)
    # The per-day file for D is published at 00:01 on D+1, so `today` itself
    # is normally a 404 -- asking for yesterday first saves a request.
    assert urls[0].endswith("meta1-20260906.json")
    assert urls[1].endswith("meta1-20260907.json")
    assert "recent/metadata/meta1-" in urls[0]
    # ... and the completed-month archive is the rollover fallback (PLAN's
    # only documented form, which does *not* exist for the current month).
    assert urls[-2].endswith("recent/metadata/08/meta1-202608.json")
    assert urls[-1].endswith("recent/metadata/07/meta1-202607.json")


def test_meta_urls_on_the_first_of_the_month():
    urls = m.meta_urls(date(2026, 10, 1))
    assert urls[0].endswith("meta1-20260930.json")  # still in metadata/
    assert any(u.endswith("recent/metadata/09/meta1-202609.json") for u in urls)


def test_daily_url_current_month_vs_archive():
    assert m.daily_url(VALMEZ_WSI, 2026, 9, TODAY) == (
        "https://opendata.chmi.cz/meteorology/climate/recent/data/daily/"
        "dly-0-203-0-11769-202609.json"
    )
    assert m.daily_url(VALMEZ_WSI, 2026, 8, TODAY) == (
        "https://opendata.chmi.cz/meteorology/climate/recent/data/daily/08/"
        "dly-0-203-0-11769-202608.json"
    )


def test_ten_minute_url():
    assert m.ten_minute_url(VALMEZ_WSI, TODAY).endswith(
        "now/data/10m-0-203-0-11769-20260907.json"
    )


def test_months_between_covers_the_window_newest_first():
    assert m.months_between(date(2026, 8, 4), date(2026, 9, 7)) == [(2026, 9), (2026, 8)]
    # 1st of a month: 35 days back reaches two months earlier.
    assert m.months_between(date(2025, 12, 28), date(2026, 2, 1)) == [
        (2026, 2),
        (2026, 1),
        (2025, 12),
    ]


# ----------------------------------------------------------------------
# station index and nearest-station selection
# ----------------------------------------------------------------------
def test_station_index_parses_the_live_file(stations):
    assert len(stations) > 700
    valmez = next(s for s in stations if s.wsi == VALMEZ_WSI)
    assert valmez.name == "Valašské Meziříčí"
    assert valmez.gh_id == "O3VALM01"
    assert valmez.elev_m == 334.0
    # GEOGR1 is the longitude and GEOGR2 the latitude, despite the names.
    assert valmez.lat == pytest.approx(49.46, abs=0.05)
    assert valmez.lon == pytest.approx(17.97, abs=0.05)


def test_nearest_stations_matches_plan(stations):
    (first, d1), (second, d2) = m.nearest_stations(VALMEZ.lat, VALMEZ.lon, stations, 2)
    assert first.wsi == VALMEZ_WSI and first.elev_m == 334.0  # PLAN §2a
    assert d1 < 1.5 and d1 < d2

    (first, d1), _ = m.nearest_stations(BYSTRICE.lat, BYSTRICE.lon, stations, 2)
    assert first.wsi == BYSTRICE_WSI and first.elev_m == 458.0  # PLAN §2a
    assert d1 < 0.5


def test_nearest_stations_n_and_ordering(stations):
    picked = m.nearest_stations(VALMEZ.lat, VALMEZ.lon, stations, 4)
    assert len(picked) == 4
    assert [d for _, d in picked] == sorted(d for _, d in picked)
    assert m.nearest_stations(VALMEZ.lat, VALMEZ.lon, stations, 0) == []


def test_load_station_index_skips_missing_days():
    http = StubHttp({"meta1-20260905.json": meta_doc([["W1", "G1", "N", 18.0, 49.0, 300.0, ""]])})
    got = m.load_station_index(http, TODAY)
    assert [s.wsi for s in got] == ["W1"]
    # yesterday, today, then two days back -- the first two 404ed
    assert len(http.calls) == 3


def test_load_station_index_raises_when_everything_is_gone():
    with pytest.raises(RuntimeError, match="no station index"):
        m.load_station_index(StubHttp({}), TODAY)


# ----------------------------------------------------------------------
# parse_daily
# ----------------------------------------------------------------------
def test_parse_daily_picks_the_daily_aggregate():
    doc = daily_doc(
        "W",
        [
            ("T", "06:00", "2026-09-01", 11.0),
            ("T", "13:00", "2026-09-01", 25.0),
            ("T", "20:00", "2026-09-01", 15.0),
            ("T", "AVG", "2026-09-01", 17.1),
            ("TMA", "20:00", "2026-09-01", 23.9),
            ("TMI", "20:00", "2026-09-01", 13.4),
            ("SRA", "06:00", "2026-09-01", 0.0),
            ("API30", "06:00", "2026-09-01", 27.2),
            ("H", "13:00", "2026-09-01", 40.0),
            ("H", "AVG", "2026-09-01", 61.0),
        ],
    )
    parsed = m.parse_daily(doc)
    day = date(2026, 9, 1)
    assert parsed[("T", day)] == 17.1  # AVG, not the 13:00 term value
    assert parsed[("TMA", day)] == 23.9
    assert parsed[("TMI", day)] == 13.4
    assert parsed[("H", day)] == 61.0
    assert parsed[("SRA", day)] == 0.0
    assert parsed[("API30", day)] == 27.2


def test_parse_daily_drops_nulls_and_survives_junk():
    doc = daily_doc(
        "W",
        [
            ("SRA", "06:00", "2026-09-01", None),
            ("SRA", "06:00", "2026-09-02", 2.3),
            ("T", "AVG", "2026-09-02", None),
            ("T", "13:00", "2026-09-02", 25.0),
        ],
    )
    doc["data"]["data"]["values"].append(["W", "SRA"])  # short row
    doc["data"]["data"]["values"].append(["W", "SRA", "06:00", "not-a-date", 1.0, "", 0.0])
    parsed = m.parse_daily(doc)
    assert ("SRA", date(2026, 9, 1)) not in parsed
    assert parsed[("SRA", date(2026, 9, 2))] == 2.3
    # AVG is null, so the next VTYPE in the order is used rather than nothing
    assert parsed[("T", date(2026, 9, 2))] == 25.0


def test_parse_daily_unknown_element_uses_the_default_order():
    doc = daily_doc(
        "W", [("XYZ", "13:00", "2026-09-01", 1.0), ("XYZ", "AVG", "2026-09-01", 2.0)]
    )
    assert m.parse_daily(doc)[("XYZ", date(2026, 9, 1))] == 2.0


def test_parse_daily_rejects_a_non_table():
    with pytest.raises(ValueError):
        m.parse_daily({"nothing": "here"})
    with pytest.raises(ValueError):
        m.parse_daily("not a dict")


def test_parse_daily_on_the_recorded_file(valmez_september):
    parsed = m.parse_daily(valmez_september)
    assert parsed[("SRA", date(2026, 9, 4))] == 2.3
    assert parsed[("API30", date(2026, 9, 6))] == 20.3
    assert parsed[("T", date(2026, 9, 6))] == 14.2  # the AVG row
    assert parsed[("TMA", date(2026, 9, 6))] == 21.5
    assert parsed[("TMI", date(2026, 9, 6))] == 10.6
    # one value per element and day, not one per VTYPE
    days = {d for _, d in parsed}
    assert max(days) == date(2026, 9, 6)
    assert len({el for el, _ in parsed}) * len(days) == len(parsed)


# ----------------------------------------------------------------------
# the climatological day and the 10-minute series
# ----------------------------------------------------------------------
def test_climatological_day_starts_at_06_utc():
    assert m.climatological_day(datetime(2026, 9, 5, 5, 50, tzinfo=timezone.utc)) == date(2026, 9, 4)
    assert m.climatological_day(datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)) == date(2026, 9, 5)
    assert m.climatological_day(datetime(2026, 9, 5, 23, 50, tzinfo=timezone.utc)) == date(2026, 9, 5)


def test_ten_minute_sra_uses_the_same_window_as_the_daily_row():
    # Real 5.9.2026 rain at Valmez: 2.3 mm before 06:00Z, 0.2 mm after --
    # ČHMÚ books the first lot on 4.9. and the rest on 5.9.
    samples = m.parse_ten_minute(
        ten_min_doc(
            VALMEZ_WSI,
            [
                ("SRA10M", "2026-09-05T04:10:00Z", 0.5),
                ("SRA10M", "2026-09-05T04:20:00Z", 1.1),
                ("SRA10M", "2026-09-05T05:00:00Z", 0.1),
                ("SRA10M", "2026-09-05T05:20:00Z", 0.3),
                ("SRA10M", "2026-09-05T05:30:00Z", 0.1),
                ("SRA10M", "2026-09-05T05:40:00Z", 0.1),
                ("SRA10M", "2026-09-05T05:50:00Z", 0.1),
                ("SRA10M", "2026-09-05T06:10:00Z", 0.1),
                ("SRA10M", "2026-09-05T06:50:00Z", 0.1),
                ("T", "2026-09-05T06:50:00Z", 12.0),
            ],
        )
    )
    total, last = m.ten_minute_sra(samples, date(2026, 9, 4))
    assert total == 2.3 and last == datetime(2026, 9, 5, 5, 50, tzinfo=timezone.utc)
    total, last = m.ten_minute_sra(samples, date(2026, 9, 5))
    assert total == 0.2 and last == datetime(2026, 9, 5, 6, 50, tzinfo=timezone.utc)
    assert m.ten_minute_sra(samples, date(2026, 9, 6)) == (0.0, None)


# ----------------------------------------------------------------------
# fetch, against the recorded live payloads
# ----------------------------------------------------------------------
def test_fetch_live_fixture(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    assert result.ok and result.error is None
    got = {(r.location, r.metric, r.date): r for r in result.readings}

    # Valmez, own station 0.9 km away (PLAN §2a)
    assert got[("valmez", "sra_mm", date(2026, 8, 28))].value == 17.2
    assert got[("valmez", "api30_mm", date(2026, 9, 6))].value == 20.3
    assert got[("valmez", "t_mean", date(2026, 9, 6))].value == 14.2
    assert got[("valmez", "t_max", date(2026, 9, 6))].value == 21.5
    assert got[("valmez", "t_min", date(2026, 9, 6))].value == 10.6
    assert got[("valmez", "rh", date(2026, 9, 6))].value == 62.0
    assert got[("valmez", "t_soil_10", date(2026, 9, 6))].value == 17.7

    reading = got[("valmez", "api30_mm", date(2026, 9, 6))]
    assert reading.source == "chmi_station"
    assert reading.text is None
    station = reading.meta["station"]
    assert station["wsi"] == VALMEZ_WSI
    assert station["elev_m"] == 334.0
    assert station["distance_km"] < 1.5
    assert [s["wsi"] for s in reading.meta["stations"]][0] == VALMEZ_WSI
    assert "fallback" not in reading.meta


def test_fetch_covers_the_whole_window(http, locations):
    result = m.fetch(locations, http=http, today=TODAY)
    days = {r.date for r in result.readings if r.location == "valmez" and r.metric == "sra_mm"}
    assert min(days) == TODAY - timedelta(days=m.DAYS_BACK - 1)
    assert len(days) == m.DAYS_BACK


def test_fetch_fills_temperature_from_a_second_station(http, locations):
    """Valašská Bystřice measures rain only -- the fallback brings the rest."""
    result = m.fetch(locations, http=http, today=TODAY)
    got = {(r.location, r.metric, r.date): r for r in result.readings}

    rain = got[("valasska-bystrice", "sra_mm", date(2026, 8, 28))]
    assert rain.value == 12.1
    assert rain.meta["wsi"] == BYSTRICE_WSI
    assert "fallback" not in rain.meta

    warm = got[("valasska-bystrice", "t_mean", date(2026, 9, 6))]
    assert warm.value == 13.5
    assert warm.meta["wsi"] == ROZNOV_WSI
    assert warm.meta["fallback"] is True
    # meta["station"] stays the *primary* one so the params cache is stable
    assert warm.meta["station"]["wsi"] == BYSTRICE_WSI
    assert [s["wsi"] for s in warm.meta["stations"]] == [BYSTRICE_WSI, ROZNOV_WSI]
    assert [s["elev_m"] for s in warm.meta["stations"]] == [458.0, 375.0]


def test_fetch_publishes_a_provisional_today(http, locations):
    """The daily file stops at 6.9.; 10-minute data covers 7.9. so far."""
    result = m.fetch(locations, http=http, today=TODAY)
    today_rain = [
        r for r in result.readings if r.date == TODAY and r.location == "valmez"
    ]
    assert [r.metric for r in today_rain] == ["sra_mm"]
    meta = today_rain[0].meta
    assert meta["provisional"] is True
    assert meta["element"] == "SRA10M"
    assert meta["complete"] is False  # the day is not over yet
    assert meta["until"].startswith("2026-09-07T17:00")
    # ... and nothing provisional for a day the daily file already has
    assert not any(
        (r.meta or {}).get("provisional") for r in result.readings if r.date < TODAY
    )


def test_fetch_params_land_in_the_store(http, locations):
    from mushroom_alerts.__main__ import cache_params
    from mushroom_alerts.store import Store

    result = m.fetch(locations, http=http, today=TODAY)
    with Store(":memory:") as store:
        cache_params(store, [result])
        params = store.get_params("valasska-bystrice")
    assert params["chmi_station"]["wsi"] == BYSTRICE_WSI
    assert params["chmi_station"]["name"] == "Valašská Bystřice"
    assert [s["wsi"] for s in params["chmi_stations"]] == [BYSTRICE_WSI, ROZNOV_WSI]


def test_fetch_is_polite_enough(http, locations):
    m.fetch(locations, http=http, today=TODAY)
    # station index + a handful of small month files + one 10-minute file
    # per location; nowhere near a request per location per day.
    assert len(http.calls) <= 16
    assert sum(1 for c in http.calls if "meta1-" in c) == 1


# ----------------------------------------------------------------------
# soft failure
# ----------------------------------------------------------------------
def test_fetch_never_raises_when_the_server_is_down(locations):
    result = m.fetch(locations, http=FakeHttp(fail=True), today=TODAY)
    assert result.ok is False and result.readings == []
    assert "no station index" in (result.error or "")


def test_fetch_without_locations():
    assert m.fetch([], http=FakeHttp(), today=TODAY).ok is False


def test_fetch_soft_fails_when_every_daily_file_is_a_404(locations):
    http = StubHttp(
        {"meta1-20260906.json": meta_doc([[VALMEZ_WSI, "G", "Valmez", 17.97, 49.47, 334.0, ""]])}
    )
    result = m.fetch(locations, http=http, today=TODAY)
    assert result.ok is False
    assert "no station data" in (result.error or "")


def test_fetch_skips_a_station_whose_file_is_missing():
    """A 404 on the nearest station falls through to the next one."""
    near = ["0-203-0-DEAD", "G1", "Dead", 17.9711, 49.4718, 300.0, ""]
    far = ["0-203-0-ALIVE", "G2", "Alive", 17.99, 49.48, 310.0, ""]
    http = StubHttp(
        {
            "meta1-20260906.json": meta_doc([near, far]),
            "dly-0-203-0-ALIVE-202609": daily_doc(
                "0-203-0-ALIVE",
                [("SRA", "06:00", "2026-09-06", 4.2), ("T", "AVG", "2026-09-06", 15.0)],
            ),
        }
    )
    result = m.fetch([VALMEZ], http=http, today=TODAY)
    assert result.ok
    rain = next(r for r in result.readings if r.metric == "sra_mm")
    assert rain.value == 4.2
    assert rain.meta["station"]["wsi"] == "0-203-0-ALIVE"


def test_fetch_reports_one_bad_location_and_keeps_the_good_one():
    from mushroom_alerts.base import Location

    http = StubHttp(
        {
            "meta1-20260906.json": meta_doc(
                [[VALMEZ_WSI, "G", "Valmez", 17.9711, 49.4718, 334.0, ""]]
            ),
            f"dly-{VALMEZ_WSI}-202609": daily_doc(
                VALMEZ_WSI,
                [("SRA", "06:00", "2026-09-06", 1.0), ("T", "AVG", "2026-09-06", 15.0)],
            ),
        }
    )
    lisboa = Location(name="Lisboa", lat=38.7, lon=-9.1, slug="lisboa")
    result = m.fetch([VALMEZ, lisboa], http=http, today=TODAY)
    assert result.ok is True
    assert {r.location for r in result.readings} == {"valmez"}
    assert "lisboa" in (result.error or "")


def test_ten_minute_failure_does_not_lose_the_daily_readings():
    http = StubHttp(
        {
            "meta1-20260906.json": meta_doc(
                [[VALMEZ_WSI, "G", "Valmez", 17.9711, 49.4718, 334.0, ""]]
            ),
            f"dly-{VALMEZ_WSI}-202609": daily_doc(
                VALMEZ_WSI,
                [("SRA", "06:00", "2026-09-06", 1.0), ("T", "AVG", "2026-09-06", 15.0)],
            ),
        }
    )  # no 10m-... document -> FileNotFoundError inside the optional step
    result = m.fetch([VALMEZ], http=http, today=TODAY)
    assert result.ok and len(result.readings) == 2
    assert not any((r.meta or {}).get("provisional") for r in result.readings)


# ----------------------------------------------------------------------
# month rollover
# ----------------------------------------------------------------------
def test_fetch_on_the_first_of_the_month_reads_the_archive():
    """1.10.: the current-month daily file does not exist yet and the daily
    metadata files have been swept into ``metadata/09/``."""
    first = date(2026, 10, 1)
    http = StubHttp(
        {
            "recent/metadata/09/meta1-202609.json": meta_doc(
                [[VALMEZ_WSI, "G", "Valmez", 17.9711, 49.4718, 334.0, ""]]
            ),
            f"daily/09/dly-{VALMEZ_WSI}-202609": daily_doc(
                VALMEZ_WSI,
                [
                    ("SRA", "06:00", "2026-09-29", 6.0),
                    ("SRA", "06:00", "2026-09-30", 1.0),
                    ("T", "AVG", "2026-09-30", 12.0),
                ],
            ),
            f"daily/08/dly-{VALMEZ_WSI}-202608": daily_doc(
                VALMEZ_WSI, [("SRA", "06:00", "2026-08-28", 17.2)]
            ),
        }
    )
    result = m.fetch([VALMEZ], http=http, today=first)
    assert result.ok, result.error
    got = {(r.metric, r.date): r.value for r in result.readings}
    assert got[("sra_mm", date(2026, 9, 30))] == 1.0
    assert got[("t_mean", date(2026, 9, 30))] == 12.0
    # the 35-day window reaches back into August, whose archive was read too
    assert got[("sra_mm", date(2026, 8, 28))] == 17.2
    # the current month was tried first and 404ed, which is not an error
    assert any(f"daily/dly-{VALMEZ_WSI}-202610" in c for c in http.calls)
    assert result.error is None
