"""Shared offline test helpers.

Fixtures in ``tests/fixtures`` were recorded from the live endpoints on
2026-09-07 with ``MUSHROOM_RECORD=1``; :class:`FakeHttp` replays them so the
whole suite runs without network access.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mushroom_alerts.base import Location
from mushroom_alerts.http import Http

FIXTURES = Path(__file__).parent / "fixtures"

#: The two locations from PLAN §2a, with their known-good 2026-09-07 values.
VALMEZ = Location(name="Valašské Meziříčí", lat=49.4718, lon=17.9711, slug="valmez")
BYSTRICE = Location(
    name="Valašská Bystřice", lat=49.416, lon=18.106, slug="valasska-bystrice"
)


class FakeHttp:
    """Replays recorded fixtures; raises for anything not recorded."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def _body(self, url: str, params=None) -> bytes:
        self.calls.append(url)
        if self.fail:
            raise ConnectionError("offline")
        path = FIXTURES / Http.fixture_name(url, params)
        if not path.exists():
            raise FileNotFoundError(f"no fixture for {url} ({path.name})")
        return path.read_bytes()

    def get_json(self, url: str, params=None):
        return json.loads(self._body(url, params))

    def get_text(self, url: str, params=None) -> str:
        return self._body(url, params).decode("utf-8")

    def get_bytes(self, url: str, params=None) -> bytes:
        return self._body(url, params)

    def post_json(self, url: str, payload, params=None):
        return json.loads(self._body(url, params))


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def locations() -> list[Location]:
    return [VALMEZ, BYSTRICE]


@pytest.fixture
def chmi_payload() -> dict:
    return json.loads(
        (FIXTURES / Http.fixture_name("https://data-provider.chmi.cz/api/data/rizika/houby")).read_bytes()
    )


@pytest.fixture
def houbymapa_payload() -> dict:
    return json.loads(
        (FIXTURES / Http.fixture_name("https://houbymapa.cz/predikce")).read_bytes()
    )
