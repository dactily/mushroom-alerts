"""Tiny HTTP helper shared by all fetchers.

One :class:`Http` instance per CLI run (one connection pool, one User-Agent
with a contact address as PLAN §6 asks for).  Every method raises
``requests.RequestException`` (or ``ValueError`` for bad JSON) on failure --
fetchers are expected to catch that and return ``FetchResult.failure``.

Recording fixtures
------------------
Run with ``MUSHROOM_RECORD=1`` and every response body is dumped into
``tests/fixtures/`` (or ``record_dir``), named after the URL.  That is how
the committed fixtures were made; tests then run fully offline.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

__all__ = ["Http", "USER_AGENT", "DEFAULT_TIMEOUT"]

USER_AGENT = "mushroom-alerts/0.1 (personal; contact ihor.travkin@gr8.tech)"
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 1
BACKOFF_SECONDS = 2.0

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class Http:
    """``requests.Session`` with a UA, a timeout and one retry on 5xx."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        record_dir: str | os.PathLike[str] | None = None,
        session: requests.Session | None = None,
        backoff: float = BACKOFF_SECONDS,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        self.record_dir: Path | None = None
        if os.environ.get("MUSHROOM_RECORD") == "1":
            self.record_dir = Path(record_dir or _default_record_dir())

    # -- context manager -------------------------------------------------
    def __enter__(self) -> "Http":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    # -- core ------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
            else:
                if resp.status_code >= 500 and attempt < self.retries:
                    last = requests.HTTPError(f"{resp.status_code} from {url}", response=resp)
                else:
                    resp.raise_for_status()
                    self._record(url, params, resp)
                    return resp
            if attempt < self.retries:
                time.sleep(self.backoff * (attempt + 1))
        assert last is not None
        raise last

    # -- convenience -----------------------------------------------------
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", url, params=params).json()

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return self.request("GET", url, params=params).text

    def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        return self.request("GET", url, params=params).content

    def post_json(
        self, url: str, payload: Any, params: dict[str, Any] | None = None
    ) -> Any:
        return self.request("POST", url, params=params, json_body=payload).json()

    # -- fixture recording ------------------------------------------------
    def _record(
        self, url: str, params: dict[str, Any] | None, resp: requests.Response
    ) -> None:
        if self.record_dir is None:
            return
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / self.fixture_name(url, params)).write_bytes(resp.content)
        except OSError:
            pass  # recording is a developer convenience, never fatal

    @staticmethod
    def fixture_name(url: str, params: dict[str, Any] | None = None) -> str:
        """Stable, filesystem-safe name for the response to ``url``."""
        stem = _SAFE.sub("_", url.split("://", 1)[-1]).strip("_")[:80]
        if params:
            digest = hashlib.sha1(
                repr(sorted(params.items())).encode("utf-8")
            ).hexdigest()[:8]
            stem = f"{stem}-{digest}"
        return f"{stem}.raw"


def _default_record_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "tests" / "fixtures"
