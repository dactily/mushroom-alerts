"""Loading and editing ``locations.yaml``.

The file is deliberately dumb -- name + coordinates (+ an optional short
``slug`` when the natural slug is unwieldy).  Everything derived lives in
SQLite, see ``store.Store.get_params`` and PLAN §2a.

    - name: Valašské Meziříčí
      slug: valmez
      lat: 49.4718
      lon: 17.9711
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

import yaml

from .base import Location

__all__ = [
    "DEFAULT_FILE",
    "locations_path",
    "slugify",
    "load_locations",
    "save_locations",
    "add_location",
    "remove_location",
    "find_location",
]

DEFAULT_FILE = "locations.yaml"

_NON_SLUG = re.compile(r"[^a-z0-9]+")

#: Transliterations ``unicodedata`` will not do for us.
_TRANSLIT = str.maketrans({"ß": "ss", "æ": "ae", "ø": "o", "đ": "d", "ł": "l", "ð": "d", "þ": "th"})


def locations_path() -> Path:
    """``$MUSHROOM_LOCATIONS`` or ``./locations.yaml``, falling back to the
    copy shipped next to the package when the cwd has none."""
    env = os.environ.get("MUSHROOM_LOCATIONS")
    if env:
        return Path(env)
    local = Path(DEFAULT_FILE)
    if local.exists():
        return local
    shipped = Path(__file__).resolve().parent.parent / DEFAULT_FILE
    return shipped if shipped.exists() else local


def slugify(name: str) -> str:
    """``"Valašská Bystřice"`` -> ``"valasska-bystrice"`` (ascii, lowercase)."""
    text = name.strip().lower().translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _NON_SLUG.sub("-", text).strip("-")
    return text or "location"


def _parse(entry: dict, taken: set[str]) -> Location:
    name = str(entry.get("name") or "").strip()
    if not name:
        raise ValueError(f"location without a name: {entry!r}")
    try:
        lat = float(entry["lat"])
        lon = float(entry["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"location {name!r} needs numeric lat/lon") from exc
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise ValueError(f"location {name!r} has out-of-range coordinates")
    slug = str(entry.get("slug") or "").strip() or slugify(name)
    base, n = slug, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    taken.add(slug)
    return Location(name=name, lat=lat, lon=lon, slug=slug)


def load_locations(path: str | os.PathLike[str] | None = None) -> list[Location]:
    """Read the YAML file.  Missing file -> empty list."""
    p = Path(path) if path is not None else locations_path()
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    if isinstance(raw, dict):  # tolerate {locations: [...]}
        raw = raw.get("locations") or []
    if not isinstance(raw, list):
        raise ValueError(f"{p}: expected a list of locations")
    taken: set[str] = set()
    return [_parse(e, taken) for e in raw if isinstance(e, dict)]


def save_locations(
    locations: list[Location], path: str | os.PathLike[str] | None = None
) -> Path:
    """Rewrite the YAML file, preserving order."""
    p = Path(path) if path is not None else locations_path()
    payload = []
    for loc in locations:
        entry: dict[str, object] = {"name": loc.name}
        if loc.slug != slugify(loc.name):
            entry["slug"] = loc.slug
        entry["lat"] = loc.lat
        entry["lon"] = loc.lon
        payload.append(entry)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# Locations watched by mushroom-alerts. Name + coordinates only;\n"
        "# everything derived (CHMU pixel, HoubyMapa cell, nearest stations)\n"
        "# is cached in state.sqlite. Edit via `python -m mushroom_alerts add/del`.\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return p


def find_location(locations: list[Location], needle: str) -> Location | None:
    """Match by slug, exact name, or slugified name -- case-insensitively."""
    key = needle.strip()
    low = key.lower()
    for loc in locations:
        if loc.slug == low or loc.name.lower() == low:
            return loc
    target = slugify(key)
    for loc in locations:
        if loc.slug == target or slugify(loc.name) == target:
            return loc
    return None


def add_location(
    name: str, lat: float, lon: float, path: str | os.PathLike[str] | None = None
) -> Location:
    """Append a location and rewrite the file.  Raises on a duplicate."""
    locations = load_locations(path)
    if find_location(locations, name) is not None:
        raise ValueError(f"location {name!r} already exists")
    taken = {loc.slug for loc in locations}
    new = _parse({"name": name, "lat": lat, "lon": lon}, taken)
    locations.append(new)
    save_locations(locations, path)
    return new


def remove_location(
    name: str, path: str | os.PathLike[str] | None = None
) -> Location:
    """Drop a location and rewrite the file.  Raises if it is not there."""
    locations = load_locations(path)
    target = find_location(locations, name)
    if target is None:
        raise ValueError(f"no such location: {name!r}")
    save_locations([loc for loc in locations if loc.slug != target.slug], path)
    return target
