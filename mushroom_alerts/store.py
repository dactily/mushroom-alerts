"""SQLite state: readings history, derived params cache, notification log.

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
    What we already told the user, for the antispam / state-transition rules.
``forecasts``
    Rolling forecast archive: what we predicted on day X for day Y, so the
    forecast error by horizon can be measured later (PLAN §3 trigger 4,
    PLAN §5).  ``upsert_readings`` mirrors forecast readings in here
    automatically.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .base import Location, Reading, utcnow

__all__ = ["Store", "db_path", "DEFAULT_DB"]

DEFAULT_DB = "state.sqlite"

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
    def upsert_readings(self, readings: Iterable[Reading]) -> int:
        """Insert or replace readings in place.  Returns the row count.

        Idempotent: the same reading written twice updates one row.  Forecast
        readings (``meta["issued"]`` set) are mirrored into ``forecasts``.
        """
        rows = list(readings)
        if not rows:
            return 0
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
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO notifications (date, location, trigger, text, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (_iso(day), location, trigger, text, utcnow().isoformat()),
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
