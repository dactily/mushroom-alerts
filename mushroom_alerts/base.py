"""Shared contract for every ``mushroom_alerts`` fetcher and for ``rules.py``.

Read this one file and you know everything you need to write a new source
module.  Nothing here imports anything outside the stdlib, so it is safe to
import from any module of the package.

Terminology
-----------
Location
    A place the user cares about: name + coordinates + a stable ``slug``.
    ``locations.yaml`` holds only ``name``/``lat``/``lon`` (+ optional
    ``slug``); everything derived (ČHMÚ pixel, HoubyMapa cell, nearest
    stations) is cached in SQLite under that slug -- see PLAN §2a and
    ``store.Store.get_params``/``set_params``.

Reading
    Exactly one measured value, for one location, for one day, for one
    metric.  A fetcher returns a flat list of them; the store upserts them.

FetchResult
    What one source produced in one run, plus a soft-failure flag.

The fetcher protocol
--------------------
Every ``fetch_*.py`` module MUST expose::

    SOURCE: str                      # one of SOURCES below
    def fetch(locations: list[Location], *, http: Http,
              today: date) -> FetchResult

Rules for ``fetch``:

* **It must never raise.**  Catch everything (``except Exception``) and
  return ``FetchResult.failure(SOURCE, "...")``.  A dead source is a note in
  the report, not a crash -- PLAN §6.  ``base.soft_fetch`` wraps a callable
  for you and is what the CLI uses, but implement the guarantee anyway so
  direct callers are safe too.
* Keyword-only ``http`` and ``today`` -- always pass them by name.
* Partial success is fine and expected: return ``ok=True`` with the readings
  you did get, and put the problem in ``error``.
* Be polite: one or two HTTP requests per run, not one per location.  Fetch
  the country-wide payload once, then slice it per location.
* Cache anything expensive to derive (pixel, grid cell, station list) with
  ``store.set_params(slug, {...})`` -- but a fetcher may also be called
  without a store, so treat the cache as an optimisation, never a
  requirement.

``Reading`` conventions
-----------------------
``source``
    One of :data:`SOURCES`.
``metric``
    Free-form, lowercase snake_case.  Established names:

    ==============  ======================================================
    ``level``       ČHMÚ / HoubyMapa growth level, 1..5 (5 = best)
    ``score``       HoubyMapa ``s``, 0.0..1.0
    ``sra_mm``      daily precipitation total of a ČHMÚ station, mm
    ``precip_mm``   daily precipitation total of a *model* (Open-Meteo), mm
    ``api30_mm``    API30 antecedent precipitation index, mm
    ``t_mean``      daily mean temperature, °C
    ``t_min``       daily minimum temperature, °C
    ``t_max``       daily maximum temperature, °C
    ``rh``          relative humidity, %
    ``t_soil_5``    soil temperature at 5 cm, °C (likewise ``_10``/``_20``)
    ``soil_moist``  volumetric soil moisture, 0..1
    ==============  ======================================================

    Adding a metric is free; reusing an existing name with different units
    is not.  Values are always ``float``.

    ``sra_mm`` and ``precip_mm`` are deliberately different names for
    "rain on day D", because the two are different quantities:

    * ``sra_mm`` (station) covers the **climatological day**
      ``[D 06:00 UTC, D+1 06:00 UTC)`` -- the gauge is read at 06 UTC and
      ČHMÚ books the total on the day the interval *starts*.  It is also
      what ``api30_mm`` is built from: ``API30(D)`` never uses ``SRA(D)``.
    * ``precip_mm`` (Open-Meteo) is a **calendar day** in Europe/Prague,
      i.e. ``[D 00:00, D+1 00:00)`` local time.

    The two windows overlap by 18 h, and ``api30.extend_series`` splices
    them day-onto-day without a shift -- an approximation that is much
    smaller than the model's own wet bias (see ``fetch_openmeteo``).
``date``
    The day the value *describes*.  For a forecast that is the **target**
    day, and ``meta["issued"]`` is the ISO date the forecast was issued on.
    Observations leave ``meta["issued"]`` unset.
``text``
    Short human-readable label for the value ("ne 6. 9.", "vysoká", the
    HoubyMapa explanation).  Goes into the Telegram message; keep it short.
``meta``
    Anything else, JSON-serialisable.  Reserved keys:
    ``issued`` (ISO date, marks a forecast), ``stale`` (bool, the upstream
    payload is older than we would like), ``distance_km``, ``px``, ``cell``.

Identity / upsert key
---------------------
``(source, location, date, metric, meta["issued"] or "")``.  Re-running
``check`` twice on the same day overwrites in place, it does not duplicate.

``rules.py`` protocol
---------------------
``rules`` is optional; the CLI degrades to a plain snapshot when it is
missing.  When present it MUST expose::

    def decide(locations: list[Location], results: list[FetchResult], *,
               store: Store, today: date) -> Decision

and, like a fetcher, should not raise -- the CLI treats an exception from
``decide`` as exit code 1.  ``Decision.exit_code`` follows the Hermes
contract of PLAN §2: ``0`` stay silent, ``10`` positive signal (stdout is
forwarded to Telegram verbatim), ``1`` error.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

__all__ = [
    "SOURCES",
    "EXIT_SILENT",
    "EXIT_SIGNAL",
    "EXIT_ERROR",
    "DataQuality",
    "SeriesPoint",
    "WindowAggregate",
    "Location",
    "Reading",
    "FetchResult",
    "Decision",
    "Fetcher",
    "utcnow",
    "soft_fetch",
    "haversine_km",
]

#: Every source id that may appear in ``Reading.source``.
SOURCES: tuple[str, ...] = (
    "chmi_map",
    "houbymapa",
    "chmi_station",
    "openmeteo",
    "api30_forecast",
)

#: Exit codes of ``python -m mushroom_alerts check`` (PLAN §2).
EXIT_SILENT = 0
EXIT_ERROR = 1
EXIT_SIGNAL = 10


def utcnow() -> datetime:
    """Timezone-aware UTC now.  Use this instead of ``datetime.utcnow()``."""
    return datetime.now(timezone.utc)


class DataQuality(str, Enum):
    """Usability of a value for a current decision."""

    FRESH = "fresh"
    PARTIAL = "partial"
    STALE = "stale"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One normalized point used by window calculations and views."""

    date: date
    value: float | None
    source: str
    quality: DataQuality
    valid_at: datetime | None = None
    issued_at: datetime | None = None
    covered: int | None = None
    expected: int | None = None


@dataclass(frozen=True, slots=True)
class WindowAggregate:
    """Calendar-window result; a partial total is a lower bound."""

    start: date
    end: date
    total: float | None
    mean: float | None
    covered_days: int
    expected_days: int
    quality: DataQuality
    lower_bound: bool

    @property
    def complete(self) -> bool:
        return self.covered_days == self.expected_days and self.quality is DataQuality.FRESH

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "total": self.total,
            "mean": self.mean,
            "covered_days": self.covered_days,
            "expected_days": self.expected_days,
            "quality": self.quality.value,
            "lower_bound": self.lower_bound,
        }


@dataclass(frozen=True, slots=True)
class Location:
    """A place we watch.  ``slug`` is the stable key used everywhere else."""

    name: str
    lat: float
    lon: float
    slug: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lat": self.lat, "lon": self.lon, "slug": self.slug}


@dataclass(slots=True)
class Reading:
    """One value, one location, one day, one metric.  See module docstring."""

    source: str
    location: str  # Location.slug
    date: date
    metric: str
    value: float
    text: str | None = None
    meta: dict[str, Any] | None = None

    # -- helpers ---------------------------------------------------------
    @property
    def issued(self) -> str:
        """ISO issue date for a forecast, or ``""`` for an observation."""
        if not self.meta:
            return ""
        return str(self.meta.get("issued") or "")

    @property
    def is_forecast(self) -> bool:
        return bool(self.issued)

    def meta_json(self) -> str | None:
        if self.meta is None:
            return None
        return json.dumps(self.meta, ensure_ascii=False, sort_keys=True, default=str)

    def key(self) -> tuple[str, str, str, str, str]:
        """The upsert identity of this reading."""
        return (self.source, self.location, self.date.isoformat(), self.metric, self.issued)


@dataclass(slots=True)
class FetchResult:
    """What one source produced in one run.

    ``ok=False`` means "this source gave us nothing usable"; the CLI notes it
    in the report and carries on.  ``ok=True`` with a non-empty ``error`` is
    a partial success (e.g. three locations out of four).
    """

    source: str
    ok: bool
    readings: list[Reading] = field(default_factory=list)
    error: str | None = None
    fetched_at: datetime = field(default_factory=utcnow)
    location_errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def failure(
        cls, source: str, error: str, location_errors: Mapping[str, str] | None = None
    ) -> "FetchResult":
        return cls(
            source=source,
            ok=False,
            readings=[],
            error=error,
            location_errors=dict(location_errors or {}),
        )

    @classmethod
    def success(
        cls,
        source: str,
        readings: list[Reading],
        error: str | None = None,
        location_errors: Mapping[str, str] | None = None,
    ) -> "FetchResult":
        return cls(
            source=source,
            ok=True,
            readings=list(readings),
            error=error,
            location_errors=dict(location_errors or {}),
        )

    def for_location(self, slug: str) -> list[Reading]:
        return [r for r in self.readings if r.location == slug]


@dataclass(slots=True)
class Decision:
    """What ``rules.decide`` returns: the exit code and the text to send."""

    exit_code: int = EXIT_SILENT
    text: str = ""
    data: dict[str, Any] | None = None


class Fetcher(Protocol):
    """Structural type of a ``fetch_*`` module."""

    SOURCE: str

    def fetch(
        self, locations: list[Location], *, http: Any, today: date
    ) -> FetchResult:  # pragma: no cover - protocol
        ...


def soft_fetch(
    source: str,
    fn: Callable[..., FetchResult],
    locations: list[Location],
    *,
    http: Any,
    today: date,
) -> FetchResult:
    """Call ``fn`` and turn any escaping exception into ``ok=False``.

    Belt and braces: fetchers promise not to raise, this makes sure of it.
    """
    try:
        result = fn(locations, http=http, today=today)
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all, PLAN §6
        detail = traceback.format_exception_only(type(exc), exc)[-1].strip()
        return FetchResult.failure(source, detail)
    if not isinstance(result, FetchResult):
        return FetchResult.failure(source, f"fetch() returned {type(result).__name__}, not FetchResult")
    return result


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km.  Shared by the grid/station fetchers."""
    from math import asin, cos, radians, sin, sqrt

    r = 6371.0088
    p1, p2 = radians(lat1), radians(lat2)
    dp = p2 - p1
    dl = radians(lon2 - lon1)
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * r * asin(sqrt(h))
