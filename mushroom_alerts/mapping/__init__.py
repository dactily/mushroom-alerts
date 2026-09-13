"""Drawing the forecast map locally -- no tiles, no network, no browser.

:mod:`mushroom_alerts.mapping.basemap` holds the pre-built picture of the
area and the Web Mercator maths that turns a location into a pixel on it.
The picture itself is a committed artefact built once by
``tools/build_basemap.py``; ``assets/basemap/README.md`` says how.
"""

from __future__ import annotations

from .basemap import (
    ATTRIBUTION,
    DEFAULT_BASEMAP,
    FONT_BOLD_PATH,
    FONT_PATH,
    Basemap,
    OutsideBasemap,
    load_basemap,
)

__all__ = [
    "ATTRIBUTION",
    "DEFAULT_BASEMAP",
    "FONT_BOLD_PATH",
    "FONT_PATH",
    "Basemap",
    "OutsideBasemap",
    "load_basemap",
]
