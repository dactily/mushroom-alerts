from __future__ import annotations

import json

import pytest
import requests

from mushroom_alerts.http import USER_AGENT, Http


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8")

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def make(script, **kw):
    return Http(session=FakeSession(script), backoff=0, **kw)


def test_user_agent_and_timeout():
    http = make([FakeResponse()])
    assert http.session.headers["User-Agent"] == USER_AGENT
    assert "ihor.travkin@gr8.tech" in USER_AGENT
    http.get_json("https://example.test/a")
    assert http.session.calls[0]["timeout"] == 20.0


def test_get_json_text_bytes():
    http = make([FakeResponse(body=b'{"a": 1}')] * 3)
    assert http.get_json("https://example.test/a") == {"a": 1}
    assert http.get_text("https://example.test/a") == '{"a": 1}'
    assert http.get_bytes("https://example.test/a") == b'{"a": 1}'


def test_post_json_sends_the_body():
    http = make([FakeResponse(body=b'{"ok": true}')])
    assert http.post_json("https://example.test/x", {"lat": 49.0}) == {"ok": True}
    assert http.session.calls[0]["method"] == "POST"
    assert http.session.calls[0]["json"] == {"lat": 49.0}


def test_retries_once_on_5xx():
    http = make([FakeResponse(status=503), FakeResponse(body=b'{"a": 2}')])
    assert http.get_json("https://example.test/a") == {"a": 2}
    assert len(http.session.calls) == 2


def test_retries_once_on_connection_error():
    http = make([requests.ConnectionError("boom"), FakeResponse(body=b'{"a": 3}')])
    assert http.get_json("https://example.test/a") == {"a": 3}


def test_gives_up_after_one_retry():
    http = make([FakeResponse(status=500), FakeResponse(status=500)])
    with pytest.raises(requests.HTTPError):
        http.get_json("https://example.test/a")
    assert len(http.session.calls) == 2


def test_does_not_retry_on_4xx():
    http = make([FakeResponse(status=404)])
    with pytest.raises(requests.HTTPError):
        http.get_json("https://example.test/a")
    assert len(http.session.calls) == 1


def test_fixture_name_is_stable_and_safe():
    name = Http.fixture_name("https://houbymapa.cz/predikce")
    assert name == "houbymapa.cz_predikce-fb12f427.raw"
    assert name == Http.fixture_name("https://houbymapa.cz/predikce")  # stable
    a = Http.fixture_name("https://api.test/f", {"lat": 49.0, "lon": 18.0})
    b = Http.fixture_name("https://api.test/f", {"lon": 18.0, "lat": 49.0})
    c = Http.fixture_name("https://api.test/f", {"lat": 50.0, "lon": 18.0})
    assert a == b and a != c
    assert "/" not in a


def test_fixture_name_does_not_collide_on_a_long_url():
    """The ČHMÚ daily files differ only past the 80-character truncation."""
    base = (
        "https://opendata.chmi.cz/meteorology/climate/recent/data/daily/"
        "dly-0-203-0-41101084001-"
    )
    august = Http.fixture_name(base + "202608.json")
    september = Http.fixture_name(base + "202609.json")
    assert august[:80] == september[:80]  # the readable part is identical...
    assert august != september  # ...and the appended digest saves us
    # a params-less URL is hashed too, so every name has the same shape
    assert len(august.split("-")[-1]) == len("deadbeef.raw")


def test_recording_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MUSHROOM_RECORD", raising=False)
    http = make([FakeResponse()], record_dir=tmp_path)
    http.get_json("https://example.test/a")
    assert http.record_dir is None
    assert list(tmp_path.iterdir()) == []


def test_recording_writes_the_body(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSHROOM_RECORD", "1")
    http = make([FakeResponse(body=b'{"a": 1}')], record_dir=tmp_path)
    http.get_json("https://example.test/a")
    written = list(tmp_path.iterdir())
    assert len(written) == 1 and written[0].read_bytes() == b'{"a": 1}'


def test_context_manager_closes():
    with make([FakeResponse()]) as http:
        assert http.get_json("https://example.test/a") == {}
