"""The basemap: a picture of the forecast area plus the maths that puts a
location on it.

The daily render must work on a box with no network, no tiles and no
browser, so the map itself is a **committed artefact**:
``assets/basemap/vsetinsko.png`` (the 1200x1000 map viewport) next to
``assets/basemap/vsetinsko.json`` (what it shows and where).  Both are
built once, by hand, with ``tools/build_basemap.py``; see
``assets/basemap/README.md``.  Nothing in this module touches the network.

Projection
----------
Web Mercator (EPSG:3857), the projection every slippy map uses, so the
picture is exactly what a tile server would have drawn.  The bounding box
was chosen so that the Mercator aspect ratio equals the pixel aspect ratio
(1000/1200) and the image is therefore geometrically undistorted::

    x = R * radians(lon)
    y = R * ln(tan(pi/4 + radians(lat)/2))

:meth:`Basemap.to_pixel` returns floats with the origin at the top-left
corner and *y* growing downwards, i.e. Pillow's coordinate system.

A location outside the box is a bug in ``locations.yaml`` or in the box,
never something to clamp or to drop quietly: :meth:`Basemap.to_pixel`
raises :class:`OutsideBasemap` and the caller has to decide.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cost is for the annotation only
    from PIL.Image import Image

__all__ = [
    "ASSETS_DIR",
    "FONT_PATH",
    "FONT_BOLD_PATH",
    "DEFAULT_BASEMAP",
    "ATTRIBUTION",
    "PROJECTION",
    "EARTH_RADIUS_M",
    "mercator_x",
    "mercator_y",
    "inverse_mercator_y",
    "OutsideBasemap",
    "Basemap",
    "load_basemap",
]

#: Repository ``assets/`` -- the package is deployed as a checkout
#: (``pip install -e .``), so the artefacts live next to the source.
ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"

#: Vendored DejaVu Sans (Czech *and* Cyrillic coverage, free licence), so a
#: render looks the same on the laptop and on the server.
FONT_PATH = ASSETS_DIR / "fonts" / "DejaVuSans.ttf"
FONT_BOLD_PATH = ASSETS_DIR / "fonts" / "DejaVuSans-Bold.ttf"

#: The one basemap we ship.
DEFAULT_BASEMAP = ASSETS_DIR / "basemap" / "vsetinsko.json"

#: ODbL asks for this, verbatim, wherever the map is shown.  The built PNG
#: already carries it in its bottom-right corner -- see the README.
ATTRIBUTION = "© OpenStreetMap contributors"

PROJECTION = "EPSG:3857"

#: WGS84 semi-major axis; the sphere EPSG:3857 pretends the Earth is.
EARTH_RADIUS_M = 6378137.0

#: Web Mercator gives up near the poles; ours is a 40 km box in Moravia.
MAX_MERCATOR_LAT = 85.05112877980659


def mercator_x(lon: float) -> float:
    """Longitude -> EPSG:3857 easting, metres."""
    return EARTH_RADIUS_M * math.radians(lon)


def mercator_y(lat: float) -> float:
    """Latitude -> EPSG:3857 northing, metres."""
    lat = max(-MAX_MERCATOR_LAT, min(MAX_MERCATOR_LAT, lat))
    return EARTH_RADIUS_M * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def inverse_mercator_y(y: float) -> float:
    """EPSG:3857 northing -> latitude, degrees."""
    return math.degrees(2 * math.atan(math.exp(y / EARTH_RADIUS_M)) - math.pi / 2)


class OutsideBasemap(ValueError):
    """A point that is not on the basemap.

    Raised instead of clamping: a marker half a kilometre off the edge is a
    lie, and a silently dropped location is worse.  Pass ``label`` (the
    location slug) so the message says *which* point is wrong.
    """

    def __init__(
        self, lat: float, lon: float, basemap: "Basemap", label: str = ""
    ) -> None:
        self.lat = lat
        self.lon = lon
        self.label = label
        self.bbox = (basemap.min_lon, basemap.min_lat, basemap.max_lon, basemap.max_lat)
        who = f"{label} " if label else ""
        super().__init__(
            f"{who}({lat:.6f}, {lon:.6f}) is outside the {basemap.name!r} basemap: "
            f"lat {basemap.min_lat:.6f}..{basemap.max_lat:.6f}, "
            f"lon {basemap.min_lon:.6f}..{basemap.max_lon:.6f}"
        )


#: ``{(path, mtime_ns, size): RGB image}`` -- one entry, in practice.  The
#: cached image is never handed out, only copies of it.
_IMAGE_CACHE: dict[tuple[str, int, int], "Image"] = {}


@dataclass(frozen=True, slots=True)
class Basemap:
    """A built basemap: the picture, its box, and the projection on to it."""

    name: str
    width: int
    height: int
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    image_path: Path
    attribution: str = ATTRIBUTION
    built_at: str = ""
    source: str = ""

    # -- geometry --------------------------------------------------------
    @property
    def centre_lat(self) -> float:
        """The latitude halfway up the *image* (not the average of the two
        edges -- Mercator stretches northwards)."""
        return inverse_mercator_y((mercator_y(self.min_lat) + mercator_y(self.max_lat)) / 2)

    @property
    def centre_lon(self) -> float:
        return (self.min_lon + self.max_lon) / 2

    def contains(self, lat: float, lon: float) -> bool:
        """Is the point on the map?  Edges count as inside."""
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon

    def to_pixel(self, lat: float, lon: float, *, label: str = "") -> tuple[float, float]:
        """Location -> pixel, origin top-left, *y* downwards, floats.

        Raises :class:`OutsideBasemap` for a point off the map.
        """
        if not self.contains(lat, lon):
            raise OutsideBasemap(lat, lon, self, label)
        x0, x1 = mercator_x(self.min_lon), mercator_x(self.max_lon)
        y0, y1 = mercator_y(self.min_lat), mercator_y(self.max_lat)
        x = (mercator_x(lon) - x0) / (x1 - x0) * self.width
        y = (y1 - mercator_y(lat)) / (y1 - y0) * self.height
        return x, y

    def km_per_pixel(self, lat: float | None = None) -> float:
        """Ground kilometres one pixel covers -- for the scale bar.

        Mercator's scale depends on latitude, so this does too; the default
        is the middle of the image, which is what a scale bar drawn near the
        centre should use.
        """
        if lat is None:
            lat = self.centre_lat
        span_m = mercator_x(self.max_lon) - mercator_x(self.min_lon)
        return span_m / self.width * math.cos(math.radians(lat)) / 1000.0

    # -- the picture -----------------------------------------------------
    def open_image(self) -> "Image":
        """The basemap picture as a fresh RGB image, safe to draw on.

        The decoded original is cached and *never* returned; every caller
        gets its own copy, so two renders in one process cannot see each
        other's markers.
        """
        from PIL import Image as _Image

        stat = self.image_path.stat()
        key = (str(self.image_path), stat.st_mtime_ns, stat.st_size)
        cached = _IMAGE_CACHE.get(key)
        if cached is None:
            with _Image.open(self.image_path) as raw:
                cached = raw.convert("RGB")
            _IMAGE_CACHE.clear()  # one basemap at a time; keep this tiny
            _IMAGE_CACHE[key] = cached
        return cached.copy()


def load_basemap(path: Path | None = None) -> Basemap:
    """Read a basemap sidecar (default :data:`DEFAULT_BASEMAP`).

    The PNG is expected next to the JSON, under the ``image`` key or, if it
    is missing, under the JSON's own stem.
    """
    p = Path(path) if path is not None else DEFAULT_BASEMAP
    data = json.loads(p.read_text(encoding="utf-8"))
    projection = str(data.get("projection") or PROJECTION)
    if projection != PROJECTION:
        raise ValueError(f"{p}: unsupported projection {projection!r}, expected {PROJECTION!r}")
    try:
        bbox = data["bbox"]
        width, height = int(data["width"]), int(data["height"])
        min_lon, min_lat = float(bbox["min_lon"]), float(bbox["min_lat"])
        max_lon, max_lat = float(bbox["max_lon"]), float(bbox["max_lat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{p}: not a basemap sidecar ({exc})") from exc
    if not (min_lon < max_lon and min_lat < max_lat):
        raise ValueError(f"{p}: empty bounding box")
    if width <= 0 or height <= 0:
        raise ValueError(f"{p}: bad image size {width}x{height}")
    image = str(data.get("image") or f"{p.stem}.png")
    return Basemap(
        name=str(data.get("name") or p.stem),
        width=width,
        height=height,
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        image_path=p.parent / image,
        attribution=str(data.get("attribution") or ATTRIBUTION),
        built_at=str(data.get("built_at") or ""),
        source=str(data.get("source") or ""),
    )
