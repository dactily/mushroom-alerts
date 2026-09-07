"""ČHMÚ "Pravděpodobnost růstu hub" raster -> level 1..5 at a point.

``GET https://data-provider.chmi.cz/api/data/rizika/houby`` returns
``{"timeLabel": "ne 6. 9.", "img": "data:image/png;base64,..."}``.  The PNG
(2755x1600 as of 2026-09) is an indexed-colour overlay of Czechia on a
transparent background, one flat colour per growth level.

Projection -- correction to PLAN §1
-----------------------------------
PLAN says "bbox EPSG:4326 = 12.0424913, 48.403695, 19.1177213, 51.0596505"
and that lat/lon maps to a pixel by that bbox.  The bbox corners are indeed
WGS84 degrees, but the raster itself is **Web Mercator (EPSG:3857)**, not
plate carrée.  Evidence, all on the live 2026-09-07 image:

* aspect ratio: lon span / lat span = 2.6639, but the image is 2755/1600 =
  1.7219 -- and the Mercator-projected span ratio is 1.7213 (0.03% off);
* the westernmost point of Czechia (12.0906E, 50.2521N) sits in the opaque
  mask at rows 495-499; Mercator predicts 495.8, plate carrée 486.5;
* the easternmost point (18.8592E, 49.5511N) sits at rows 919-927;
  Mercator predicts 919.5, plate carrée 908.8.

Using plate carrée would shift a point near Valašské Meziříčí ~10 px (~1.6
km) north.  ``pixel_for`` therefore does the Mercator transform; longitude
is linear either way.

Palette -- how the RGBs were derived
------------------------------------
The live PNG contains exactly one flat colour per level plus full
transparency; ``Image.getcolors()`` on 2026-09-07 returned five entries:
``(255,255,255,0)`` transparent, ``(255,255,191)``, ``(134,203,102)``,
``(248,141,81)``, ``(33,155,81)``.  There is no legend inside the image (it
is a bare overlay) and no ČHMÚ HTML page is reachable for it any more
(``chmi.cz/predpoved-pocasi/rizika/houby`` -> 404), so the level ordering
was pinned down two other ways:

1. HoubyMapa publishes its own legend using exactly the same five hex
   values: ``#d93629`` velmi nízká, ``#f88d51`` nízká, ``#ffffbf`` střední,
   ``#86cb66`` vysoká, ``#219b51`` velmi vysoká -- i.e. red -> green
   ascending, which is the ordering assumed here;
2. cross-validation against HoubyMapa's 442 numeric cell levels for the
   same day: sampling this image at every cell centre gives Pearson
   r = +0.51 (81.5% agree within one level).  The sign is the point: with
   the reversed ordering it would be -0.51.

``#d93629`` (level 1) did not occur on 2026-09-07 -- no part of the country
was that dry -- so it is carried over from the HoubyMapa legend and is the
one constant not yet confirmed against a live ČHMÚ pixel.
"""

from __future__ import annotations

import base64
import binascii
import io
import math
import re
from collections import Counter
from datetime import date
from typing import Any, Iterable

from .base import FetchResult, Location, Reading

__all__ = ["SOURCE", "URL", "BBOX", "PALETTE", "LEVEL_LABELS", "fetch", "pixel_for", "level_at", "decode_image", "parse_time_label"]

SOURCE = "chmi_map"
URL = "https://data-provider.chmi.cz/api/data/rizika/houby"

#: (min_lon, min_lat, max_lon, max_lat) in WGS84 degrees; raster is EPSG:3857.
BBOX = (12.0424913, 48.403695, 19.1177213, 51.0596505)

#: RGB -> level 1..5, ascending "probability of mushroom growth".
PALETTE: dict[tuple[int, int, int], int] = {
    (217, 54, 41): 1,   # #d93629 velmi nízká  (from the HoubyMapa legend)
    (248, 141, 81): 2,  # #f88d51 nízká
    (255, 255, 191): 3, # #ffffbf střední
    (134, 203, 102): 4, # #86cb66 vysoká
    (33, 155, 81): 5,   # #219b51 velmi vysoká
}

LEVEL_LABELS = {
    1: "очень низкая",
    2: "низкая",
    3: "средняя",
    4: "высокая",
    5: "очень высокая",
}

#: Side of the square sampling window, in pixels (5x5 majority vote).
WINDOW = 5

#: ``timeLabel`` older than this many days -> ``meta["stale"] = True``.
STALE_AFTER_DAYS = 2

_CZ_DAYS = ("po", "út", "st", "čt", "pá", "so", "ne")
_LABEL_RE = re.compile(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})?")


# ----------------------------------------------------------------------
# geometry
# ----------------------------------------------------------------------
def _merc_y(lat: float) -> float:
    lat = max(min(lat, 85.05), -85.05)
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def pixel_for(lat: float, lon: float, size: tuple[int, int]) -> tuple[int, int]:
    """Pixel (x, y) of a WGS84 point on the ČHMÚ raster of the given size.

    Read ``size`` from the decoded image; do not hardcode 2755x1600.  The
    result is clamped to the image, so a point outside Czechia yields an
    edge pixel (which will be transparent -> "no data").
    """
    width, height = size
    min_lon, min_lat, max_lon, max_lat = BBOX
    fx = (lon - min_lon) / (max_lon - min_lon)
    top, bottom = _merc_y(max_lat), _merc_y(min_lat)
    fy = (top - _merc_y(lat)) / (top - bottom)
    x = int(fx * width)
    y = int(fy * height)
    return (max(0, min(width - 1, x)), max(0, min(height - 1, y)))


# ----------------------------------------------------------------------
# image
# ----------------------------------------------------------------------
def decode_image(payload: dict[str, Any]):
    """``{"img": "data:image/png;base64,..."}`` -> RGBA ``PIL.Image``."""
    from PIL import Image  # imported lazily: keeps `base`/`store` Pillow-free

    raw = payload.get("img")
    if not isinstance(raw, str) or not raw:
        raise ValueError("response has no 'img'")
    blob = raw.split(",", 1)[1] if raw.startswith("data:") else raw
    try:
        data = base64.b64decode(blob, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"img is not valid base64: {exc}") from exc
    return Image.open(io.BytesIO(data)).convert("RGBA")


def level_at(image, lat: float, lon: float, window: int = WINDOW) -> tuple[int | None, tuple[int, int], int]:
    """Majority palette level in a ``window``x``window`` box around a point.

    Returns ``(level_or_None, (x, y), n_votes)``.  Pixels that are
    transparent or whose colour is not in :data:`PALETTE` are "no data" and
    simply do not vote; if nothing votes, the level is ``None``.
    """
    width, height = image.size
    x, y = pixel_for(lat, lon, (width, height))
    px = image.load()
    votes: Counter[int] = Counter()
    half = window // 2
    for dy in range(-half, half + 1):
        for dx in range(-half, half + 1):
            xx, yy = x + dx, y + dy
            if not (0 <= xx < width and 0 <= yy < height):
                continue
            r, g, b, a = px[xx, yy]
            if a == 0:
                continue
            level = PALETTE.get((r, g, b))
            if level is not None:
                votes[level] += 1
    if not votes:
        return (None, (x, y), 0)
    # ties -> the higher (more optimistic) level, deterministically
    best = max(votes.items(), key=lambda kv: (kv[1], kv[0]))
    return (best[0], (x, y), best[1])


def parse_time_label(label: str | None, today: date) -> date | None:
    """``"ne 6. 9."`` -> a real date, guessing the year around ``today``.

    Returns ``None`` when the label does not look like a date at all.
    """
    if not label:
        return None
    m = _LABEL_RE.search(label)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    if m.group(3):
        try:
            return date(int(m.group(3)), month, day)
        except ValueError:
            return None
    # The map is published for today or a day or two back, never far ahead,
    # so prefer the most recent candidate that is not in the future.
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    past = [c for c in candidates if 0 <= (today - c).days <= 300]
    if past:
        return max(past)
    ahead = [c for c in candidates if 0 < (c - today).days <= 7]
    return min(ahead) if ahead else None


# ----------------------------------------------------------------------
# fetcher
# ----------------------------------------------------------------------
def fetch(locations: Iterable[Location], *, http: Any, today: date) -> FetchResult:
    """One request for the whole country, then one pixel per location."""
    locs = list(locations)
    try:
        payload = http.get_json(URL)
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
        image = decode_image(payload)
    except Exception as exc:  # noqa: BLE001 - soft failure, PLAN §6
        return FetchResult.failure(SOURCE, f"{type(exc).__name__}: {exc}")

    time_label = payload.get("timeLabel")
    label_date = parse_time_label(time_label, today)
    stale = label_date is not None and (today - label_date).days > STALE_AFTER_DAYS

    readings: list[Reading] = []
    missing: list[str] = []
    location_errors: dict[str, str] = {}
    for loc in locs:
        try:
            level, (x, y), votes = level_at(image, loc.lat, loc.lon)
        except Exception as exc:  # noqa: BLE001
            detail = type(exc).__name__
            location_errors[loc.slug] = detail
            missing.append(f"{loc.slug}: {detail}")
            continue
        if level is None:
            detail = f"no data at px {x},{y}"
            location_errors[loc.slug] = detail
            missing.append(f"{loc.slug}: {detail}")
            continue
        meta: dict[str, Any] = {
            "time_label": time_label,
            "px": [x, y],
            "votes": votes,
            "label": LEVEL_LABELS.get(level),
        }
        if label_date is not None:
            meta["map_date"] = label_date.isoformat()
        if stale:
            meta["stale"] = True
        readings.append(
            Reading(
                source=SOURCE,
                location=loc.slug,
                date=label_date or today,
                metric="level",
                value=float(level),
                text=time_label,
                meta=meta,
            )
        )

    error = "; ".join(missing) or None
    if not readings:
        return FetchResult.failure(SOURCE, error or "no locations resolved", location_errors)
    return FetchResult.success(SOURCE, readings, error, location_errors)


def params_for(location: Location, size: tuple[int, int]) -> dict[str, Any]:
    """Derived params worth caching per PLAN §2a (``store.set_params``)."""
    x, y = pixel_for(location.lat, location.lon, size)
    return {"chmi_px": [x, y], "chmi_px_size": [size[0], size[1]]}
