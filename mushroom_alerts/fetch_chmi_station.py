"""ČHMÚ open data, daily station observations -> SRA / API30 / T ... per location.

Official, CC BY 4.0, stable format -- the source PLAN §6 wants the main
trigger to hang on.  For every location we pick the nearest station(s) that
actually publish data and turn their daily rows into :class:`Reading`\\ s for
the last :data:`DAYS_BACK` days.

Endpoints (all re-verified against the live server on 2026-09-07)
-----------------------------------------------------------------
Station index
    ``…/climate/recent/metadata/meta1-YYYYMMDD.json`` -- one file **per day**,
    only for the current month; the file for day *D* appears at 00:01 on
    *D+1*, so ``today`` itself is normally a 404 and we start at ``today-1``.
    Completed months are archived as
    ``…/recent/metadata/MM/meta1-YYYYMM.json`` (``MM`` = literal month
    number, holding the newest completed instance of that month), which is
    the fallback around a month rollover.

    *PLAN §1 is wrong here*: it lists only the ``MM/meta1-YYYYMM.json`` form,
    which for the **current** month does not exist -- ``metadata/09/`` on
    2026-09-07 still holds ``meta1-202509.json``, i.e. last year's September.

    Columns: ``WSI,GH_ID,FULL_NAME,GEOGR1,GEOGR2,ELEVATION,BEGIN_DATE``.
    ``GEOGR1`` is the **longitude**, ``GEOGR2`` the latitude.  There is no
    END_DATE, so "is this station alive" is answered by whether its current
    daily file exists -- which is exactly what :func:`fetch` probes.

Daily data
    current month  ``…/recent/data/daily/dly-<WSI>-YYYYMM.json``
    earlier months ``…/recent/data/daily/MM/dly-<WSI>-YYYYMM.json``
    Rows: ``STATION,ELEMENT,VTYPE,DT,VAL,FLAG,QUALITY``.

10-minute data (optional, fills the last, not-yet-published day)
    ``…/climate/now/data/10m-<WSI>-YYYYMMDD.json``, rows
    ``STATION,ELEMENT,DT,VAL,FLAG,QUALITY``, element ``SRA10M``.

The climatological day
----------------------
``DT`` is the **start** of the interval it describes.  A daily ``SRA`` row
stamped ``2026-09-04T06:00:00Z`` is the precipitation total of
``[2026-09-04 06:00Z, 2026-09-05 06:00Z)`` -- verified against the 10-minute
series: 2.3 mm fell between 04:10Z and 06:00Z on 5.9. and ČHMÚ books it on
**4.9.**, the remaining 0.2 mm after 06:00Z on 5.9.  :func:`ten_minute_sra`
uses that same window, so a provisional value and the real one that replaces
it tomorrow mean the same thing.  ``Reading.date`` is always the DT date.

VTYPE
-----
Several rows may exist per element and day (term values at 06/13/20 h plus
an aggregate).  :func:`parse_daily` keeps exactly one, chosen by
:data:`VTYPE_PREFERENCE` with :data:`DEFAULT_VTYPE_ORDER` as the fallback:
``AVG`` for T/H/soil temperatures, the single ``06:00`` row for SRA/API30,
``20:00`` for TMA/TMI.

Station choice
--------------
The nearest station with data wins.  A second one is kept as a fallback --
and, when the nearest is a rain-only station (Valašská Bystřice measures
SRA and nothing else), the fallback is pushed outwards until it supplies the
missing core metric, so every location ends up with both precipitation and
temperature.  Values coming from the fallback carry ``meta["fallback"]``.

``meta["station"]`` is always the **primary** station of the location -- it
is what ``__main__.cache_params`` stores as ``chmi_station`` -- and
``meta["stations"]`` lists the picked ones.  To keep the SQLite file small
those two only ride on the readings of the newest day; older days carry just
``wsi``/``distance_km``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .base import FetchResult, Location, Reading, haversine_km

__all__ = [
    "SOURCE",
    "BASE_URL",
    "DAYS_BACK",
    "ELEMENTS",
    "VTYPE_PREFERENCE",
    "Station",
    "fetch",
    "load_station_index",
    "nearest_stations",
    "parse_daily",
    "parse_ten_minute",
    "meta_urls",
    "daily_url",
    "ten_minute_url",
    "climatological_day",
    "ten_minute_sra",
    "ten_minute_sra_detail",
    "TenMinuteCoverage",
]

SOURCE = "chmi_station"
BASE_URL = "https://opendata.chmi.cz/meteorology/climate"

#: How much history one run publishes (PLAN §3 needs 30 days for API30).
DAYS_BACK = 35

#: ČHMÚ ELEMENT code -> ``Reading.metric`` (see the table in ``base``).
ELEMENTS: dict[str, str] = {
    "SRA": "sra_mm",  # Srážka, mm
    "API30": "api30_mm",  # Index předchozích srážek, mm
    "T": "t_mean",  # Teplota, °C
    "TMA": "t_max",  # Teplota max, °C
    "TMI": "t_min",  # Teplota min, °C
    "H": "rh",  # Vlhkost relativní, %
    "T05": "t_soil_5",  # Teplota půdy 5 cm, °C
    "T10": "t_soil_10",  # Teplota půdy 10 cm, °C
    "T20": "t_soil_20",  # Teplota půdy 20 cm, °C
}

#: Metrics a location really wants; drives the fallback-station search.
CORE_METRICS = frozenset({"sra_mm", "t_mean"})

#: Which VTYPE is "the" daily value for an element.
VTYPE_PREFERENCE: dict[str, tuple[str, ...]] = {
    "SRA": ("06:00",),
    "API30": ("06:00",),
    "T": ("AVG",),
    "TMA": ("20:00",),
    "TMI": ("20:00",),
    "H": ("AVG",),
    "T05": ("AVG",),
    "T10": ("AVG",),
    "T20": ("AVG",),
    "T50": ("AVG",),
    "T100": ("AVG",),
}

#: Fallback order for elements not in :data:`VTYPE_PREFERENCE`.
DEFAULT_VTYPE_ORDER: tuple[str, ...] = ("AVG", "06:00", "20:00", "13:00", "00:00", "")

#: How many nearest stations we may probe before giving up on a location.
CANDIDATE_POOL = 6
#: How many stations end up in ``meta["stations"]``.
MAX_STATIONS = 2
#: A station further away than this is not "the local weather" any more.
MAX_STATION_DISTANCE_KM = 40.0
#: Daily metadata files to try before falling back to the monthly archive.
META_LOOKBACK_DAYS = 4

#: 10-minute data is a nice-to-have; set to False to skip those requests.
TEN_MINUTE = True
#: Never ask for more than this many not-yet-published days.
MAX_PROVISIONAL_DAYS = 2

#: ELEMENT code of 10-minute precipitation.
TEN_MINUTE_SRA = "SRA10M"

#: Start of the ČHMÚ climatological day, UTC.
DAY_START_HOUR = 6


# ----------------------------------------------------------------------
# stations
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Station:
    """One row of ``meta1``: a ČHMÚ observing station."""

    wsi: str
    gh_id: str
    name: str
    lat: float
    lon: float
    elev_m: float | None = None
    begin: str | None = None

    def as_dict(self, distance_km: float | None = None) -> dict[str, Any]:
        """The JSON blob cached as ``chmi_station`` (PLAN §2a)."""
        out: dict[str, Any] = {
            "wsi": self.wsi,
            "gh_id": self.gh_id,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "elev_m": self.elev_m,
        }
        if distance_km is not None:
            out["distance_km"] = round(distance_km, 2)
        return out


def _table(payload: Any) -> tuple[list[str], list[list[Any]]]:
    """``(header columns, rows)`` out of the ČHMÚ ``DataCollection`` wrapper."""
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    node: Any = payload
    for _ in range(4):  # {..., "data": {"type": ..., "data": {header, values}}}
        if isinstance(node, dict) and "header" in node and "values" in node:
            break
        if isinstance(node, dict) and "data" in node:
            node = node["data"]
        else:
            node = None
            break
    if not isinstance(node, dict) or "header" not in node or "values" not in node:
        raise ValueError("payload has no header/values table")
    header = [c.strip() for c in str(node["header"]).split(",")]
    values = node["values"]
    if not isinstance(values, list):
        raise ValueError("'values' is not a list")
    return header, [r for r in values if isinstance(r, (list, tuple))]


def _column_getter(header: Sequence[str], *names: str) -> int:
    for name in names:
        if name in header:
            return header.index(name)
    raise ValueError(f"column {names[0]!r} missing from {','.join(header)}")


def stations_from_payload(payload: Any) -> list[Station]:
    """Parse a ``meta1`` document into :class:`Station` objects."""
    header, rows = _table(payload)
    i_wsi = _column_getter(header, "WSI")
    i_gh = _column_getter(header, "GH_ID")
    i_name = _column_getter(header, "FULL_NAME", "NAME")
    i_lon = _column_getter(header, "GEOGR1")  # longitude, despite the name
    i_lat = _column_getter(header, "GEOGR2")
    i_elev = header.index("ELEVATION") if "ELEVATION" in header else None
    i_begin = header.index("BEGIN_DATE") if "BEGIN_DATE" in header else None

    out: list[Station] = []
    for row in rows:
        try:
            lat = float(row[i_lat])
            lon = float(row[i_lon])
        except (TypeError, ValueError, IndexError):
            continue
        elev = None
        if i_elev is not None and i_elev < len(row):
            try:
                elev = float(row[i_elev])
            except (TypeError, ValueError):
                elev = None
        out.append(
            Station(
                wsi=str(row[i_wsi]),
                gh_id=str(row[i_gh]) if i_gh < len(row) else "",
                name=str(row[i_name]) if i_name < len(row) else "",
                lat=lat,
                lon=lon,
                elev_m=elev,
                begin=str(row[i_begin])[:10] if i_begin is not None and i_begin < len(row) else None,
            )
        )
    if not out:
        raise ValueError("station index is empty")
    return out


def meta_urls(today: date) -> list[str]:
    """Candidate ``meta1`` URLs, most likely first.

    ``today-1`` leads: the per-day file is published at 00:01 the following
    night, so asking for ``today`` first would waste a 404 on every run.
    """
    days = [today - timedelta(days=1), today]
    days += [today - timedelta(days=n) for n in range(2, META_LOOKBACK_DAYS + 1)]
    urls = [f"{BASE_URL}/recent/metadata/meta1-{d:%Y%m%d}.json" for d in days]
    month = date(today.year, today.month, 1)
    for _ in range(2):  # completed months, for the rollover
        month = (month - timedelta(days=1)).replace(day=1)
        urls.append(f"{BASE_URL}/recent/metadata/{month:%m}/meta1-{month:%Y%m}.json")
    return urls


def load_station_index(http: Any, today: date) -> list[Station]:
    """Download the station list.  Raises only if every candidate URL fails."""
    problems: list[str] = []
    for url in meta_urls(today):
        try:
            return stations_from_payload(http.get_json(url))
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            problems.append(f"{url.rsplit('/', 1)[-1]}: {type(exc).__name__}")
    raise RuntimeError("no station index available (" + "; ".join(problems[:3]) + ")")


def nearest_stations(
    lat: float, lon: float, stations: Iterable[Station], n: int = 2
) -> list[tuple[Station, float]]:
    """The ``n`` closest stations as ``(station, distance_km)``, nearest first."""
    scored = [(s, haversine_km(lat, lon, s.lat, s.lon)) for s in stations]
    scored.sort(key=lambda pair: pair[1])
    return scored[: max(0, n)]


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------
def _dt(raw: Any) -> datetime:
    text = str(raw).replace("Z", "+00:00")
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_daily(payload: Any) -> dict[tuple[str, date], float]:
    """``{(ELEMENT, date): value}`` with one VTYPE picked per element and day.

    Rows with ``VAL = null`` are dropped; a day where only the unwanted VTYPE
    has a value therefore disappears rather than becoming a wrong number.
    """
    header, rows = _table(payload)
    i_el = _column_getter(header, "ELEMENT")
    i_vt = header.index("VTYPE") if "VTYPE" in header else None
    i_dt = _column_getter(header, "DT")
    i_val = _column_getter(header, "VAL")

    by_vtype: dict[tuple[str, date], dict[str, float]] = {}
    for row in rows:
        try:
            value = row[i_val]
            if value is None:
                continue
            key = (str(row[i_el]), _dt(row[i_dt]).date())
            vtype = str(row[i_vt]) if i_vt is not None and i_vt < len(row) else ""
            by_vtype.setdefault(key, {})[vtype] = float(value)
        except (IndexError, TypeError, ValueError):
            continue

    out: dict[tuple[str, date], float] = {}
    for key, variants in by_vtype.items():
        vtype = _pick_vtype(key[0], variants)
        if vtype is not None:
            out[key] = variants[vtype]
    return out


def _pick_vtype(element: str, variants: dict[str, float]) -> str | None:
    order = list(VTYPE_PREFERENCE.get(element, ()))
    order += [v for v in DEFAULT_VTYPE_ORDER if v not in order]
    order += [v for v in sorted(variants) if v not in order]
    for vtype in order:
        if vtype in variants:
            return vtype
    return None


def parse_ten_minute(payload: Any) -> dict[tuple[str, datetime], float]:
    """``{(ELEMENT, utc datetime): value}`` from a ``10m-…`` document."""
    header, rows = _table(payload)
    i_el = _column_getter(header, "ELEMENT")
    i_dt = _column_getter(header, "DT")
    i_val = _column_getter(header, "VAL")
    out: dict[tuple[str, datetime], float] = {}
    for row in rows:
        try:
            value = row[i_val]
            if value is None:
                continue
            out[(str(row[i_el]), _dt(row[i_dt]))] = float(value)
        except (IndexError, TypeError, ValueError):
            continue
    return out


def climatological_day(moment: datetime) -> date:
    """Which ČHMÚ daily row a UTC instant belongs to (day starts at 06:00Z)."""
    return (moment.astimezone(timezone.utc) - timedelta(hours=DAY_START_HOUR)).date()


def ten_minute_sra(
    samples: dict[tuple[str, datetime], float], day: date
) -> tuple[float, datetime | None]:
    """Sum ``SRA10M`` over ``[day 06:00Z, day+1 06:00Z)``.

    Returns ``(mm, last sample used)``; ``(0.0, None)`` when the window has no
    samples at all, which is how the caller knows not to publish anything.
    """
    total = 0.0
    last: datetime | None = None
    for (element, when), value in samples.items():
        if element != TEN_MINUTE_SRA or climatological_day(when) != day:
            continue
        total += value
        if last is None or when > last:
            last = when
    return round(total, 2), last


@dataclass(frozen=True, slots=True)
class TenMinuteCoverage:
    """Precipitation total and coverage of one 144-slot climatic day."""

    total: float
    first: datetime | None
    last: datetime | None
    covered_slots: int
    expected_slots: int
    missing: tuple[datetime, ...]

    @property
    def complete(self) -> bool:
        return self.covered_slots == self.expected_slots


def ten_minute_sra_detail(
    samples: dict[tuple[str, datetime], float], day: date
) -> TenMinuteCoverage:
    """Return SRA plus exact coverage of the expected UTC timestamps."""
    start = datetime(day.year, day.month, day.day, DAY_START_HOUR, tzinfo=timezone.utc)
    expected = tuple(start + timedelta(minutes=10 * n) for n in range(24 * 6))
    used = {
        when.astimezone(timezone.utc): float(value)
        for (element, when), value in samples.items()
        if element == TEN_MINUTE_SRA and climatological_day(when) == day
    }
    present = sorted(set(expected) & set(used))
    missing = tuple(stamp for stamp in expected if stamp not in used)
    return TenMinuteCoverage(
        total=round(sum(used[stamp] for stamp in present), 2),
        first=present[0] if present else None,
        last=present[-1] if present else None,
        covered_slots=len(present),
        expected_slots=len(expected),
        missing=missing,
    )


# ----------------------------------------------------------------------
# URLs
# ----------------------------------------------------------------------
def daily_url(wsi: str, year: int, month: int, today: date) -> str:
    """Current month lives in ``daily/``, older months in ``daily/MM/``."""
    stamp = f"{year:04d}{month:02d}"
    if (year, month) == (today.year, today.month):
        return f"{BASE_URL}/recent/data/daily/dly-{wsi}-{stamp}.json"
    return f"{BASE_URL}/recent/data/daily/{month:02d}/dly-{wsi}-{stamp}.json"


def ten_minute_url(wsi: str, day: date) -> str:
    return f"{BASE_URL}/now/data/10m-{wsi}-{day:%Y%m%d}.json"


def months_between(since: date, until: date) -> list[tuple[int, int]]:
    """Every ``(year, month)`` touched by ``[since, until]``, newest first."""
    out: list[tuple[int, int]] = []
    cursor = date(until.year, until.month, 1)
    first = date(since.year, since.month, 1)
    while cursor >= first:
        out.append((cursor.year, cursor.month))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return out


# ----------------------------------------------------------------------
# fetching
# ----------------------------------------------------------------------
class _Files:
    """Per-run download cache, so two locations sharing a station pay once."""

    def __init__(self, http: Any, today: date, since: date) -> None:
        self.http = http
        self.today = today
        self.since = since
        self.months = months_between(since, today)
        self._daily: dict[tuple[str, int, int], dict[tuple[str, date], float] | None] = {}
        self._ten_min: dict[tuple[str, date], dict[tuple[str, datetime], float]] = {}

    def month(self, wsi: str, year: int, month: int) -> dict[tuple[str, date], float] | None:
        key = (wsi, year, month)
        if key not in self._daily:
            url = daily_url(wsi, year, month, self.today)
            try:
                parsed = parse_daily(self.http.get_json(url))
            except Exception:  # noqa: BLE001 - a missing month is not an error
                parsed = {}
            parsed = {k: v for k, v in parsed.items() if self.since <= k[1] <= self.today}
            self._daily[key] = parsed or None
        return self._daily[key]

    def probe(self, wsi: str) -> dict[tuple[str, date], float]:
        """Newest month that has data inside the window -- the liveness test."""
        for year, month in self.months:
            got = self.month(wsi, year, month)
            if got:
                return dict(got)
        return {}

    def window(self, wsi: str) -> dict[tuple[str, date], float]:
        merged: dict[tuple[str, date], float] = {}
        for year, month in self.months:
            got = self.month(wsi, year, month)
            if got:
                merged.update(got)
        return merged

    def ten_minute(self, wsi: str, day: date) -> dict[tuple[str, datetime], float]:
        key = (wsi, day)
        if key not in self._ten_min:
            try:
                self._ten_min[key] = parse_ten_minute(
                    self.http.get_json(ten_minute_url(wsi, day))
                )
            except Exception:  # noqa: BLE001 - optional data, PLAN §6
                self._ten_min[key] = {}
        return self._ten_min[key]


@dataclass(slots=True)
class _Picked:
    station: Station
    distance_km: float
    values: dict[tuple[str, date], float]

    @property
    def metrics(self) -> set[str]:
        return {ELEMENTS[el] for el, _ in self.values if el in ELEMENTS}


def _choose(
    location: Location, stations: Sequence[Station], files: _Files
) -> list[_Picked]:
    """Primary = nearest station with data.  Secondary completes it if it can."""
    picked: list[_Picked] = []
    spare: _Picked | None = None
    for station, distance in nearest_stations(
        location.lat, location.lon, stations, CANDIDATE_POOL
    ):
        if distance > MAX_STATION_DISTANCE_KM:
            break
        values = files.probe(station.wsi)
        if not values:
            continue
        candidate = _Picked(station, distance, values)
        if not picked:
            picked.append(candidate)
            continue
        # A complete primary takes whatever comes next as its fallback; an
        # incomplete one (rain-only station) keeps looking for the rest.
        missing = CORE_METRICS - picked[0].metrics
        if not missing or missing & candidate.metrics:
            picked.append(candidate)
            break
        spare = spare or candidate
    if len(picked) < MAX_STATIONS and spare is not None:
        picked.append(spare)
    return picked[:MAX_STATIONS]


def _provisional_sra(
    files: _Files, wsi: str, last_day: date | None, today: date
) -> list[tuple[date, TenMinuteCoverage]]:
    """Sum 10-minute rain for the days the daily file has not reached yet."""
    if not TEN_MINUTE:
        return []
    start = (last_day + timedelta(days=1)) if last_day else today
    days = _daterange(start, today)[-MAX_PROVISIONAL_DAYS:]
    out: list[tuple[date, TenMinuteCoverage]] = []
    for day in days:
        # A climatological day spans two calendar files: [D 06:00Z, D+1 06:00Z).
        samples: dict[tuple[str, datetime], float] = dict(files.ten_minute(wsi, day))
        if day + timedelta(days=1) <= today:
            samples.update(files.ten_minute(wsi, day + timedelta(days=1)))
        detail = ten_minute_sra_detail(samples, day)
        if detail.last is not None:
            out.append((day, detail))
    return out


def _daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def fetch(locations: Iterable[Location], *, http: Any, today: date) -> FetchResult:
    """One :class:`FetchResult` with ``DAYS_BACK`` days per location.

    Never raises: a dead endpoint is ``ok=False``, a location we could not
    resolve is a note in ``error`` next to the locations that worked.
    """
    locs = list(locations)
    if not locs:
        return FetchResult.failure(SOURCE, "no locations")
    try:
        stations = load_station_index(http, today)
    except Exception as exc:  # noqa: BLE001 - soft failure, PLAN §6
        return FetchResult.failure(SOURCE, f"{type(exc).__name__}: {exc}")

    since = today - timedelta(days=DAYS_BACK - 1)
    files = _Files(http, today, since)

    readings: list[Reading] = []
    problems: list[str] = []
    location_errors: dict[str, str] = {}
    for loc in locs:
        try:
            readings.extend(_readings_for(loc, stations, files))
        except Exception as exc:  # noqa: BLE001 - one bad location, not a dead source
            detail = f"{type(exc).__name__}: {exc}"
            location_errors[loc.slug] = detail
            problems.append(f"{loc.slug}: {detail}")
            continue
        if not any(r.location == loc.slug for r in readings):
            location_errors[loc.slug] = "no station data"
            problems.append(f"{loc.slug}: no station data")

    error = "; ".join(problems) or None
    if not readings:
        return FetchResult.failure(SOURCE, error or "no station data", location_errors)
    return FetchResult.success(SOURCE, readings, error, location_errors)


def _readings_for(
    loc: Location, stations: Sequence[Station], files: _Files
) -> list[Reading]:
    since, today = files.since, files.today
    picked = _choose(loc, stations, files)
    if not picked:
        return []
    for item in picked:  # the probe only loaded one month; get the rest
        item.values = files.window(item.station.wsi)

    primary = picked[0]
    station_meta = primary.station.as_dict(primary.distance_km)
    stations_meta = [p.station.as_dict(p.distance_km) for p in picked]
    newest = max((d for _, d in primary.values), default=since)

    out: list[Reading] = []
    seen: set[tuple[str, date]] = set()
    for item in picked:
        fallback = item is not primary
        for (element, day), value in sorted(item.values.items()):
            metric = ELEMENTS.get(element)
            if metric is None or (metric, day) in seen or not since <= day <= today:
                continue
            seen.add((metric, day))
            meta: dict[str, Any] = {
                "wsi": item.station.wsi,
                "distance_km": round(item.distance_km, 2),
                "element": element,
            }
            if fallback:
                meta["fallback"] = True
            if day >= newest:  # only the newest day carries the heavy blob
                meta["station"] = station_meta
                meta["stations"] = stations_meta
            out.append(
                Reading(
                    source=SOURCE,
                    location=loc.slug,
                    date=day,
                    metric=metric,
                    value=value,
                    text=None,
                    meta=meta,
                )
            )

    last_sra = max((d for el, d in primary.values if el == "SRA"), default=None)
    try:
        provisional = _provisional_sra(files, primary.station.wsi, last_sra, today)
    except Exception:  # noqa: BLE001 - optional extra, never fatal
        provisional = []
    for day, detail in provisional:
        if ("sra_mm", day) in seen:
            continue
        seen.add(("sra_mm", day))
        out.append(
            Reading(
                source=SOURCE,
                location=loc.slug,
                date=day,
                metric="sra_mm",
                value=detail.total,
                text=None,
                meta={
                    "wsi": primary.station.wsi,
                    "distance_km": round(primary.distance_km, 2),
                    "element": TEN_MINUTE_SRA,
                    "provisional": True,
                    "complete": detail.complete,
                    "from": detail.first.isoformat() if detail.first else None,
                    "until": detail.last.isoformat() if detail.last else None,
                    "covered_slots": detail.covered_slots,
                    "expected_slots": detail.expected_slots,
                    "gaps": [stamp.isoformat() for stamp in detail.missing],
                    "station": station_meta,
                    "stations": stations_meta,
                },
            )
        )
    return out


def _day_end(day: date) -> datetime:
    """Last 10-minute slot of the climatological day ``day``."""
    start = datetime(day.year, day.month, day.day, DAY_START_HOUR, tzinfo=timezone.utc)
    return start + timedelta(days=1) - timedelta(minutes=10)
