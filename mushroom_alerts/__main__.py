"""CLI: ``python -m mushroom_alerts check|status|add|list|del``.

Exit-code contract for Hermes (PLAN §2)::

    0   nothing to say, stay silent
    10  positive signal -- forward stdout to Telegram verbatim
    1   error: the script itself crashed, or *every* source failed

A single dead source is never an error: it becomes a note in the report and
the other sources carry on (PLAN §6).

``check`` hands the fetch results to ``rules.decide`` when a ``rules``
module exists; until wave B lands it falls back to printing one line per
location and exiting 0.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json as jsonlib
import pkgutil
import sys
import traceback
from datetime import date
from typing import Any, Iterable, Sequence

from .base import (
    EXIT_ERROR,
    EXIT_SIGNAL,
    EXIT_SILENT,
    Decision,
    FetchResult,
    Location,
    Reading,
    soft_fetch,
)
from .http import Http
from .locations import (
    add_location,
    load_locations,
    locations_path,
    remove_location,
)
from .store import Store

#: Preferred run order; modules not listed run afterwards, alphabetically.
FETCH_ORDER = (
    "fetch_chmi_map",
    "fetch_houbymapa",
    "fetch_chmi_station",
    "fetch_openmeteo",
    "api30",
)

#: How a source is labelled in the human-readable report.
SOURCE_LABELS = {
    "chmi_map": "ČHMÚ",
    "houbymapa": "HoubyMapa",
    "chmi_station": "stanice",
    "openmeteo": "Open-Meteo",
    "api30_forecast": "API30",
}


# ----------------------------------------------------------------------
# fetcher discovery
# ----------------------------------------------------------------------
def discover_fetchers(only: Sequence[str] | None = None) -> list[Any]:
    """Import every available ``fetch_*`` module (plus ``api30`` if it is a
    fetcher too).  Modules that are absent or fail to import are skipped --
    other agents add theirs later, and the CLI must not care.

    ``only`` filters by source id (``chmi_map``) or module name
    (``fetch_chmi_map``); an unknown name simply matches nothing.
    """
    package = __package__ or __name__.rsplit(".", 1)[0]
    names = {
        m.name
        for m in pkgutil.iter_modules(importlib.import_module(package).__path__)
        if m.name.startswith("fetch_") or m.name == "api30"
    }
    ordered = [n for n in FETCH_ORDER if n in names]
    ordered += sorted(names - set(ordered))

    wanted = {s.strip().lower() for s in only} if only else None
    modules: list[Any] = []
    for name in ordered:
        try:
            mod = importlib.import_module(f"{package}.{name}")
        except Exception:  # noqa: BLE001 - not written yet / broken deps
            continue
        if not callable(getattr(mod, "fetch", None)):
            continue
        source = getattr(mod, "SOURCE", name)
        if wanted is not None and not (
            source.lower() in wanted
            or name.lower() in wanted
            or name.removeprefix("fetch_").lower() in wanted
        ):
            continue
        modules.append(mod)
    return modules


def run_fetchers(
    locations: list[Location],
    *,
    http: Http,
    today: date,
    only: Sequence[str] | None = None,
) -> list[FetchResult]:
    results = []
    for mod in discover_fetchers(only):
        source = getattr(mod, "SOURCE", None) or getattr(mod, "__name__", "unknown")
        results.append(soft_fetch(source, mod.fetch, locations, http=http, today=today))
    return results


def cache_params(store: Store, results: Iterable[FetchResult]) -> None:
    """Harvest derived parameters out of reading meta into the cache (§2a)."""
    harvest = {
        "chmi_map": {"px": "chmi_px"},
        "houbymapa": {"cell": "houbymapa_cell", "distance_km": "houbymapa_distance_km"},
        "chmi_station": {"station": "chmi_station", "stations": "chmi_stations"},
    }
    collected: dict[str, dict[str, Any]] = {}
    for result in results:
        keys = harvest.get(result.source)
        if not keys:
            continue
        for reading in result.readings:
            if not reading.meta:
                continue
            for src_key, dst_key in keys.items():
                if src_key in reading.meta:
                    collected.setdefault(reading.location, {})[dst_key] = reading.meta[src_key]
    for slug, params in collected.items():
        store.set_params(slug, params)


# ----------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------
def _fmt_value(reading: Reading) -> str:
    if reading.metric == "level":
        return f"{int(reading.value)}/5"
    if reading.metric == "score":
        return f"{reading.value:.2f}"
    if reading.metric.endswith("_mm"):
        return f"{reading.value:.0f} mm"
    if reading.metric.startswith("t_"):
        return f"{reading.value:.1f} °C"
    return f"{reading.value:g}"


def _fmt_segment(source: str, readings: list[Reading]) -> str:
    label = SOURCE_LABELS.get(source, source)
    parts = []
    for r in sorted(readings, key=lambda r: r.metric):
        if r.metric == "level":
            parts.append(_fmt_value(r))
        else:
            parts.append(f"{r.metric} {_fmt_value(r)}")
    flag = " (starý)" if any((r.meta or {}).get("stale") for r in readings) else ""
    return f"{label} {' '.join(parts)}{flag}"


def format_snapshot(location: Location, readings: list[Reading]) -> str:
    """PLAN §3 shape: one line per location, one segment per source."""
    by_source: dict[str, list[Reading]] = {}
    for r in readings:
        by_source.setdefault(r.source, []).append(r)
    if not by_source:
        return f"🍄 {location.name}: žádná data"
    order = [s for s in SOURCE_LABELS if s in by_source] + [
        s for s in sorted(by_source) if s not in SOURCE_LABELS
    ]
    segments = [_fmt_segment(s, by_source[s]) for s in order]
    return f"🍄 {location.name}: " + ", ".join(segments)


def _reading_dict(r: Reading) -> dict[str, Any]:
    return {
        "source": r.source,
        "location": r.location,
        "date": r.date.isoformat(),
        "metric": r.metric,
        "value": r.value,
        "text": r.text,
        "meta": r.meta,
    }


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    today = date.today()
    locations = load_locations()
    if not locations:
        print(f"no locations configured ({locations_path()})", file=sys.stderr)
        return EXIT_ERROR

    only = args.only.split(",") if args.only else None
    with Http() as http, Store() as store:
        for loc in locations:
            store.sync_location(loc)
        results = run_fetchers(locations, http=http, today=today, only=only)
        if not results:
            print("no fetchers available", file=sys.stderr)
            return EXIT_ERROR
        for result in results:
            store.upsert_readings(result.readings)
        cache_params(store, results)

        failures = [r for r in results if not r.ok]
        notes = [f"{SOURCE_LABELS.get(r.source, r.source)}: {r.error}" for r in results if r.error]
        all_failed = len(failures) == len(results)

        decision = _decide(locations, results, store=store, today=today)
        if decision is None:  # no rules module yet -> plain snapshot
            lines = [
                format_snapshot(loc, [r for res in results for r in res.for_location(loc.slug)])
                for loc in locations
            ]
            decision = Decision(
                exit_code=EXIT_ERROR if all_failed else EXIT_SILENT,
                text="" if all_failed else "\n".join(lines),
            )
        elif all_failed:
            decision.exit_code = EXIT_ERROR

        if args.json:
            print(
                jsonlib.dumps(
                    {
                        "date": today.isoformat(),
                        "exit_code": decision.exit_code,
                        "text": decision.text,
                        "notes": notes,
                        "sources": [
                            {
                                "source": r.source,
                                "ok": r.ok,
                                "error": r.error,
                                "fetched_at": r.fetched_at.isoformat(),
                                "readings": [_reading_dict(x) for x in r.readings],
                            }
                            for r in results
                        ],
                        "decision": decision.data,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            if decision.text:
                print(decision.text)
            # Source notes belong in the forwarded message only when there is
            # one; otherwise they are diagnostics.
            stream = sys.stdout if decision.exit_code == EXIT_SIGNAL else sys.stderr
            for note in notes:
                print(f"⚠ {note}", file=stream)
        return decision.exit_code


def _decide(
    locations: list[Location],
    results: list[FetchResult],
    *,
    store: Store,
    today: date,
) -> Decision | None:
    """Call ``rules.decide`` if the module exists.  ``None`` = not available."""
    package = __package__ or __name__.rsplit(".", 1)[0]
    try:
        if importlib.util.find_spec(f"{package}.rules") is None:
            return None
    except (ImportError, ValueError):
        return None
    try:
        rules = importlib.import_module(f"{package}.rules")
    except Exception:  # noqa: BLE001 - rules exists but is broken => error
        traceback.print_exc(file=sys.stderr)
        return Decision(exit_code=EXIT_ERROR, text="rules module failed to import")
    decide = getattr(rules, "decide", None)
    if not callable(decide):
        return None
    try:
        decision = decide(locations, results, store=store, today=today)
    except Exception:  # noqa: BLE001 - a broken rules module is an error
        traceback.print_exc(file=sys.stderr)
        return Decision(exit_code=EXIT_ERROR, text="rules.decide() failed")
    if not isinstance(decision, Decision):
        return Decision(exit_code=EXIT_ERROR, text="rules.decide() returned a non-Decision")
    return decision


def cmd_status(args: argparse.Namespace) -> int:
    """Print the last stored snapshot.  Never fetches -- ``check`` does that."""
    locations = load_locations()
    with Store() as store:
        payload = []
        lines = []
        for loc in locations:
            readings = store.latest_snapshot(loc.slug)
            lines.append(format_snapshot(loc, readings))
            payload.append(
                {
                    "location": loc.as_dict(),
                    "params": store.get_params(loc.slug),
                    "readings": [_reading_dict(r) for r in readings],
                }
            )
    if args.json:
        print(jsonlib.dumps({"locations": payload}, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines) if lines else "no locations configured")
    return EXIT_SILENT


def cmd_add(args: argparse.Namespace) -> int:
    try:
        loc = add_location(args.name, args.lat, args.lon)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    with Store() as store:
        store.sync_location(loc)
        store.invalidate_params(loc.slug)
    print(f"added {loc.name} [{loc.slug}] {loc.lat}, {loc.lon} -> {locations_path()}")
    return EXIT_SILENT


def cmd_list(args: argparse.Namespace) -> int:
    locations = load_locations()
    if args.json:
        with Store() as store:
            print(
                jsonlib.dumps(
                    [
                        {**loc.as_dict(), "params": store.get_params(loc.slug)}
                        for loc in locations
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
    elif not locations:
        print("no locations configured")
    else:
        for loc in locations:
            print(f"{loc.slug:24} {loc.name}  {loc.lat}, {loc.lon}")
    return EXIT_SILENT


def cmd_del(args: argparse.Namespace) -> int:
    try:
        loc = remove_location(args.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    with Store() as store:
        store.forget_location(loc.slug)
    print(f"removed {loc.name} [{loc.slug}]")
    return EXIT_SILENT


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mushroom_alerts",
        description="Mushroom growth alerts. Exit codes: 0 silent, 10 signal, 1 error.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="fetch every source, store, decide")
    p_check.add_argument("--json", action="store_true", help="machine-readable output")
    p_check.add_argument("--only", metavar="SOURCE,...", help="restrict to these sources")
    p_check.set_defaults(func=cmd_check)

    p_status = sub.add_parser("status", help="print the last stored snapshot")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_add = sub.add_parser("add", help="add a location to locations.yaml")
    p_add.add_argument("name")
    p_add.add_argument("lat", type=float)
    p_add.add_argument("lon", type=float)
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="list configured locations")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_del = sub.add_parser("del", help="remove a location from locations.yaml")
    p_del.add_argument("name")
    p_del.set_defaults(func=cmd_del)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_ERROR
    except Exception:  # noqa: BLE001 - the CLI itself crashed => exit 1
        traceback.print_exc(file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
