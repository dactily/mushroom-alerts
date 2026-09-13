#!/usr/bin/env python3
"""Sample the whole country against the ČHMÚ map, to calibrate the chance.

**This is a one-off tool, run by hand.**  Nothing on the daily path may
import it.  It exists because the chance of PLAN §9b was never fitted
against anything: every constant in ``policy`` was argued from first
principles on thirteen locations that all share one weather regime.  To
argue about the shape of the model one needs points that disagree, and the
only cheap source of "somebody else's opinion about this forest today" is
the ČHMÚ map -- one level 1..5 per pixel, for the whole country.

    .venv/bin/python tools/sample_chmu.py collect --reference --home
    .venv/bin/python tools/sample_chmu.py score --rows

``collect`` does three things:

1. **Picks the points.**  One request gets the ČHMÚ raster; it is read on a
   :data:`GRID_STEP_DEG` lat/lon grid, the grid cells are grouped into
   connected patches of equal level, and each patch is eroded so points are
   taken from its *interior* -- a point on a boundary pixel would be
   compared against a level its own neighbourhood does not really have.
   Levels are sampled with equal quotas (a level that cannot fill its quota
   hands the rest back), so the rare dry corner of the country weighs as
   much in the fit as the wet mountains that cover half of it, and no two
   points end up within :data:`MIN_SEPARATION_KM` of each other.
2. **Runs the real pipeline** over those points: the same fetchers, the
   same store, the same ``rules.derive``, the same ``views``.  Nothing here
   reimplements the model -- a calibration harness that computes its own
   API30 would be calibrating something we do not ship.
3. **Writes the raw rows to JSON**, one file per run, so a refit never
   refetches.  The cache carries the station series and the forecast
   curves, not only the finished percentage, because changing the model
   means re-deriving the rain episodes from the same observations.

``score`` reads that JSON back, recomputes the chance for every row with
whatever ``policy`` currently says, and reports Spearman, Pearson and the
mean absolute error against :data:`CHMI_ANCHORS`.  Run it before and after
editing ``policy`` and the two numbers are comparable by construction: same
points, same observations, same day.

Politeness
----------
One request for the map, one for the HoubyMapa grid, one Open-Meteo call
per point, and the station fetcher is called **once** with every point at
the same time so its per-run download cache is actually used (neighbouring
points share stations).  ``--ten-minute`` re-enables the optional 10-minute
provisional rain, which costs roughly two more requests per distinct
station and is off here by default.  Every run prints the request count it
actually made.

The run writes to its own SQLite file (``--db``, default next to the cache)
and never touches ``state.sqlite``: sixty throwaway points have no business
in the history of the thirteen watched forests.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # run me without installing the package
    sys.path.insert(0, str(REPO_ROOT))

from mushroom_alerts import biology  # noqa: E402
from mushroom_alerts import chance as chance_lib  # noqa: E402
from mushroom_alerts import fetch_chmi_map as chmi_map  # noqa: E402
from mushroom_alerts import fetch_chmi_station as chmi_station  # noqa: E402
from mushroom_alerts import policy, views  # noqa: E402
from mushroom_alerts import rules as rules_lib  # noqa: E402
from mushroom_alerts.__main__ import run_pipeline  # noqa: E402
from mushroom_alerts.base import DataQuality, Location, SeriesPoint, haversine_km  # noqa: E402
from mushroom_alerts.http import Http  # noqa: E402
from mushroom_alerts.locations import load_locations, slugify  # noqa: E402
from mushroom_alerts.store import Store  # noqa: E402

# -- the sample ----------------------------------------------------------

#: Scan box in WGS84 degrees, a little larger than Czechia; anything
#: outside the country reads as transparent and simply never votes.
COUNTRY = (48.53, 12.06, 51.08, 18.90)  # lat_min, lon_min, lat_max, lon_max

#: Grid of the scan.  0.05° is ~5.6 km north-south and ~3.6 km east-west at
#: 50°N: fine enough that a patch of a few hundred km² still has an
#: interior, coarse enough to scan the country in a second.
GRID_STEP_DEG = 0.05

#: No two sampled points closer than this.  Two points 5 km apart share
#: their ČHMÚ patch *and* usually their station, so the second one adds a
#: duplicate row to the fit and one more request to the servers.
MIN_SEPARATION_KM = 8.0

DEFAULT_POINTS = 60

#: How deep inside its patch a point must sit, in grid cells.  1 means "no
#: neighbour of another level", which is what "interior" has to mean at
#: this resolution: demanding 2 empties every narrow patch.
MIN_PATCH_DEPTH = 1

#: The calibration target (PLAN §9b): ČHMÚ level -> the percentage a place
#: of that level should score on average.  Not a probability of finding
#: mushrooms and not a claim that ČHMÚ is right -- an anchor that makes two
#: models comparable on one axis.
CHMI_ANCHORS = {1: 10.0, 2: 25.0, 3: 45.0, 4: 65.0, 5: 85.0}

#: The points the calibration was argued over: the eight strongest ČHMÚ
#: zones of northern Czechia on 2026-09-13 (centres of the largest level
#: 4/5 patches), plus the two towns the sanity checks name.  Committed so
#: the before/after table can be reproduced, not because the model knows
#: anything about them -- ``--reference`` adds them to the sample.
REFERENCE_POINTS: tuple[tuple[str, float, float], ...] = (
    ("Jizerky", 50.730, 15.490),
    ("Krušné W", 50.380, 12.788),
    ("Orlické", 50.258, 16.418),
    ("Krušné E", 50.715, 13.767),
    ("Kralický", 50.161, 16.923),
    ("Broumov", 50.588, 16.098),
    ("Jeseníky", 50.199, 17.349),
    ("Slavkov", 50.060, 12.680),
    ("Liberec", 50.767, 15.056),
    ("Praha", 50.075, 14.437),
)

CACHE_DIR = REPO_ROOT / ".cache" / "chmu-sample"


# ----------------------------------------------------------------------
# the grid
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Cell:
    """One grid node of the ČHMÚ scan."""

    row: int
    col: int
    lat: float
    lon: float
    level: int


def scan(image, step: float = GRID_STEP_DEG) -> dict[tuple[int, int], Cell]:
    """Read the map on a lat/lon grid; cells with no colour are dropped."""
    lat_min, lon_min, lat_max, lon_max = COUNTRY
    rows = int(round((lat_max - lat_min) / step)) + 1
    cols = int(round((lon_max - lon_min) / step)) + 1
    out: dict[tuple[int, int], Cell] = {}
    for r in range(rows):
        lat = lat_max - r * step  # row 0 is north, like the raster
        for c in range(cols):
            lon = lon_min + c * step
            level, _, votes = chmi_map.level_at(image, lat, lon)
            if level is None or votes == 0:
                continue
            out[(r, c)] = Cell(r, c, round(lat, 4), round(lon, 4), level)
    return out


def _neighbours(key: tuple[int, int]) -> Iterable[tuple[int, int]]:
    r, c = key
    yield (r - 1, c)
    yield (r + 1, c)
    yield (r, c - 1)
    yield (r, c + 1)


def patches(cells: dict[tuple[int, int], Cell]) -> list[list[tuple[int, int]]]:
    """Connected components of equal level, largest first (4-neighbour)."""
    seen: set[tuple[int, int]] = set()
    found: list[list[tuple[int, int]]] = []
    for key in sorted(cells):
        if key in seen:
            continue
        level = cells[key].level
        component = [key]
        seen.add(key)
        queue = deque([key])
        while queue:
            for other in _neighbours(queue.popleft()):
                if other in seen or other not in cells:
                    continue
                if cells[other].level != level:
                    continue
                seen.add(other)
                component.append(other)
                queue.append(other)
        found.append(sorted(component))
    found.sort(key=lambda item: (-len(item), item[0]))
    return found


def depths(
    cells: dict[tuple[int, int], Cell], patch: Sequence[tuple[int, int]]
) -> dict[tuple[int, int], int]:
    """How deep inside its patch each cell sits, in grid steps.

    Depth 1 is a boundary cell (some 4-neighbour is missing or carries
    another level), depth 2 is a cell all of whose neighbours are boundary
    cells, and so on -- a plain multi-source BFS inwards from the edge.
    """
    members = set(patch)
    out: dict[tuple[int, int], int] = {}
    queue: deque[tuple[int, int]] = deque()
    for key in patch:
        if any(other not in members for other in _neighbours(key)):
            out[key] = 1
            queue.append(key)
    while queue:
        key = queue.popleft()
        for other in _neighbours(key):
            if other in members and other not in out:
                out[other] = out[key] + 1
                queue.append(other)
    # A patch with no edge at all cannot happen on a finite grid, but a
    # single-cell patch must still get a depth rather than vanish.
    for key in patch:
        out.setdefault(key, 1)
    return out


def choose_points(
    cells: dict[tuple[int, int], Cell],
    wanted: int = DEFAULT_POINTS,
    *,
    min_km: float = MIN_SEPARATION_KM,
    min_depth: int = MIN_PATCH_DEPTH,
) -> list[Cell]:
    """Stratified sample: equal quota per present level, patch interiors.

    Within a level the patches are visited round-robin, largest first, and
    inside a patch the deepest cells come first, so a quota is spread over
    the level's regions instead of landing in one valley.
    """
    by_level: dict[int, list[list[tuple[int, int]]]] = {}
    for patch in patches(cells):
        by_level.setdefault(cells[patch[0]].level, []).append(patch)

    ordered: dict[int, list[list[Cell]]] = {}
    for level, group in by_level.items():
        ranked: list[list[Cell]] = []
        for patch in group:
            depth = depths(cells, patch)
            inside = [k for k in patch if depth[k] >= min_depth]
            if not inside:
                continue
            inside.sort(key=lambda k: (-depth[k], k))
            ranked.append([cells[k] for k in inside])
        if ranked:
            ordered[level] = ranked

    levels = sorted(ordered)
    picked: list[Cell] = []

    def far_enough(cell: Cell) -> bool:
        return all(
            haversine_km(cell.lat, cell.lon, other.lat, other.lon) >= min_km
            for other in picked
        )

    def take(level: int, quota: int) -> int:
        """Take up to ``quota`` points of one level; return how many."""
        taken = 0
        cursors = [0] * len(ordered[level])
        while taken < quota:
            progressed = False
            for index, patch in enumerate(ordered[level]):
                if taken >= quota:
                    break
                while cursors[index] < len(patch):
                    cell = patch[cursors[index]]
                    cursors[index] += 1
                    if far_enough(cell):
                        picked.append(cell)
                        taken += 1
                        progressed = True
                        break
            if not progressed:
                break
        return taken

    # Equal quotas, then whatever a small level could not use is offered to
    # the others in order of how much of the country they cover.
    quota, extra = divmod(wanted, len(levels)) if levels else (0, 0)
    got = {level: take(level, quota + (1 if i < extra else 0))
           for i, level in enumerate(levels)}
    short = wanted - sum(got.values())
    if short > 0:
        for level in sorted(levels, key=lambda lv: -sum(len(p) for p in ordered[lv])):
            if short <= 0:
                break
            short -= take(level, short)
    picked.sort(key=lambda cell: (cell.level, cell.lat, cell.lon))
    return picked


# ----------------------------------------------------------------------
# the points
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Point:
    """One place to measure, and where it came from."""

    name: str
    slug: str
    lat: float
    lon: float
    group: str
    grid_level: int | None = None

    def as_location(self) -> Location:
        return Location(name=self.name, lat=self.lat, lon=self.lon, slug=self.slug)


def _unique(slug: str, taken: set[str]) -> str:
    base, n = slug, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    taken.add(slug)
    return slug


def build_points(
    sample: Sequence[Cell],
    *,
    reference: bool,
    home: bool,
    min_km: float = MIN_SEPARATION_KM,
) -> list[Point]:
    """Sampled cells first, then the fixed points that are not too close."""
    taken: set[str] = set()
    points: list[Point] = []
    for cell in sample:
        slug = _unique(f"s{cell.level}-{cell.lat:.2f}-{cell.lon:.2f}".replace(".", ""), taken)
        points.append(
            Point(
                name=f"L{cell.level} {cell.lat:.3f},{cell.lon:.3f}",
                slug=slug,
                lat=cell.lat,
                lon=cell.lon,
                group="sample",
                grid_level=cell.level,
            )
        )
    fixed: list[tuple[str, str, float, float, str]] = []
    if reference:
        fixed += [("reference", n, la, lo, slugify(n)) for n, la, lo in REFERENCE_POINTS]
    if home:
        fixed += [("home", lc.name, lc.lat, lc.lon, lc.slug) for lc in load_locations()]
    for group, name, lat, lon, slug in fixed:
        # A fixed point that lands on top of a sampled one would be fetched
        # twice and counted twice in the fit; the named one wins.
        points = [
            p
            for p in points
            if p.group != "sample" or haversine_km(p.lat, p.lon, lat, lon) >= min_km
        ]
        points.append(
            Point(name=name, slug=_unique(slug, taken), lat=lat, lon=lon, group=group)
        )
    return points


# ----------------------------------------------------------------------
# collection
# ----------------------------------------------------------------------
class CountingHttp(Http):
    """``Http`` that says how many requests a run actually made."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.requests = 0

    def request(self, method: str, url: str, **kw: Any):  # type: ignore[override]
        self.requests += 1
        return super().request(method, url, **kw)


def _points_payload(points: dict[date, SeriesPoint]) -> dict[str, Any]:
    """A ``SeriesPoint`` series reduced to what a refit needs: value+quality."""
    return {
        day.isoformat(): [point.value, point.quality.value]
        for day, point in sorted(points.items())
        if point.value is not None
    }


def _series_payload(values: dict[date, float]) -> dict[str, float]:
    return {day.isoformat(): float(value) for day, value in sorted(values.items())}


def row_for(store: Store, point: Point, today: date) -> dict[str, Any]:
    """Everything the chance reads for one point, plus what produced it."""
    snap = views.location_snapshot(store, point.as_location(), today, history_days=45)
    bio = snap.get("biological") or {}
    forecast = snap.get("forecast") or {}
    station = snap.get("station") or {}
    status = snap.get("source_status") or {}
    series = station.get("series_points") or {}

    chance_api30, chance_quality = views.chance_moisture(
        forecast, snap.get("station"), status.get(views.STATION) or {}, today
    )

    def fresh_map(block: dict[str, Any] | None, source: str) -> bool:
        return bool(
            block
            and block.get("level") is not None
            and (status.get(source) or {}).get("quality") == DataQuality.FRESH.value
        )

    chmi = snap.get("chmi") or {}
    houby = snap.get("houbymapa") or {}
    measured = store.series(
        views.STATION,
        point.slug,
        "api30_mm",
        since=today - timedelta(days=45),
        until=today,
    )
    return {
        "name": point.name,
        "slug": point.slug,
        "lat": point.lat,
        "lon": point.lon,
        "group": point.group,
        "grid_level": point.grid_level,
        # -- the two published opinions
        "chmi_level": None if not chmi else float(chmi["level"]),
        "chmi_quality": (status.get(views.CHMI_MAP) or {}).get("quality"),
        "chmi_fresh_level": float(chmi["level"]) if fresh_map(chmi, views.CHMI_MAP) else None,
        "houbymapa_score": houby.get("score"),
        "houbymapa_level": houby.get("level"),
        "houbymapa_quality": (status.get(views.HOUBYMAPA) or {}).get("quality"),
        "houbymapa_fresh_score": (
            float(houby["score"])
            if houby.get("score") is not None
            and (status.get(views.HOUBYMAPA) or {}).get("quality")
            == DataQuality.FRESH.value
            else None
        ),
        # -- the station
        "station": station.get("station"),
        "station_quality": (status.get(views.STATION) or {}).get("quality"),
        "station_api30_mm": station.get("api30_mm"),
        "station_api30_date": (
            None if station.get("api30_date") is None else station["api30_date"].isoformat()
        ),
        "station_available": (status.get(views.STATION) or {}).get("quality")
        in {DataQuality.FRESH.value, DataQuality.PARTIAL.value},
        "sra_points": _points_payload(series.get("sra_mm") or {}),
        "t_mean_points": _points_payload(series.get("t_mean") or {}),
        "measured_api30": {
            reading.date.isoformat(): float(reading.value) for reading in measured
        },
        # -- the forecast side
        "api30_curve": _series_payload(dict(forecast.get("curve") or [])),
        "api30_quality_by_date": {
            day.isoformat(): value
            for day, value in sorted((forecast.get("api30_quality_by_date") or {}).items())
        },
        "api30_run_quality": forecast.get("api30_run_quality"),
        "openmeteo_quality": forecast.get("openmeteo_quality"),
        "precip_mm": _series_payload(forecast.get("rain") or {}),
        "t_mean": _series_payload(forecast.get("t_mean") or {}),
        "t_min": _series_payload(forecast.get("t_min") or {}),
        # -- what the chance was actually handed
        "chance_api30": _series_payload(chance_api30),
        "chance_api30_quality": {
            day.isoformat(): quality.value if isinstance(quality, DataQuality) else str(quality)
            for day, quality in sorted(chance_quality.items())
        },
        "frost_present": bool((bio.get("frost") or {}).get("present")),
        "history_sufficient": bool((bio.get("history") or {}).get("sufficient")),
        "episodes": [
            {
                "start": ep["start"].isoformat(),
                "end": ep["end"].isoformat(),
                "anchor": ep["date"].isoformat(),
                "total_mm": ep["total_mm"],
            }
            for ep in (bio.get("rain_episodes") or [])
        ],
        "phase": ((bio.get("guidance") or {}).get("phase")),
        "verdict": ((bio.get("guidance") or {}).get("verdict")),
    }


def collect(
    points: Sequence[Point],
    *,
    today: date,
    db: Path,
    ten_minute: bool,
) -> dict[str, Any]:
    """Run the real pipeline over every point and return the cache payload."""
    chmi_station.TEN_MINUTE = ten_minute
    locations = [point.as_location() for point in points]
    started = time.time()
    with CountingHttp() as http, Store(db) as store:
        run = run_pipeline(locations, store=store, http=http, today=today)
        if run is None:
            raise SystemExit("no fetchers available")
        rules_lib.derive(locations, run.results, store=store, today=today)
        rows = [row_for(store, point, today) for point in points]
        requests = http.requests
    return {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "today": today.isoformat(),
        "seconds": round(time.time() - started, 1),
        "requests": requests,
        "ten_minute": ten_minute,
        "rules_version": policy.RULES_VERSION,
        "notes": run.notes,
        "rows": rows,
    }


# ----------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------
def _points_from_payload(payload: dict[str, Any], source: str) -> dict[date, SeriesPoint]:
    out: dict[date, SeriesPoint] = {}
    for iso, (value, quality) in payload.items():
        day = date.fromisoformat(iso)
        out[day] = SeriesPoint(
            date=day,
            value=None if value is None else float(value),
            source=source,
            quality=DataQuality(quality),
        )
    return out


def _dates(payload: dict[str, Any]) -> dict[date, Any]:
    return {date.fromisoformat(iso): value for iso, value in payload.items()}


def chance_of(row: dict[str, Any], today: date) -> chance_lib.ChanceOutlook:
    """Recompute the chance for one cached row with the current ``policy``.

    The rain episodes are re-derived rather than replayed: changing
    ``RAIN_EPISODE_MM`` must move this number, otherwise the refit would be
    measuring yesterday's model.  The wiring mirrors
    ``views.location_snapshot`` -- same series, same anchors, same freshness
    rules -- so a number here and a number from a live run of the same day
    are the same number.
    """
    sra = _points_from_payload(row["sra_points"], views.STATION)
    temp = _points_from_payload(row["t_mean_points"], views.STATION)
    episodes = biology.detect_rain_episodes(row["slug"], sra, temp, today)
    api30 = {day: float(mm) for day, mm in _dates(row["chance_api30"]).items()}
    quality = {
        day: DataQuality(q) for day, q in _dates(row["chance_api30_quality"]).items()
    }
    return chance_lib.assess_chance_horizon(
        today,
        [episode.anchor for episode in episodes],
        sorted(api30),
        api30=api30,
        api30_quality=quality,
        t_mean={day: float(v) for day, v in _dates(row["t_mean"]).items()},
        t_min={day: float(v) for day, v in _dates(row["t_min"]).items()},
        frost_present=bool(row["frost_present"]),
        chmi_level=row["chmi_fresh_level"],
        houbymapa_score=row["houbymapa_fresh_score"],
        station_available=bool(row["station_available"]),
    )


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    return pearson(_rank(xs), _rank(ys))


@dataclass
class Scored:
    """One row after re-running the model on it."""

    row: dict[str, Any]
    value: int | None
    curve: dict[date, int | None] = field(default_factory=dict)

    @property
    def level(self) -> float | None:
        return self.row.get("chmi_level")

    @property
    def anchor(self) -> float | None:
        level = self.level
        return None if level is None else CHMI_ANCHORS.get(int(round(level)))


def score_rows(payload: dict[str, Any], groups: Sequence[str] = ()) -> list[Scored]:
    today = date.fromisoformat(payload["today"])
    out: list[Scored] = []
    for row in payload["rows"]:
        if groups and row["group"] not in groups:
            continue
        outlook = chance_of(row, today)
        out.append(
            Scored(
                row=row,
                value=outlook.current.value,
                curve={item.date: item.value for item in outlook.outlook},
            )
        )
    return out


def statistics(scored: Sequence[Scored]) -> dict[str, Any]:
    """Correlation and error against :data:`CHMI_ANCHORS`, plus coverage."""
    usable = [s for s in scored if s.value is not None and s.anchor is not None]
    levels = [float(s.level or 0) for s in usable]
    values = [float(s.value or 0) for s in usable]
    errors = [abs(v - float(s.anchor or 0)) for s, v in zip(usable, values)]
    biases = [v - float(s.anchor or 0) for s, v in zip(usable, values)]
    per_level: dict[int, list[float]] = {}
    for s, v in zip(usable, values):
        per_level.setdefault(int(round(s.level or 0)), []).append(v)
    return {
        "n": len(scored),
        "n_scored": len(usable),
        "n_no_number": sum(1 for s in scored if s.value is None),
        "spearman": spearman(levels, values),
        "pearson": pearson(levels, values),
        "mae": (sum(errors) / len(errors)) if errors else None,
        "bias": (sum(biases) / len(biases)) if biases else None,
        "per_level": {
            level: {
                "n": len(items),
                "mean": round(sum(items) / len(items), 1),
                "min": min(items),
                "max": max(items),
                "anchor": CHMI_ANCHORS.get(level),
            }
            for level, items in sorted(per_level.items())
        },
    }


def _fmt(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def print_statistics(stats: dict[str, Any]) -> None:
    print(
        f"n={stats['n']} scored={stats['n_scored']} no-number={stats['n_no_number']}  "
        f"Spearman={_fmt(stats['spearman'])}  Pearson={_fmt(stats['pearson'])}  "
        f"MAE={_fmt(stats['mae'], 1)}  bias={_fmt(stats['bias'], 1)}"
    )
    print(f"{'ČHMÚ':>5} {'n':>3} {'anchor':>7} {'mean':>6} {'min':>5} {'max':>5}")
    for level, item in stats["per_level"].items():
        print(
            f"{level:>5} {item['n']:>3} {item['anchor']:>7} "
            f"{item['mean']:>6} {item['min']:>5.0f} {item['max']:>5.0f}"
        )


def _cell(value: Any, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}" if digits else f"{float(value):.0f}"


def print_rows(scored: Sequence[Scored]) -> None:
    header = (
        f"{'point':<26} {'grp':<5} {'ČHMÚ':>4} {'HM':>5} {'API30':>6} "
        f"{'ep':>3} {'phase':>9} {'chance':>6} {'peak':>5}"
    )
    print(header)
    print("-" * len(header))
    for item in sorted(scored, key=lambda s: (-(s.level or 0), s.row["name"])):
        row = item.row
        peak = max((v for v in item.curve.values() if v is not None), default=None)
        print(
            f"{row['name'][:26]:<26} {row['group'][:5]:<5} "
            f"{_cell(row['chmi_level']):>4} "
            f"{_cell(row['houbymapa_score'], 2):>5} "
            f"{_cell(row['station_api30_mm']):>6} "
            f"{len(row['episodes']):>3} "
            f"{(row['phase'] or '—')[:9]:>9} "
            f"{_cell(item.value):>6} "
            f"{_cell(peak):>5}"
        )


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def cmd_collect(args: argparse.Namespace) -> int:
    today = date.today() if args.today is None else date.fromisoformat(args.today)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with CountingHttp() as http:
        payload = http.get_json(chmi_map.URL)
        image = chmi_map.decode_image(payload)
        map_requests = http.requests
    label = payload.get("timeLabel")
    print(f"ČHMÚ map {label!r} {image.size[0]}x{image.size[1]} ({map_requests} request)")

    cells = scan(image)
    present = sorted({cell.level for cell in cells.values()})
    print(f"grid {GRID_STEP_DEG}° -> {len(cells)} cells, levels present: {present}")

    sample = choose_points(cells, args.points)
    points = build_points(sample, reference=args.reference, home=args.home)
    counted: dict[Any, int] = {}
    for cell in sample:
        counted[cell.level] = counted.get(cell.level, 0) + 1
    print(f"sampled {len(sample)} points {counted}, {len(points)} points in total")

    db = Path(args.db) if args.db else out_dir / f"sample-{today.isoformat()}.sqlite"
    data = collect(points, today=today, db=db, ten_minute=args.ten_minute)
    data["map_label"] = label
    data["levels_present"] = present
    data["grid_cells"] = len(cells)
    data["requests"] += map_requests

    path = out_dir / f"rows-{today.isoformat()}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    no_station = [r["name"] for r in data["rows"] if not r["station_available"]]
    print(
        f"wrote {path} -- {len(data['rows'])} rows, {data['requests']} requests, "
        f"{data['seconds']} s; {len(no_station)} without a usable station"
    )
    if no_station:
        print("  no station: " + ", ".join(no_station))
    for note in data["notes"]:
        print(f"  note: {note}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.rows_file).read_text(encoding="utf-8"))
    scored = score_rows(payload, args.group)
    stats = statistics(scored)
    print(f"{args.rows_file}  rules={payload.get('rules_version')}  today={payload['today']}")
    print_statistics(stats)
    if args.rows:
        print()
        print_rows(scored)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    collect_cmd = sub.add_parser("collect", help="pick points, fetch, cache rows")
    collect_cmd.add_argument("--points", type=int, default=DEFAULT_POINTS)
    collect_cmd.add_argument("--out", default=str(CACHE_DIR))
    collect_cmd.add_argument("--db", default=None, help="SQLite file for this run")
    collect_cmd.add_argument("--today", default=None)
    collect_cmd.add_argument("--reference", action="store_true", help="add REFERENCE_POINTS")
    collect_cmd.add_argument("--home", action="store_true", help="add locations.yaml")
    collect_cmd.add_argument(
        "--ten-minute",
        action="store_true",
        help="also fetch the optional 10-minute provisional rain (~2 requests/station)",
    )
    collect_cmd.set_defaults(func=cmd_collect)

    score_cmd = sub.add_parser("score", help="re-run the model on a cached file")
    score_cmd.add_argument("rows_file")
    score_cmd.add_argument("--rows", action="store_true", help="print every point")
    score_cmd.add_argument("--json", action="store_true")
    score_cmd.add_argument(
        "--group", action="append", default=[], help="sample|reference|home"
    )
    score_cmd.set_defaults(func=cmd_score)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
