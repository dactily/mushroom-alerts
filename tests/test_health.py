from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from mushroom_alerts import health
from mushroom_alerts import __main__ as cli
from mushroom_alerts.base import Location, Reading
from mushroom_alerts.store import Store

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
REVISION = "1377f95ad2c8191c274bf393774249a70c145f25"
LOCATIONS = [
    Location("First Forest", 49.1, 18.1, "first"),
    Location("Second Forest", 49.2, 18.2, "second"),
]


def _database(path: Path) -> None:
    with Store(path) as store:
        store.upsert_readings(
            [Reading("chmi_station", "first", date(2026, 9, 19), "sra_mm", 4.0)]
        )
        store.save_report(
            date(2026, 9, 19),
            "daily",
            chances={"first": 60, "second": 55, "removed-location": 95},
            verdicts={},
            candidates={},
            events={},
            error_class="none",
            rules_version="test",
            sent=False,
            reason="test",
        )
        store.conn.execute(
            "UPDATE readings SET fetched_at='2026-09-19T09:55:00+00:00'"
        )
        store.conn.execute(
            "UPDATE reports SET created_at='2026-09-19T09:59:00+00:00'"
        )
        store.conn.commit()


def _jobs(path: Path, *, daily_status: str = "ok", daily_next: str | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "f28cb85d2a56",
                        "name": "UNTRUSTED DAILY NAME",
                        "enabled": True,
                        "last_run_at": "2026-09-19T08:31:31+02:00",
                        "last_status": daily_status,
                        "next_run_at": daily_next or "2026-09-20T08:30:00+02:00",
                        "prompt": "must never appear",
                        "last_error": "must never appear",
                    },
                    {
                        "id": "17167162fb31",
                        "name": "UNTRUSTED WEEKEND NAME",
                        "enabled": False,
                        "last_run_at": None,
                        "last_status": None,
                        "next_run_at": "2026-09-25T19:00:00+02:00",
                    },
                    {
                        "id": "not-allowlisted",
                        "name": "Secret job",
                        "enabled": True,
                        "prompt": "secret",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def test_schema_v1_is_sanitized_and_uses_shared_favorable_threshold(
    tmp_path, monkeypatch
):
    database = tmp_path / "state.sqlite"
    jobs = tmp_path / "jobs.json"
    _database(database)
    _jobs(jobs)
    before = database.read_bytes()
    monkeypatch.setattr(health, "_deployed_revision", lambda project_root=None: REVISION)

    payload, readable = health.build(
        LOCATIONS,
        generated_at=NOW,
        database=database,
        jobs_file=jobs,
        project_root=tmp_path,
    )

    assert readable is True
    assert payload == {
        "schemaVersion": "1",
        "generatedAt": "2026-09-19T10:00:00Z",
        "locationsCount": 2,
        "favorableLocationsCount": 1,
        "lastEvaluationAt": "2026-09-19T09:59:00Z",
        "weatherDataAgeSeconds": 300,
        "deployedRevision": REVISION,
        "jobs": [
            {
                "id": "f28cb85d2a56",
                "name": "mushroom-daily",
                "enabled": True,
                "lastRun": "2026-09-19T08:31:31+02:00",
                "lastStatus": "ok",
                "nextRun": "2026-09-20T08:30:00+02:00",
            },
            {
                "id": "17167162fb31",
                "name": "mushroom-weekend",
                "enabled": False,
                "lastRun": None,
                "lastStatus": "unknown",
                "nextRun": "2026-09-25T19:00:00+02:00",
            },
        ],
    }
    assert database.read_bytes() == before
    serialized = json.dumps(payload)
    assert "UNTRUSTED" not in serialized
    assert "must never appear" not in serialized
    assert "not-allowlisted" not in serialized


def test_overdue_enabled_job_is_missed(tmp_path, monkeypatch):
    database = tmp_path / "state.sqlite"
    jobs = tmp_path / "jobs.json"
    _database(database)
    _jobs(jobs, daily_status="ok", daily_next="2026-09-19T09:40:00Z")
    monkeypatch.setattr(health, "_deployed_revision", lambda project_root=None: None)

    payload, readable = health.build(
        LOCATIONS, generated_at=NOW, database=database, jobs_file=jobs
    )

    assert readable is True
    assert payload["jobs"][0]["lastStatus"] == "missed"


def test_failed_scheduler_status_is_normalized(tmp_path, monkeypatch):
    database = tmp_path / "state.sqlite"
    jobs = tmp_path / "jobs.json"
    _database(database)
    _jobs(jobs, daily_status="delivery_failed")
    monkeypatch.setattr(health, "_deployed_revision", lambda project_root=None: None)

    payload, readable = health.build(
        LOCATIONS, generated_at=NOW, database=database, jobs_file=jobs
    )

    assert readable is True
    assert payload["jobs"][0]["lastStatus"] == "failed"


def test_paused_job_is_not_reported_as_enabled(tmp_path, monkeypatch):
    database = tmp_path / "state.sqlite"
    jobs = tmp_path / "jobs.json"
    _database(database)
    _jobs(jobs)
    document = json.loads(jobs.read_text(encoding="utf-8"))
    document["jobs"][0]["state"] = "paused"
    jobs.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(health, "_deployed_revision", lambda project_root=None: None)

    payload, readable = health.build(
        LOCATIONS, generated_at=NOW, database=database, jobs_file=jobs
    )

    assert readable is True
    assert payload["jobs"][0]["enabled"] is False


def test_missing_allowlisted_job_keeps_shape_and_marks_export_unreadable(
    tmp_path, monkeypatch
):
    database = tmp_path / "state.sqlite"
    jobs = tmp_path / "jobs.json"
    _database(database)
    jobs.write_text('{"jobs": []}', encoding="utf-8")
    monkeypatch.setattr(health, "_deployed_revision", lambda project_root=None: None)

    payload, readable = health.build(
        LOCATIONS, generated_at=NOW, database=database, jobs_file=jobs
    )

    assert readable is False
    assert len(payload["jobs"]) == 2
    assert all(job["enabled"] is False for job in payload["jobs"])
    assert all(job["lastStatus"] == "unknown" for job in payload["jobs"])


def test_missing_database_keeps_schema_and_marks_export_unreadable(
    tmp_path, monkeypatch
):
    jobs = tmp_path / "jobs.json"
    _jobs(jobs)
    monkeypatch.setattr(health, "_deployed_revision", lambda project_root=None: None)

    payload, readable = health.build(
        LOCATIONS,
        generated_at=NOW,
        database=tmp_path / "missing.sqlite",
        jobs_file=jobs,
    )

    assert readable is False
    assert payload["favorableLocationsCount"] == 0
    assert payload["lastEvaluationAt"] is None
    assert payload["weatherDataAgeSeconds"] == 0


def test_export_health_cli_emits_json_and_a_useful_exit_code(
    tmp_path, monkeypatch, capsys
):
    database = tmp_path / "state.sqlite"
    jobs = tmp_path / "jobs.json"
    _database(database)
    _jobs(jobs)
    monkeypatch.setenv("MUSHROOM_DB", str(database))
    monkeypatch.setenv("MUSHROOM_HERMES_JOBS", str(jobs))
    monkeypatch.setattr(cli, "load_locations", lambda: LOCATIONS)
    monkeypatch.setattr(health, "utcnow", lambda: NOW)
    monkeypatch.setattr(health, "_deployed_revision", lambda project_root=None: REVISION)

    assert cli.main(["export-health", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == "1"
    assert payload["jobs"][0]["id"] == "f28cb85d2a56"

    jobs.write_text('{"jobs": []}', encoding="utf-8")
    assert cli.main(["export-health", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["jobs"][0]["lastStatus"] == "unknown"

    _jobs(jobs)
    monkeypatch.setattr(cli, "load_locations", lambda: (_ for _ in ()).throw(ValueError()))
    assert cli.main(["export-health", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["locationsCount"] == 0
    assert "locations unavailable" in captured.err
