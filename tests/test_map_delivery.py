"""``brief --mode ... --map-dir``: the picture as an attachment, never a risk.

The block is what Hermes sends.  The map is an addition to it, and every
test here is about that asymmetry:

* without ``--map-dir`` the output is byte-for-byte what it was before the
  renderer existed;
* with it, the only change is one more line at the end, ``MEDIA:<path>``;
* a silent day writes no file at all -- an unsent picture is a picture
  nobody asked for, and it would still be lying in the directory tomorrow;
* a broken renderer costs the picture and nothing else: the block is
  printed in full, the exit code does not move, the report row is already
  stored, and yesterday's map is never attached instead.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from mushroom_alerts import __main__ as cli
from mushroom_alerts.mapping import render as render_lib
from mushroom_alerts.store import Store

from tests.fake_basemap import FakeBasemap
from tests.test_brief import (  # the same fake stack the block's own tests use
    fake_module,
    full_stack,
    use_fetchers,
)

SHIPPED = Path(__file__).resolve().parent.parent / "locations.yaml"
TODAY = date.today()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own DB, own copy of locations.yaml, no network, a fake basemap."""
    locations = tmp_path / "locations.yaml"
    locations.write_text(SHIPPED.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("MUSHROOM_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("MUSHROOM_LOCATIONS", str(locations))
    monkeypatch.delenv("MUSHROOM_RECORD", raising=False)
    monkeypatch.delenv("MUSHROOM_API30_THRESHOLD", raising=False)
    monkeypatch.setattr(cli, "Http", lambda **kw: _NullHttp())
    monkeypatch.setattr(render_lib, "load_basemap", lambda path=None: FakeBasemap())
    yield tmp_path


class _NullHttp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def maps(tmp_path):
    return tmp_path / "maps"


def media_line(out: str) -> str | None:
    lines = out.rstrip("\n").splitlines()
    return lines[-1] if lines and lines[-1].startswith("MEDIA:") else None


def silence_today(reason: str = "вчера") -> None:
    """Copy today's stored report back one day, so nothing has moved."""
    with Store() as store:
        row = store.last_report("daily")
        store.save_report(
            TODAY - timedelta(days=1),
            "daily",
            chances=json.loads(row["chances_json"]),
            verdicts=json.loads(row["verdicts_json"]),
            candidates=json.loads(row["candidates_json"]),
            events=json.loads(row["events_json"]),
            error_class=row["error_class"],
            rules_version=row["rules_version"],
            sent=True,
            reason=reason,
        )


# ----------------------------------------------------------------------
# the happy path
# ----------------------------------------------------------------------
def test_the_map_is_written_and_announced_on_the_last_line(monkeypatch, capsys, maps):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)]) == 0
    out = capsys.readouterr().out

    line = media_line(out)
    assert line is not None, out
    path = Path(line[len("MEDIA:") :])
    assert path.is_absolute() and path.exists()
    assert path.parent == maps.resolve()
    assert render_lib.FILE_PATTERN.match(path.name)
    assert path.name.startswith(f"mushroom-daily-{TODAY.isoformat()}-")
    # the line is last, and it is the last thing after ОГОВОРКИ
    body = out.rstrip("\n").splitlines()
    assert body[-2].startswith("ОГОВОРКИ:")
    assert out.count("MEDIA:") == 1


def test_the_weekend_run_writes_its_own_map(monkeypatch, capsys, maps):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "weekend", "--map-dir", str(maps)]) == 0
    line = media_line(capsys.readouterr().out)
    assert line and "mushroom-weekend-" in line
    assert Path(line[len("MEDIA:") :]).exists()


def test_without_the_flag_the_block_is_byte_identical(monkeypatch, capsys, maps):
    """The same run twice: the only difference the flag may make is the line."""
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily"]) == 0
    plain = capsys.readouterr().out
    assert "MEDIA:" not in plain

    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)]) == 0
    withmap = capsys.readouterr().out
    line = media_line(withmap)
    assert line is not None
    assert withmap == plain.rstrip("\n") + "\n" + line + "\n"


def test_two_runs_leave_two_files(monkeypatch, capsys, maps):
    full_stack(monkeypatch)
    cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)])
    first = media_line(capsys.readouterr().out)
    cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)])
    second = media_line(capsys.readouterr().out)

    assert first != second
    written = sorted(path.name for path in maps.iterdir())
    assert len(written) == 2
    assert all(render_lib.FILE_PATTERN.match(name) for name in written)
    for line in (first, second):
        assert Path(line[len("MEDIA:") :]).exists()


# ----------------------------------------------------------------------
# a silent day
# ----------------------------------------------------------------------
def test_a_silent_day_writes_no_file(monkeypatch, capsys, maps):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)]) == 0
    capsys.readouterr()
    before = sorted(path.name for path in maps.iterdir())
    assert len(before) == 1
    silence_today()

    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ОТПРАВЛЯТЬ: нет\n")
    assert "MEDIA:" not in out
    assert sorted(path.name for path in maps.iterdir()) == before


def test_a_silent_day_does_not_even_make_the_directory(monkeypatch, capsys, tmp_path):
    full_stack(monkeypatch)
    fresh = tmp_path / "never"
    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(fresh)]) == 0
    capsys.readouterr()
    silence_today()
    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(fresh)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ОТПРАВЛЯТЬ: нет\n") and "MEDIA:" not in out
    assert not (fresh / "unused").exists()


# ----------------------------------------------------------------------
# --json
# ----------------------------------------------------------------------
def test_the_json_carries_the_absolute_map_path(monkeypatch, capsys, maps):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily", "--json", "--map-dir", str(maps)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["map_path"] is not None
    assert Path(payload["map_path"]).is_absolute()
    assert Path(payload["map_path"]).exists()
    assert payload["text"].rstrip("\n").endswith(f"MEDIA:{payload['map_path']}")
    assert payload["exit_code"] == 0


def test_the_json_says_null_when_nothing_was_sent(monkeypatch, capsys, maps):
    full_stack(monkeypatch)
    cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)])
    capsys.readouterr()
    silence_today()
    assert cli.main(["brief", "--mode", "daily", "--json", "--map-dir", str(maps)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["send"] is False
    assert payload["map_path"] is None
    assert "MEDIA:" not in payload["text"]


def test_the_json_has_no_map_path_without_the_flag(monkeypatch, capsys):
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "map_path" not in payload
    assert "MEDIA:" not in payload["text"]


# ----------------------------------------------------------------------
# when the drawing fails
# ----------------------------------------------------------------------
def test_a_broken_renderer_still_prints_the_block(monkeypatch, capsys, maps):
    full_stack(monkeypatch)

    def explode(*args, **kwargs):
        raise RuntimeError("no basemap on this box")

    monkeypatch.setattr(render_lib, "render_map", explode)
    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)]) == 0
    captured = capsys.readouterr()

    assert captured.out.startswith("ОТПРАВЛЯТЬ: да\n")
    assert "ШАНС на " in captured.out and "ОГОВОРКИ:" in captured.out
    assert "MEDIA:" not in captured.out
    assert "map: not rendered" in captured.err
    assert "no basemap on this box" in captured.err
    assert not maps.exists() or list(maps.iterdir()) == []
    # the decision was taken and stored before the drawing was attempted
    with Store() as store:
        assert store.last_report("daily")["sent"] == 1


def test_a_broken_renderer_keeps_the_exit_code_of_a_blackout(monkeypatch, capsys, maps):
    use_fetchers(monkeypatch, [fake_module("chmi_map", ok=False, error="boom")])
    monkeypatch.setattr(
        render_lib, "render_map", lambda *a, **k: 1 / 0
    )
    assert cli.main(["brief", "--mode", "weekend", "--map-dir", str(maps)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("ОТПРАВЛЯТЬ: да\n")
    assert "MEDIA:" not in out


def test_a_broken_brief_draws_nothing_at_all(monkeypatch, capsys, maps):
    """No numbers in the block means no numbers to put on a picture."""
    use_fetchers(monkeypatch, [fake_module("chmi_map", ok=False, error="boom")])
    assert cli.main(["brief", "--mode", "weekend", "--map-dir", str(maps)]) == 1
    captured = capsys.readouterr()
    assert "MEDIA:" not in captured.out
    assert "map: the brief did not assemble" in captured.err
    assert not maps.exists() or list(maps.iterdir()) == []


def test_an_older_map_is_never_attached(monkeypatch, capsys, maps):
    full_stack(monkeypatch)
    maps.mkdir(parents=True)
    stale = maps / render_lib.map_name("daily", TODAY - timedelta(days=1))
    stale.write_bytes(b"not today")

    monkeypatch.setattr(render_lib, "render_map", lambda *a, **k: 1 / 0)
    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)]) == 0
    out = capsys.readouterr().out
    assert "MEDIA:" not in out
    assert stale.name not in out
    assert stale.exists()  # nor is it deleted: pruning only runs on success


# ----------------------------------------------------------------------
# housekeeping
# ----------------------------------------------------------------------
def test_a_successful_run_prunes_only_its_own_old_maps(monkeypatch, capsys, maps):
    full_stack(monkeypatch)
    maps.mkdir(parents=True)
    old = maps / render_lib.map_name("daily", TODAY - timedelta(days=60))
    recent = maps / render_lib.map_name("weekend", TODAY - timedelta(days=3))
    alien = maps / "family-photo.png"
    for path in (old, recent, alien):
        path.write_bytes(b"x")

    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)]) == 0
    captured = capsys.readouterr()

    assert not old.exists()
    assert recent.exists() and alien.exists()
    assert f"map: pruned {old.name}" in captured.err
    line = media_line(captured.out)
    assert line and Path(line[len("MEDIA:") :]).exists()


def test_a_location_outside_the_basemap_is_reported_to_stderr(monkeypatch, capsys, maps, tmp_path):
    """The picture cannot show it; the run must still say so out loud."""
    locations = tmp_path / "locations.yaml"
    locations.write_text(
        locations.read_text(encoding="utf-8")
        + "- name: Praha\n  slug: praha\n  short: Praha\n  lat: 50.0755\n  lon: 14.4378\n",
        encoding="utf-8",
    )
    full_stack(monkeypatch)
    assert cli.main(["brief", "--mode", "daily", "--map-dir", str(maps)]) == 0
    captured = capsys.readouterr()

    assert "map: praha" in captured.err and "outside" in captured.err
    assert "Praha — нет данных" in captured.out  # still in the block
    line = media_line(captured.out)
    assert line and Path(line[len("MEDIA:") :]).exists()


def test_the_map_flag_is_documented_in_the_help(capsys):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["brief", "--help"])
    help_text = capsys.readouterr().out
    assert "--map-dir" in help_text
    assert "31" in help_text
