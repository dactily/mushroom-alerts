"""HoubyMapa.cz prediction grid -> nearest cell per location.

``GET https://houbymapa.cz/predikce`` returns one JSON document with the
whole 0.2 deg grid (442 cells over Czechia as of 2026-09)::

    {"updated": 1788726639, "model": "v3.0", "step": 0.2,
     "bbox": [48.55, 12.09, 51.06, 18.87],
     "legend": [{"label": "velmi nízká", "color": "#d93629"}, ...],
     "radar": {...}, "ok": true, "attribution": "...",
     "cells": [{"lat": 49.55, "lng": 17.89, "s": 0.47, "l": 3,
                "w": "sušeji než obvykle, ...", "sm": 0.176,
                "st": 18.2, "sf": 0.52}, ...]}

Correction to PLAN §1: ``updated`` is a **Unix timestamp**, not the string
``"2026-09-06 22:30"``.  It is converted to a Europe/Prague date here and
that date is what the readings are stamped with -- the grid is refreshed
once each evening, so this gives exactly one reading per location per day
and a stalled upstream produces no phantom "new day" (see also
``fetch_chmi_map``).

Field meanings (from the site's own legend): ``s`` score 0..1, ``l`` level
1..5, ``w`` a Czech explanation string, ``sm`` soil moisture, ``st`` soil
temperature °C, ``sf`` a seasonal factor.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable

from .base import FetchResult, Location, Reading, haversine_km

__all__ = ["SOURCE", "URL", "fetch", "nearest_cell", "cells_from_payload", "updated_date"]

SOURCE = "houbymapa"
URL = "https://houbymapa.cz/predikce"

#: Sanity limit: the grid step is 0.2 deg (~15x22 km), so the nearest cell
#: centre is never further than ~14 km from a point inside Czechia.
MAX_CELL_DISTANCE_KM = 40.0

STALE_AFTER_DAYS = 2

try:  # stdlib since 3.9, but tzdata may be missing on some Linux images
    from zoneinfo import ZoneInfo

    _PRAGUE: Any = ZoneInfo("Europe/Prague")
except Exception:  # pragma: no cover - fall back to UTC
    _PRAGUE = timezone.utc


def cells_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Validate the payload and return its cell list."""
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    cells = payload.get("cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("payload has no 'cells'")
    return [c for c in cells if isinstance(c, dict) and "lat" in c and "lng" in c]


def nearest_cell(
    cells: list[dict[str, Any]], lat: float, lon: float
) -> tuple[dict[str, Any], float]:
    """Closest grid cell by haversine.  Returns ``(cell, distance_km)``."""
    if not cells:
        raise ValueError("no cells")
    best = min(cells, key=lambda c: haversine_km(lat, lon, float(c["lat"]), float(c["lng"])))
    return best, haversine_km(lat, lon, float(best["lat"]), float(best["lng"]))


def updated_date(payload: dict[str, Any], today: date) -> date:
    """Europe/Prague date of ``updated``; ``today`` if it is unusable."""
    raw = payload.get("updated")
    if isinstance(raw, (int, float)) and raw > 0:
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).astimezone(_PRAGUE).date()
        except (OverflowError, OSError, ValueError):
            return today
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            return today
    return today


def fetch(locations: Iterable[Location], *, http: Any, today: date) -> FetchResult:
    """One request for the whole grid, then the nearest cell per location."""
    locs = list(locations)
    try:
        payload = http.get_json(URL)
        cells = cells_from_payload(payload)
    except Exception as exc:  # noqa: BLE001 - soft failure, PLAN §6
        return FetchResult.failure(SOURCE, f"{type(exc).__name__}: {exc}")

    day = updated_date(payload, today)
    stale = (today - day).days > STALE_AFTER_DAYS
    model = payload.get("model")

    readings: list[Reading] = []
    problems: list[str] = []
    for loc in locs:
        try:
            cell, dist = nearest_cell(cells, loc.lat, loc.lon)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{loc.slug}: {type(exc).__name__}")
            continue
        if dist > MAX_CELL_DISTANCE_KM:
            problems.append(f"{loc.slug}: nearest cell {dist:.0f} km away")
            continue
        word = cell.get("w")
        text = word if isinstance(word, str) and word.strip() else None
        meta: dict[str, Any] = {
            "updated": payload.get("updated"),
            "updated_date": day.isoformat(),
            "model": model,
            "cell": [cell.get("lat"), cell.get("lng")],
            "distance_km": round(dist, 2),
            "sm": cell.get("sm"),
            "st": cell.get("st"),
            "sf": cell.get("sf"),
        }
        if stale:
            meta["stale"] = True
        for metric, key in (("level", "l"), ("score", "s")):
            value = cell.get(key)
            if not isinstance(value, (int, float)):
                problems.append(f"{loc.slug}: cell has no '{key}'")
                continue
            readings.append(
                Reading(
                    source=SOURCE,
                    location=loc.slug,
                    date=day,
                    metric=metric,
                    value=float(value),
                    text=text,
                    meta=dict(meta),
                )
            )

    error = "; ".join(problems) or None
    if not readings:
        return FetchResult.failure(SOURCE, error or "no locations resolved")
    return FetchResult.success(SOURCE, readings, error)


def params_for(
    location: Location, cells: list[dict[str, Any]]
) -> dict[str, Any]:
    """Derived params worth caching per PLAN §2a (``store.set_params``)."""
    cell, dist = nearest_cell(cells, location.lat, location.lon)
    return {
        "houbymapa_cell": [cell.get("lat"), cell.get("lng")],
        "houbymapa_distance_km": round(dist, 2),
    }
