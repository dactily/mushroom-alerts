"""SQLite state: readings, forecast releases, cache, and emission log.

Path comes from ``$MUSHROOM_DB``, default ``./state.sqlite``.  Everything is
plain stdlib ``sqlite3``; the file is also what Metabase will read (PLAN §2).

Tables
------
``readings``
    One row per (source, location, date, metric, issued).  ``issued`` is
    ``''`` for observations and the ISO issue date for forecasts -- an empty
    string rather than NULL so the UNIQUE constraint actually dedupes.
``locations``
    Derived-parameter cache keyed by slug (ČHMÚ pixel, HoubyMapa cell,
    nearest stations) -- PLAN §2a.  ``locations.yaml`` stays coordinates-only.
``notifications``
    Legacy emission log retained for rollback compatibility.
``signal_emissions``
    Signals written to stdout by ``check``.  This is not delivery proof;
    Hermes owns transport outcomes.
``forecasts``
    Rolling forecast archive: what we predicted on day X for day Y, so the
    forecast error by horizon can be measured later (PLAN §3 trigger 4,
    PLAN §5).  ``upsert_readings`` mirrors forecast readings in here
    automatically.
``forecast_runs`` / ``forecast_points``
    Immutable forecast releases. Schema v3 gives new runs a content hash and
    deterministic UUID so an identical retry reuses the original release.
    Older rows keep a NULL hash and remain readable.
``reports``
    One row per rendered Hermes report (date, mode).  It holds the compact
    state the send/silent decision compares against, so continuity lives in
    SQLite rather than in an agent's notepad (PLAN §2b).  Schema v5 adds
    ``chances_json``: the send rule compares percentages (PLAN §9d), while
    the verdicts stay for the debugging view.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .base import Location, Reading, utcnow

__all__ = ["Store", "db_path", "DEFAULT_DB", "SCHEMA_VERSION"]

DEFAULT_DB = "state.sqlite"
SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,
    location   TEXT NOT NULL,
    date       TEXT NOT NULL,
    metric     TEXT NOT NULL,
    issued     TEXT NOT NULL DEFAULT '',
    value      REAL NOT NULL,
    text       TEXT,
    meta_json  TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE (source, location, date, metric, issued)
);
CREATE INDEX IF NOT EXISTS readings_lookup
    ON readings (location, source, metric, date);

CREATE TABLE IF NOT EXISTS locations (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    location   TEXT NOT NULL,
    trigger    TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_lookup
    ON notifications (location, trigger, date);

CREATE TABLE IF NOT EXISTS forecasts (
    id          INTEGER PRIMARY KEY,
    issued      TEXT NOT NULL,
    source      TEXT NOT NULL,
    location    TEXT NOT NULL,
    target_date TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    UNIQUE (issued, source, location, target_date, metric)
);
CREATE INDEX IF NOT EXISTS forecasts_lookup
    ON forecasts (location, source, metric, target_date);
"""

MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS forecast_runs (
    run_id                  TEXT PRIMARY KEY,
    source                  TEXT NOT NULL,
    retrieved_at            TEXT NOT NULL,
    upstream_issued_at      TEXT,
    model                   TEXT,
    calculation_version     TEXT,
    status                  TEXT NOT NULL DEFAULT 'ok',
    input_run_ids_json      TEXT NOT NULL DEFAULT '[]',
    meta_json               TEXT,
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS forecast_runs_lookup
    ON forecast_runs (source, retrieved_at);

CREATE TABLE IF NOT EXISTS forecast_points (
    run_id       TEXT NOT NULL REFERENCES forecast_runs(run_id),
    source       TEXT NOT NULL,
    location     TEXT NOT NULL,
    target_date  TEXT NOT NULL,
    metric       TEXT NOT NULL,
    value        REAL NOT NULL,
    meta_json    TEXT,
    PRIMARY KEY (run_id, location, target_date, metric)
);
CREATE INDEX IF NOT EXISTS forecast_points_lookup
    ON forecast_points (location, source, metric, target_date, run_id);
"""

MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS signal_emissions (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL,
    location     TEXT NOT NULL,
    trigger      TEXT NOT NULL,
    emission_key TEXT NOT NULL,
    text_json    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (date, location, trigger, emission_key)
);
CREATE INDEX IF NOT EXISTS signal_emissions_lookup
    ON signal_emissions (location, trigger, date);
"""

MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS reports (
    id             INTEGER PRIMARY KEY,
    date           TEXT NOT NULL,
    mode           TEXT NOT NULL,
    verdicts_json  TEXT NOT NULL DEFAULT '{}',
    chances_json   TEXT NOT NULL DEFAULT '{}',
    candidates_json TEXT NOT NULL DEFAULT '{}',
    events_json    TEXT NOT NULL DEFAULT '{}',
    error_class    TEXT NOT NULL DEFAULT 'none',
    rules_version  TEXT NOT NULL DEFAULT '',
    sent           INTEGER NOT NULL DEFAULT 0,
    reason         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    UNIQUE (date, mode)
);
CREATE INDEX IF NOT EXISTS reports_lookup ON reports (mode, date);
"""

MIGRATION_3_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS forecast_runs_content
    ON forecast_runs (source, content_hash)
    WHERE content_hash IS NOT NULL;
"""


def db_path() -> Path:
    """Where the SQLite file lives (``$MUSHROOM_DB`` or ``./state.sqlite``)."""
    return Path(os.environ.get("MUSHROOM_DB") or DEFAULT_DB)


def _iso(value: date | datetime | str) -> str:
    if isinstance(value, str):
        return value
    return value.isoformat()


class Store:
    """Thin, synchronous wrapper around the SQLite file.  Usable as a CM."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # readings
    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version < 1:
            self.conn.executescript(MIGRATION_1)
            self._backfill_legacy_forecasts()
            self.conn.execute("PRAGMA user_version=1")
            version = 1
        if version < 2:
            self.conn.executescript(MIGRATION_2)
            self._backfill_notifications()
            self.conn.execute("PRAGMA user_version=2")
            version = 2
        if version < 3:
            columns = {
                str(row["name"])
                for row in self.conn.execute("PRAGMA table_info(forecast_runs)")
            }
            if "content_hash" not in columns:
                self.conn.execute(
                    "ALTER TABLE forecast_runs ADD COLUMN content_hash TEXT"
                )
            self.conn.executescript(MIGRATION_3_INDEX)
            self.conn.execute("PRAGMA user_version=3")
            version = 3
        if version < 4:
            self.conn.executescript(MIGRATION_4)
            self.conn.execute("PRAGMA user_version=4")
            version = 4
        if version < 5:
            columns = {
                str(row["name"])
                for row in self.conn.execute("PRAGMA table_info(reports)")
            }
            if "chances_json" not in columns:
                self.conn.execute(
                    "ALTER TABLE reports ADD COLUMN chances_json "
                    "TEXT NOT NULL DEFAULT '{}'"
                )
            self.conn.execute("PRAGMA user_version=5")

    def _backfill_notifications(self) -> None:
        for row in self.conn.execute("SELECT * FROM notifications ORDER BY id"):
            try:
                payload = json.loads(row["text"])
                emission_key = str(payload.get("key") or row["text"])
            except (json.JSONDecodeError, TypeError, AttributeError):
                emission_key = str(row["text"])
            self.conn.execute(
                """INSERT OR IGNORE INTO signal_emissions
                   (date, location, trigger, emission_key, text_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    row["date"],
                    row["location"],
                    row["trigger"],
                    emission_key,
                    row["text"],
                    row["created_at"],
                ),
            )

    def _backfill_legacy_forecasts(self) -> None:
        groups = self.conn.execute(
            "SELECT DISTINCT source, issued FROM forecasts ORDER BY source, issued"
        ).fetchall()
        created_at = utcnow().isoformat()
        for group in groups:
            source, issued = str(group["source"]), str(group["issued"])
            run_id = f"legacy:{source}:{issued}"
            retrieved_at = f"{issued}T00:00:00+00:00"
            self.conn.execute(
                """INSERT OR IGNORE INTO forecast_runs
                   (run_id, source, retrieved_at, calculation_version, status,
                    input_run_ids_json, meta_json, created_at)
                   VALUES (?, ?, ?, 'legacy', 'ok', '[]', '{\"legacy\": true}', ?)""",
                (run_id, source, retrieved_at, created_at),
            )
            self.conn.execute(
                """INSERT OR IGNORE INTO forecast_points
                   (run_id, source, location, target_date, metric, value, meta_json)
                   SELECT ?, source, location, target_date, metric, value,
                          '{\"legacy\": true}'
                   FROM forecasts WHERE source=? AND issued=?""",
                (run_id, source, issued),
            )

    def upsert_readings(
        self,
        readings: Iterable[Reading],
        *,
        retrieved_at: datetime | None = None,
        calculation_version: str | None = None,
        input_run_ids: Sequence[str] = (),
    ) -> int:
        """Insert or replace readings in place.  Returns the row count.

        Idempotent: the same reading written twice updates one row.  Forecast
        readings (``meta["issued"]`` set) are mirrored into ``forecasts``.
        """
        rows = list(readings)
        if not rows:
            return 0
        self._archive_forecast_groups(
            rows,
            retrieved_at=retrieved_at,
            calculation_version=calculation_version,
            input_run_ids=input_run_ids,
        )
        now = utcnow().isoformat()
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO readings
                    (source, location, date, metric, issued, value, text, meta_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, location, date, metric, issued) DO UPDATE SET
                    value = excluded.value,
                    text = excluded.text,
                    meta_json = excluded.meta_json,
                    fetched_at = excluded.fetched_at
                """,
                [
                    (
                        r.source,
                        r.location,
                        _iso(r.date),
                        r.metric,
                        r.issued,
                        float(r.value),
                        r.text,
                        r.meta_json(),
                        now,
                    )
                    for r in rows
                ],
            )
        forecasts = [r for r in rows if r.is_forecast]
        if forecasts:
            self.upsert_forecasts(forecasts)
        return len(rows)

    def _archive_forecast_groups(
        self,
        rows: Sequence[Reading],
        *,
        retrieved_at: datetime | None,
        calculation_version: str | None,
        input_run_ids: Sequence[str],
    ) -> None:
        """Archive complete model/derived runs, including their current day."""
        grouped: dict[str, list[Reading]] = {}
        for reading in rows:
            if reading.source in {"openmeteo", "api30_forecast"} or reading.is_forecast:
                grouped.setdefault(reading.source, []).append(reading)
        if not grouped:
            return
        stamp = (retrieved_at or utcnow()).isoformat()
        for source, points in grouped.items():
            first_meta = next((r.meta for r in points if r.meta), {}) or {}
            upstream_issued_at = first_meta.get("upstream_issued_at")
            model = first_meta.get("model")
            version = calculation_version or first_meta.get("calculation_version")
            inputs = sorted(set(input_run_ids))
            content_hash = self._forecast_content_hash(
                source,
                points,
                calculation_version=version,
                input_run_ids=inputs,
            )
            existing = self.conn.execute(
                """SELECT run_id, retrieved_at FROM forecast_runs
                   WHERE source=? AND content_hash=?""",
                (source, content_hash),
            ).fetchone()
            if existing is not None:
                self._bind_forecast_run(
                    points,
                    run_id=str(existing["run_id"]),
                    retrieved_at=str(existing["retrieved_at"]),
                    calculation_version=version,
                )
                continue

            run_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"mushroom-alerts:forecast:{source}:{content_hash}",
                )
            )
            run_meta = {
                "locations": sorted({r.location for r in points}),
                "point_count": len(points),
            }
            with self.conn:
                inserted = self.conn.execute(
                    """INSERT OR IGNORE INTO forecast_runs
                       (run_id, source, retrieved_at, upstream_issued_at, model,
                        calculation_version, status, input_run_ids_json,
                        meta_json, created_at, content_hash)
                       VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?)""",
                    (
                        run_id,
                        source,
                        stamp,
                        upstream_issued_at,
                        model,
                        version,
                        json.dumps(inputs),
                        json.dumps(run_meta, ensure_ascii=False, sort_keys=True),
                        utcnow().isoformat(),
                        content_hash,
                    ),
                )
                if not inserted.rowcount:
                    concurrent = self.conn.execute(
                        """SELECT run_id, retrieved_at FROM forecast_runs
                           WHERE source=? AND content_hash=?""",
                        (source, content_hash),
                    ).fetchone()
                    if concurrent is None:
                        raise RuntimeError("forecast run identity collision")
                    self._bind_forecast_run(
                        points,
                        run_id=str(concurrent["run_id"]),
                        retrieved_at=str(concurrent["retrieved_at"]),
                        calculation_version=version,
                    )
                    continue
                self._bind_forecast_run(
                    points,
                    run_id=run_id,
                    retrieved_at=stamp,
                    calculation_version=version,
                )
                for reading in points:
                    self.conn.execute(
                        """INSERT INTO forecast_points
                           (run_id, source, location, target_date, metric, value, meta_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            run_id,
                            source,
                            reading.location,
                            _iso(reading.date),
                            reading.metric,
                            float(reading.value),
                            reading.meta_json(),
                        ),
                    )

    @staticmethod
    def _forecast_content_hash(
        source: str,
        points: Sequence[Reading],
        *,
        calculation_version: str | None,
        input_run_ids: Sequence[str],
    ) -> str:
        """Stable identity of a complete forecast release.

        Retrieval time and the run identity injected by a previous upsert are
        operational metadata, not forecast content.
        """
        canonical_points = []
        for reading in points:
            meta = {
                key: value
                for key, value in (reading.meta or {}).items()
                if key not in {"run_id", "retrieved_at", "calculation_version"}
            }
            canonical_points.append(
                {
                    "location": reading.location,
                    "date": _iso(reading.date),
                    "metric": reading.metric,
                    "value": float(reading.value),
                    "meta": meta,
                }
            )
        payload = {
            "source": source,
            "calculation_version": calculation_version,
            "input_run_ids": sorted(set(input_run_ids)),
            "points": sorted(
                canonical_points,
                key=lambda item: (
                    item["location"],
                    item["date"],
                    item["metric"],
                    json.dumps(
                        item["meta"],
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                ),
            ),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _bind_forecast_run(
        points: Sequence[Reading],
        *,
        run_id: str,
        retrieved_at: str,
        calculation_version: str | None,
    ) -> None:
        for reading in points:
            meta = dict(reading.meta or {})
            meta["run_id"] = run_id
            meta["retrieved_at"] = retrieved_at
            if calculation_version:
                meta["calculation_version"] = calculation_version
            reading.meta = meta

    def get_reading(
        self, source: str, location: str, metric: str, day: date | str
    ) -> Reading | None:
        """The observation for one specific day (issued = '')."""
        row = self.conn.execute(
            """SELECT * FROM readings
               WHERE source=? AND location=? AND metric=? AND date=? AND issued=''""",
            (source, location, metric, _iso(day)),
        ).fetchone()
        return _to_reading(row)

    def latest(
        self,
        source: str,
        location: str,
        metric: str,
        before: date | str | None = None,
    ) -> Reading | None:
        """Most recent observation, optionally strictly before ``before``."""
        sql = """SELECT * FROM readings
                 WHERE source=? AND location=? AND metric=? AND issued=''"""
        args: list[Any] = [source, location, metric]
        if before is not None:
            sql += " AND date < ?"
            args.append(_iso(before))
        sql += " ORDER BY date DESC LIMIT 1"
        return _to_reading(self.conn.execute(sql, args).fetchone())

    def series(
        self,
        source: str,
        location: str,
        metric: str,
        since: date | str | None = None,
        until: date | str | None = None,
    ) -> list[Reading]:
        """Observations in ``[since, until]``, oldest first."""
        sql = """SELECT * FROM readings
                 WHERE source=? AND location=? AND metric=? AND issued=''"""
        args: list[Any] = [source, location, metric]
        if since is not None:
            sql += " AND date >= ?"
            args.append(_iso(since))
        if until is not None:
            sql += " AND date <= ?"
            args.append(_iso(until))
        sql += " ORDER BY date ASC"
        out = [_to_reading(r) for r in self.conn.execute(sql, args)]
        return [r for r in out if r is not None]

    def latest_snapshot(self, location: str) -> list[Reading]:
        """Newest observation of every (source, metric) for one location."""
        rows = self.conn.execute(
            """
            SELECT r.* FROM readings r
            JOIN (SELECT source, metric, MAX(date) AS d FROM readings
                  WHERE location=? AND issued='' GROUP BY source, metric) m
              ON r.source=m.source AND r.metric=m.metric AND r.date=m.d
            WHERE r.location=? AND r.issued=''
            ORDER BY r.source, r.metric
            """,
            (location, location),
        )
        return [r for r in (_to_reading(x) for x in rows) if r is not None]

    # ------------------------------------------------------------------
    # locations / derived params cache
    # ------------------------------------------------------------------
    def sync_location(self, location: Location) -> None:
        """Make sure the row exists and name/coords match ``locations.yaml``.

        If the coordinates moved, the cached params are dropped -- they were
        derived from the old ones.
        """
        row = self.conn.execute(
            "SELECT lat, lon FROM locations WHERE slug=?", (location.slug,)
        ).fetchone()
        moved = row is not None and (
            abs(row["lat"] - location.lat) > 1e-9 or abs(row["lon"] - location.lon) > 1e-9
        )
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO locations (slug, name, lat, lon, params_json, updated_at)
                VALUES (?, ?, ?, ?, '{}', ?)
                ON CONFLICT (slug) DO UPDATE SET
                    name = excluded.name, lat = excluded.lat, lon = excluded.lon,
                    params_json = CASE WHEN ? THEN '{}' ELSE locations.params_json END,
                    updated_at = excluded.updated_at
                """,
                (
                    location.slug,
                    location.name,
                    location.lat,
                    location.lon,
                    utcnow().isoformat(),
                    1 if moved else 0,
                ),
            )

    def get_params(self, slug: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT params_json FROM locations WHERE slug=?", (slug,)
        ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row["params_json"] or "{}")
        except json.JSONDecodeError:
            return {}

    def set_params(self, slug: str, params: dict[str, Any]) -> None:
        """Merge ``params`` into the cached derived parameters for ``slug``."""
        merged = self.get_params(slug)
        merged.update(params)
        blob = json.dumps(merged, ensure_ascii=False, sort_keys=True, default=str)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE locations SET params_json=?, updated_at=? WHERE slug=?",
                (blob, utcnow().isoformat(), slug),
            )
            if cur.rowcount == 0:
                self.conn.execute(
                    """INSERT INTO locations (slug, name, lat, lon, params_json, updated_at)
                       VALUES (?, ?, 0, 0, ?, ?)""",
                    (slug, slug, blob, utcnow().isoformat()),
                )

    def invalidate_params(self, slug: str | None = None) -> None:
        """Drop the derived-params cache for one location (or all of them)."""
        with self.conn:
            if slug is None:
                self.conn.execute("UPDATE locations SET params_json='{}'")
            else:
                self.conn.execute(
                    "UPDATE locations SET params_json='{}' WHERE slug=?", (slug,)
                )

    def forget_location(self, slug: str) -> None:
        """Remove the cache row for a deleted location (readings are kept)."""
        with self.conn:
            self.conn.execute("DELETE FROM locations WHERE slug=?", (slug,))

    # ------------------------------------------------------------------
    # notifications
    # ------------------------------------------------------------------
    def add_notification(
        self, day: date | str, location: str, trigger: str, text: str
    ) -> int:
        try:
            payload = json.loads(text)
            emission_key = str(payload.get("key") or text)
        except (json.JSONDecodeError, TypeError, AttributeError):
            emission_key = text
        created_at = utcnow().isoformat()
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO notifications (date, location, trigger, text, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (_iso(day), location, trigger, text, created_at),
            )
            self.conn.execute(
                """INSERT OR IGNORE INTO signal_emissions
                   (date, location, trigger, emission_key, text_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_iso(day), location, trigger, emission_key, text, created_at),
            )
        return int(cur.lastrowid or 0)

    def last_notification(
        self, location: str, trigger: str | None = None
    ) -> sqlite3.Row | None:
        """Most recent notification row, or ``None``.  Keys: date, text, ..."""
        sql = "SELECT * FROM notifications WHERE location=?"
        args: list[Any] = [location]
        if trigger is not None:
            sql += " AND trigger=?"
            args.append(trigger)
        sql += " ORDER BY date DESC, id DESC LIMIT 1"
        return self.conn.execute(sql, args).fetchone()

    def add_emission(
        self,
        day: date | str,
        location: str,
        trigger: str,
        emission_key: str,
        text_json: str,
    ) -> bool:
        """Record stdout emission, not confirmed transport delivery.

        The legacy row is dual-written so an application rollback retains
        antispam continuity.  Returns ``True`` only for a new emission.
        """
        created_at = utcnow().isoformat()
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO signal_emissions
                   (date, location, trigger, emission_key, text_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_iso(day), location, trigger, emission_key, text_json, created_at),
            )
            inserted = bool(cur.rowcount)
            if inserted:
                self.conn.execute(
                    """INSERT INTO notifications
                       (date, location, trigger, text, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (_iso(day), location, trigger, text_json, created_at),
                )
        return inserted

    def last_emission(
        self,
        location: str,
        trigger: str | None = None,
        emission_key: str | None = None,
    ) -> sqlite3.Row | None:
        sql = "SELECT * FROM signal_emissions WHERE location=?"
        args: list[Any] = [location]
        if trigger is not None:
            sql += " AND trigger=?"
            args.append(trigger)
        if emission_key is not None:
            sql += " AND emission_key=?"
            args.append(emission_key)
        sql += " ORDER BY date DESC, id DESC LIMIT 1"
        return self.conn.execute(sql, args).fetchone()

    # ------------------------------------------------------------------
    # reports: the state the send/silent decision compares against
    # ------------------------------------------------------------------
    def save_report(
        self,
        day: date | str,
        mode: str,
        *,
        chances: dict[str, Any],
        verdicts: dict[str, Any],
        candidates: dict[str, Any],
        events: dict[str, Any],
        error_class: str,
        rules_version: str,
        sent: bool,
        reason: str,
    ) -> None:
        """Write (or replace) the report row for one ``(date, mode)`` pair.

        Replacing keeps a same-day re-run idempotent: the decision itself is
        taken against the last report from an *earlier* date, so re-running
        never invents a change out of its own previous row.
        """
        with self.conn:
            self.conn.execute(
                """INSERT INTO reports
                   (date, mode, chances_json, verdicts_json, candidates_json,
                    events_json, error_class, rules_version, sent, reason,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (date, mode) DO UPDATE SET
                    chances_json=excluded.chances_json,
                    verdicts_json=excluded.verdicts_json,
                    candidates_json=excluded.candidates_json,
                    events_json=excluded.events_json,
                    error_class=excluded.error_class,
                    rules_version=excluded.rules_version,
                    sent=excluded.sent,
                    reason=excluded.reason,
                    created_at=excluded.created_at""",
                (
                    _iso(day),
                    mode,
                    json.dumps(chances, ensure_ascii=False, sort_keys=True),
                    json.dumps(verdicts, ensure_ascii=False, sort_keys=True),
                    json.dumps(candidates, ensure_ascii=False, sort_keys=True),
                    json.dumps(events, ensure_ascii=False, sort_keys=True),
                    error_class,
                    rules_version,
                    1 if sent else 0,
                    reason,
                    utcnow().isoformat(),
                ),
            )

    def last_report(
        self, mode: str, *, before: date | str | None = None
    ) -> sqlite3.Row | None:
        """Newest report for ``mode``, optionally strictly before a date."""
        sql = "SELECT * FROM reports WHERE mode=?"
        args: list[Any] = [mode]
        if before is not None:
            sql += " AND date < ?"
            args.append(_iso(before))
        sql += " ORDER BY date DESC, id DESC LIMIT 1"
        return self.conn.execute(sql, args).fetchone()

    # ------------------------------------------------------------------
    # forecasts
    # ------------------------------------------------------------------
    def upsert_forecasts(self, readings: Sequence[Reading]) -> int:
        """Archive forecast readings.  Skips readings without ``issued``."""
        rows = [r for r in readings if r.is_forecast]
        if not rows:
            return 0
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO forecasts (issued, source, location, target_date, metric, value)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (issued, source, location, target_date, metric)
                DO UPDATE SET value = excluded.value
                """,
                [
                    (
                        r.issued,
                        r.source,
                        r.location,
                        _iso(r.date),
                        r.metric,
                        float(r.value),
                    )
                    for r in rows
                ],
            )
        return len(rows)

    def latest_issue(self, source: str, location: str, metric: str) -> str | None:
        """ISO date of the newest forecast run we have, or ``None``."""
        row = self.conn.execute(
            """SELECT MAX(issued) AS issued FROM forecasts
               WHERE source=? AND location=? AND metric=?""",
            (source, location, metric),
        ).fetchone()
        return row["issued"] if row and row["issued"] else None

    def forecast_series(
        self, source: str, location: str, metric: str, issued: date | str
    ) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM forecasts
                   WHERE source=? AND location=? AND metric=? AND issued=?
                   ORDER BY target_date""",
                (source, location, metric, _iso(issued)),
            )
        )

    def latest_forecast_run(
        self, source: str, location: str, metric: str | None = None
    ) -> sqlite3.Row | None:
        sql = """SELECT DISTINCT fr.* FROM forecast_runs fr
                 JOIN forecast_points fp ON fp.run_id=fr.run_id
                 WHERE fr.source=? AND fp.location=?"""
        args: list[Any] = [source, location]
        if metric is not None:
            sql += " AND fp.metric=?"
            args.append(metric)
        sql += " ORDER BY fr.retrieved_at DESC, fr.created_at DESC LIMIT 1"
        return self.conn.execute(sql, args).fetchone()

    def get_forecast_run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM forecast_runs WHERE run_id=?", (run_id,)
        ).fetchone()

    def forecast_run_points(
        self,
        run_id: str,
        *,
        location: str | None = None,
        metric: str | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM forecast_points WHERE run_id=?"
        args: list[Any] = [run_id]
        if location is not None:
            sql += " AND location=?"
            args.append(location)
        if metric is not None:
            sql += " AND metric=?"
            args.append(metric)
        sql += " ORDER BY target_date, metric"
        return list(self.conn.execute(sql, args))

    def forecast_error_pairs(
        self,
        location: str,
        forecast_source: str,
        forecast_metric: str,
        actual_source: str,
        actual_metric: str,
    ) -> list[sqlite3.Row]:
        """Archived predictions paired with later observations."""
        return list(
            self.conn.execute(
                """SELECT fr.run_id, fr.retrieved_at, fp.target_date,
                          fp.value AS predicted, actual.value AS actual
                   FROM forecast_runs fr
                   JOIN forecast_points fp ON fp.run_id=fr.run_id
                   JOIN readings actual
                     ON actual.location=fp.location
                    AND actual.date=fp.target_date
                    AND actual.issued=''
                    AND actual.source=?
                    AND actual.metric=?
                   WHERE fr.source=? AND fr.status='ok'
                     AND fp.location=? AND fp.metric=?
                   ORDER BY fr.retrieved_at, fp.target_date""",
                (
                    actual_source,
                    actual_metric,
                    forecast_source,
                    location,
                    forecast_metric,
                ),
            )
        )


def _to_reading(row: sqlite3.Row | None) -> Reading | None:
    if row is None:
        return None
    meta = None
    if row["meta_json"]:
        try:
            meta = json.loads(row["meta_json"])
        except json.JSONDecodeError:
            meta = None
    return Reading(
        source=row["source"],
        location=row["location"],
        date=date.fromisoformat(row["date"]),
        metric=row["metric"],
        value=row["value"],
        text=row["text"],
        meta=meta,
    )
