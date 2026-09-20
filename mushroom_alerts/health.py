"""Sanitized, read-only health export for Mushroom Alerts and its Hermes jobs.

The exporter is intentionally small and conservative.  It reads the application
database through a SQLite ``mode=ro`` URI, reads only the two explicitly allowed
Hermes job records, and never exposes prompts, delivery targets, errors, or other
scheduler configuration.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import policy
from .base import Location, utcnow
from .store import DEFAULT_DB, db_path

SCHEMA_VERSION = "1"
OVERDUE_GRACE_SECONDS = 15 * 60

# These IDs are deployment identities, not secrets.  Keeping the display names
# here, rather than copying them from jobs.json, prevents scheduler-controlled
# text from leaking into the sanitized export.
ALLOWED_JOBS = (
    ("f28cb85d2a56", "mushroom-daily"),
    ("17167162fb31", "mushroom-weekend"),
)


def _rfc3339(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: Any) -> datetime | None:
    normalized = _rfc3339(value)
    if normalized is None:
        return None
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def _database_health(
    path: Path,
    locations: Sequence[Location],
    generated_at: datetime,
) -> tuple[int, str | None, int, bool]:
    """Return favorable count, last evaluation, weather age, and read status."""
    if not path.is_file():
        return 0, None, 0, False

    connection: sqlite3.Connection | None = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        report = connection.execute(
            """SELECT chances_json, created_at
               FROM reports
               ORDER BY datetime(created_at) DESC, id DESC
               LIMIT 1"""
        ).fetchone()
        freshest = connection.execute(
            "SELECT MAX(fetched_at) AS fetched_at FROM readings"
        ).fetchone()
    except (OSError, sqlite3.Error):
        return 0, None, 0, False
    finally:
        if connection is not None:
            connection.close()

    last_evaluation = _rfc3339(report["created_at"]) if report is not None else None
    configured = {location.slug for location in locations}
    favorable = 0
    data_ok = report is None or last_evaluation is not None
    if report is not None:
        try:
            chances = json.loads(report["chances_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            chances = {}
            data_ok = False
        if not isinstance(chances, dict):
            chances = {}
            data_ok = False
        favorable = sum(
            1
            for slug, chance in chances.items()
            if slug in configured
            and isinstance(chance, (int, float))
            and not isinstance(chance, bool)
            and chance >= policy.CHANCE_ALERT_PCT
        )

    fetched_at = _parse_rfc3339(freshest["fetched_at"] if freshest else None)
    if freshest is not None and freshest["fetched_at"] is not None and fetched_at is None:
        data_ok = False
    weather_age = (
        max(0, int((generated_at - fetched_at).total_seconds()))
        if fetched_at is not None
        else 0
    )
    return favorable, last_evaluation, weather_age, data_ok


def _jobs_path() -> Path:
    configured = os.environ.get("MUSHROOM_HERMES_JOBS")
    if configured:
        return Path(configured)
    return Path.home() / ".hermes" / "profiles" / "family" / "cron" / "jobs.json"


def _normalized_job_status(
    job: Mapping[str, Any] | None,
    *,
    generated_at: datetime,
) -> str:
    if job is None:
        return "unknown"

    enabled = _job_enabled(job)
    next_run = _parse_rfc3339(job.get("next_run_at"))
    if enabled and not job.get("next_run_at"):
        return "missed"
    if (
        enabled
        and next_run is not None
        and (generated_at - next_run).total_seconds() > OVERDUE_GRACE_SECONDS
    ):
        return "missed"

    raw = str(job.get("last_status") or "").strip().lower()
    if raw in {"ok", "success", "succeeded"}:
        return "ok"
    if raw in {
        "error",
        "failed",
        "failure",
        "delivery_failed",
        "timed_out",
        "timeout",
    }:
        return "failed"
    if raw in {"missed", "overdue"}:
        return "missed"
    return "unknown"


def _job_enabled(job: Mapping[str, Any]) -> bool:
    return job.get("enabled") is True and job.get("state") not in {
        "paused",
        "completed",
    }


def _scheduler_health(
    path: Path,
    generated_at: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    loaded = True
    records: dict[str, Mapping[str, Any]] = {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        jobs = document.get("jobs") if isinstance(document, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("jobs must be a list")
        records = {
            str(job.get("id")): job
            for job in jobs
            if isinstance(job, dict) and str(job.get("id")) in dict(ALLOWED_JOBS)
        }
    except (OSError, ValueError, json.JSONDecodeError):
        loaded = False

    result = []
    records_valid = len(records) == len(ALLOWED_JOBS)
    for job_id, display_name in ALLOWED_JOBS:
        job = records.get(job_id)
        if job is not None:
            for key in ("last_run_at", "next_run_at"):
                if job.get(key) is not None and _rfc3339(job.get(key)) is None:
                    records_valid = False
        result.append(
            {
                "id": job_id,
                "name": display_name,
                "enabled": bool(job is not None and _job_enabled(job)),
                "lastRun": _rfc3339(job.get("last_run_at")) if job else None,
                "lastStatus": _normalized_job_status(job, generated_at=generated_at),
                "nextRun": _rfc3339(job.get("next_run_at")) if job else None,
            }
        )
    return result, loaded and records_valid


def _deployed_revision(project_root: Path | None = None) -> str | None:
    root = project_root or Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return None
    try:
        tracked_changes = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if tracked_changes.stdout.strip():
            return None
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = completed.stdout.strip().lower()
    if not (40 <= len(revision) <= 64) or any(
        character not in "0123456789abcdef" for character in revision
    ):
        return None
    return revision


def build(
    locations: Sequence[Location],
    *,
    generated_at: datetime | None = None,
    database: Path | None = None,
    jobs_file: Path | None = None,
    project_root: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Build schema v1 and indicate whether both read-only inputs were valid."""
    now = generated_at or utcnow()
    if now.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    now = now.astimezone(timezone.utc)
    root = project_root or Path(__file__).resolve().parent.parent
    default_database = db_path() if os.environ.get("MUSHROOM_DB") else root / DEFAULT_DB

    favorable, last_evaluation, weather_age, database_ok = _database_health(
        database or default_database, locations, now
    )
    jobs, scheduler_ok = _scheduler_health(jobs_file or _jobs_path(), now)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "locationsCount": len(locations),
        "favorableLocationsCount": favorable,
        "lastEvaluationAt": last_evaluation,
        "weatherDataAgeSeconds": weather_age,
        "deployedRevision": _deployed_revision(root),
        "jobs": jobs,
    }
    return payload, database_ok and scheduler_ok
