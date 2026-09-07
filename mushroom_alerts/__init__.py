"""mushroom-alerts: mushroom growth alerts around Valašské Meziříčí.

See ``base.py`` for the contract every source module follows.
"""

from .base import (
    EXIT_ERROR,
    EXIT_SIGNAL,
    EXIT_SILENT,
    SOURCES,
    Decision,
    FetchResult,
    Location,
    Reading,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "SOURCES",
    "EXIT_SILENT",
    "EXIT_SIGNAL",
    "EXIT_ERROR",
    "Location",
    "Reading",
    "FetchResult",
    "Decision",
]
