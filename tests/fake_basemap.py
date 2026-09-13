"""A basemap stand-in, so the renderer can be tested without the artefact.

The real one (``mushroom_alerts.mapping.basemap``) carries a committed
1200x1000 PNG of the Vsetín hills.  A test must not depend on that file
existing, on how it looks, or on the branch it arrives from, so this class
implements the same interface over a flat colour: same Web Mercator maths,
same origin (top-left, *y* downwards), same :class:`OutsideBasemap` on a
point off the map -- imported from :mod:`~mushroom_alerts.mapping.render`,
so the fake always raises exactly what the renderer catches.

The box is a little wider than ``locations.yaml`` needs, which is what a
real basemap would be too: 17.6..18.5 E, 49.15..49.60 N covers all thirteen
forests with room to spare.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from mushroom_alerts.mapping.render import OutsideBasemap

EARTH_RADIUS_M = 6378137.0


def mercator_y(lat: float) -> float:
    return EARTH_RADIUS_M * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def mercator_x(lon: float) -> float:
    return EARTH_RADIUS_M * math.radians(lon)


@dataclass(frozen=True, slots=True)
class FakeBasemap:
    """Everything ``render`` asks a basemap for, and nothing else."""

    name: str = "fake"
    width: int = 1200
    height: int = 1000
    min_lon: float = 17.60
    min_lat: float = 49.15
    max_lon: float = 18.50
    max_lat: float = 49.60
    image_path: Path = Path("fake-basemap.png")
    attribution: str = "© OpenStreetMap contributors"
    built_at: str = "2026-09-13T00:00:00+00:00"
    source: str = "fake"
    fill: tuple[int, int, int] = (206, 219, 196)

    @property
    def centre_lat(self) -> float:
        return (self.min_lat + self.max_lat) / 2

    def contains(self, lat: float, lon: float) -> bool:
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon

    def to_pixel(self, lat: float, lon: float) -> tuple[float, float]:
        if not self.contains(lat, lon):
            raise OutsideBasemap(lat, lon, self)
        x0, x1 = mercator_x(self.min_lon), mercator_x(self.max_lon)
        y0, y1 = mercator_y(self.min_lat), mercator_y(self.max_lat)
        return (
            (mercator_x(lon) - x0) / (x1 - x0) * self.width,
            (y1 - mercator_y(lat)) / (y1 - y0) * self.height,
        )

    def km_per_pixel(self, lat: float | None = None) -> float:
        lat = self.centre_lat if lat is None else lat
        span_m = mercator_x(self.max_lon) - mercator_x(self.min_lon)
        return span_m / self.width * math.cos(math.radians(lat)) / 1000.0

    def open_image(self) -> Image.Image:
        """A fresh RGB image: the committed picture if there is one, else
        the flat colour a test can recognise under the markers."""
        if self.image_path.exists():
            with Image.open(self.image_path) as raw:
                return raw.convert("RGB")
        return Image.new("RGB", (self.width, self.height), self.fill)
