"""CLI: ``python -m mushroom_alerts check|brief|status|add|list|del``.

Exit-code contract for Hermes (PLAN §2)::

    0   nothing to say, stay silent
    10  positive signal emitted to stdout
    1   error: the script itself crashed, or *every* source failed

A single dead source is never an error: it becomes a note in the report and
the other sources carry on (PLAN §6).

``check`` fetches, stores, derives the API30 curve, evaluates the rules,
renders the result, and records only signals emitted to stdout.

``brief`` runs the very same pipeline (:func:`run_pipeline` + :func:`_decide`)
and then prints the facts at length instead of collapsing them: the debugging
view (PLAN §2b, ``mushroom_alerts/brief.py``). With ``--mode daily|weekend``
it prints instead the short ready-to-send block from ``report.py``, including
the send/silent flag computed against the stored previous report -- that is
what Hermes reads and only re-words (``hermes/PROMPT.md``). It exits ``1``
when every source failed or rule calculation failed; partial results remain
exit ``0``.  ``--location SLUG`` (repeatable) narrows either view to a few
locations; being partial, such a run never records the report row.

``status`` never fetches: it renders the shared location view from SQLite.
"""

from __future__ import annotations

import argparse
import importlib
import json as jsonlib
import os
import pkgutil
import sys
import traceback
from dataclasses import dataclass
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
    find_location,
    load_locations,
    locations_path,
    remove_location,
)
from . import report as report_lib
from . import rules as rules_lib
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
    "chmi_station": "станция",
    "openmeteo": "Open-Meteo",
    "api30_forecast": "API30",
}


# ----------------------------------------------------------------------
# fetcher discovery
# ----------------------------------------------------------------------
@dataclass(slots=True)
class _UnavailableFetcher:
    """Module-shaped fetcher that reports an import failure explicitly."""

    SOURCE: str
    detail: str

    def fetch(
        self, locations: list[Location], *, http: Any, today: date
    ) -> FetchResult:
        return FetchResult.failure(self.SOURCE, self.detail)


def discover_fetchers(only: Sequence[str] | None = None) -> list[Any]:
    """Import every available ``fetch_*`` module (plus ``api30`` if it is a
    fetcher too).  Modules that are absent or fail to import are skipped --
    other agents add theirs later, and the CLI must not care.

    ``only`` filters by source id (``chmi_map``) or module name
    (``fetch_chmi_map``); an unknown name simply matches nothing.  Import
    failures are represented by a module-shaped failed fetcher, rather than
    disappearing from the run without a diagnostic.
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
        source_hint = name.removeprefix("fetch_")
        if wanted is not None and not (
            source_hint.lower() in wanted or name.lower() in wanted
        ):
            continue
        try:
            mod = importlib.import_module(f"{package}.{name}")
        except Exception as exc:  # noqa: BLE001 - convert to source failure
            # api30 is discoverable for historical reasons but is not a
            # fetcher.  Do not invent an upstream failure if that helper
            # module itself cannot be imported.
            if name != "api30":
                detail = traceback.format_exception_only(type(exc), exc)[-1].strip()
                modules.append(_UnavailableFetcher(source_hint, f"import failed: {detail}"))
            continue
        if not callable(getattr(mod, "fetch", None)):
            continue
        source = getattr(mod, "SOURCE", name)
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
@dataclass(slots=True)
class Pipeline:
    """One fetch+store pass: what ``check`` and ``brief`` both start from."""

    results: list[FetchResult]
    notes: list[str]
    all_failed: bool


def run_pipeline(
    locations: list[Location],
    *,
    store: Store,
    http: Http,
    today: date,
    only: Sequence[str] | None = None,
) -> Pipeline | None:
    """Fetch every source, upsert the readings, cache derived params.

    ``None`` means there was nothing to run (no fetcher matched), which the
    callers turn into exit 1.  The order matters and is shared on purpose:
    the triggers and the brief both ask the store what yesterday looked
    like, so the readings of this run must be in it first.
    """
    for loc in locations:
        store.sync_location(loc)
    results = run_fetchers(locations, http=http, today=today, only=only)
    if not results:
        return None
    for result in results:
        store.upsert_readings(result.readings, retrieved_at=result.fetched_at)
    cache_params(store, results)
    return Pipeline(
        results=results,
        notes=[
            f"{SOURCE_LABELS.get(r.source, r.source)}: {r.error}" for r in results if r.error
        ],
        all_failed=all(not r.ok for r in results),
    )


def cmd_check(args: argparse.Namespace) -> int:
    today = date.today()
    locations = load_locations()
    if not locations:
        print(f"no locations configured ({locations_path()})", file=sys.stderr)
        return EXIT_ERROR

    only = args.only.split(",") if args.only else None
    with Http() as http, Store() as store:
        run = run_pipeline(locations, store=store, http=http, today=today, only=only)
        if run is None:
            print("no fetchers available", file=sys.stderr)
            return EXIT_ERROR
        results, notes, all_failed = run.results, run.notes, run.all_failed

        decision = _decide(locations, results, store=store, today=today)
        if all_failed:
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
                                "location_errors": r.location_errors,
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


def select_locations(
    locations: list[Location], wanted: Sequence[str]
) -> tuple[list[Location], list[str]]:
    """``--location`` filter, in the order of ``locations.yaml``.

    Twenty locations are ~60 KB of debugging tables, so the debug brief
    needs a way to look at two of them.  The order of the file survives the
    filter: the flags may come in any order, the output does not change.
    """
    chosen: set[str] = set()
    missing: list[str] = []
    for needle in wanted:
        found = find_location(locations, needle)
        if found is None:
            missing.append(needle)
        else:
            chosen.add(found.slug)
    return [loc for loc in locations if loc.slug in chosen], missing


def cmd_brief(args: argparse.Namespace) -> int:
    """The Hermes brief: same pipeline as ``check``, facts instead of a verdict.

    Without ``--mode`` this is the full debugging view with both tables.
    With ``--mode`` it is the short human block plus the send decision, and
    the run is recorded in ``reports`` so tomorrow can compare against it.

    ``--location`` narrows the run to the named locations (repeatable, slug
    or name).  It is a debugging flag: the block it prints covers a subset,
    so with ``--mode`` the decision is still computed but **not** stored --
    a partial row would make tomorrow's comparison lie about the locations
    left out.

    Exit code is always ``0`` -- the brief is meant to be read, not keyed
    off -- except when *every* source failed, which is exit ``1`` like
    everywhere else.  Partial failure is fine: the dead source becomes a
    line in "сбои источников" and the stored values still get printed.
    """
    from . import brief as brief_lib

    today = date.today()
    locations = load_locations()
    if not locations:
        print(f"no locations configured ({locations_path()})", file=sys.stderr)
        return EXIT_ERROR
    wanted = list(getattr(args, "location", None) or [])
    if wanted:
        locations, missing = select_locations(locations, wanted)
        if missing:
            print(f"no such location: {', '.join(missing)}", file=sys.stderr)
            return EXIT_ERROR

    with Http() as http, Store() as store:
        run = run_pipeline(locations, store=store, http=http, today=today)
        if run is None:
            print("no fetchers available", file=sys.stderr)
            return EXIT_ERROR
        # The decision is what derives and stores the API30 curve and names
        # the triggers that fired; the brief only reports them.
        decision = _decide(
            locations, run.results, store=store, today=today, record=False
        )
        calculation_error = None
        if decision.exit_code == EXIT_ERROR:
            calculation_error = decision.text or "rules calculation failed"
        notes = list(run.notes)
        if calculation_error:
            notes.append(f"расчёт: {calculation_error}")
        payload = brief_lib.build(
            store,
            locations,
            today,
            days=max(int(getattr(args, "days", brief_lib.DEFAULT_DAYS) or 0), 0),
            decision_data=decision.data,
            notes=notes,
            failed_sources=[r.source for r in run.results if not r.ok],
            results=run.results,
        )

        exit_code = EXIT_ERROR if run.all_failed or calculation_error else EXIT_SILENT
        mode = getattr(args, "mode", None)
        if mode:
            summary = report_lib.summarize(
                payload,
                mode=mode,
                today=today,
                calculation_error=calculation_error,
                all_failed=run.all_failed,
            )
            # A filtered run is a debugging view of a subset: decide, but
            # never overwrite the day's state with a partial report.
            if wanted:
                send, reason = report_lib.decide(store, summary)
            else:
                send, reason = report_lib.publish(store, summary)
            if args.json:
                print(
                    jsonlib.dumps(
                        dict(
                            report_lib.to_json(summary, send=send, reason=reason),
                            exit_code=exit_code,
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(report_lib.render(summary, send=send, reason=reason), end="")
            return exit_code

    if args.json:
        payload = dict(
            brief_lib.to_json(payload),
            exit_code=exit_code,
            calculation_error=calculation_error,
        )
        print(jsonlib.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(brief_lib.render(payload), end="")
    return exit_code


def _decide(
    locations: list[Location],
    results: list[FetchResult],
    *,
    store: Store,
    today: date,
    record: bool = True,
) -> Decision:
    """Call the required rules module and convert failures to exit 1."""
    package = __package__ or __name__.rsplit(".", 1)[0]
    try:
        rules = importlib.import_module(f"{package}.rules")
    except Exception:  # noqa: BLE001 - rules exists but is broken => error
        traceback.print_exc(file=sys.stderr)
        message = "rules module failed to import"
        return Decision(exit_code=EXIT_ERROR, text=message, data={"error": message})
    decide = getattr(rules, "decide", None)
    if not callable(decide):
        message = "rules.decide() is unavailable"
        return Decision(exit_code=EXIT_ERROR, text=message, data={"error": message})
    try:
        kwargs = {"store": store, "today": today}
        if not record:
            kwargs["record"] = False
        decision = decide(locations, results, **kwargs)
    except Exception:  # noqa: BLE001 - a broken rules module is an error
        traceback.print_exc(file=sys.stderr)
        message = "rules.decide() failed"
        return Decision(exit_code=EXIT_ERROR, text=message, data={"error": message})
    if not isinstance(decision, Decision):
        message = "rules.decide() returned a non-Decision"
        return Decision(exit_code=EXIT_ERROR, text=message, data={"error": message})
    return decision


def cmd_status(args: argparse.Namespace) -> int:
    """Print the last stored snapshot.  Never fetches -- ``check`` does that."""
    today = date.today()
    locations = load_locations()
    with Store() as store:
        payload = []
        lines = []
        for loc in locations:
            readings = store.latest_snapshot(loc.slug)
            lines.append(rules_lib.describe(store, loc, today))
            item = {
                "location": loc.as_dict(),
                "params": store.get_params(loc.slug),
                "readings": [_reading_dict(r) for r in readings],
            }
            item["view"] = rules_lib._jsonable(rules_lib.snapshot(store, loc, today))
            payload.append(item)
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


def cmd_calibration(args: argparse.Namespace) -> int:
    """Report archived forecast error without changing data or thresholds."""
    from . import calibration as calibration_lib

    locations = load_locations()
    with Store() as store:
        payload = calibration_lib.build(store, locations)
    if args.json:
        print(jsonlib.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(calibration_lib.render(payload, locations), end="")
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

    p_brief = sub.add_parser(
        "brief", help="fetch, store, print the full fact brief for Hermes"
    )
    p_brief.add_argument("--json", action="store_true", help="machine-readable output")
    p_brief.add_argument(
        "--days",
        type=int,
        default=16,
        metavar="N",
        help="forecast horizon in days (default 16, Open-Meteo's maximum)",
    )
    p_brief.add_argument(
        "--mode",
        choices=report_lib.MODES,
        help="print the short ready-to-send report block instead of the tables",
    )
    p_brief.add_argument(
        "--location",
        action="append",
        metavar="SLUG",
        help="only this location (repeatable; slug or name). Debugging filter: "
        "with --mode the decision is printed but not recorded",
    )
    p_brief.set_defaults(func=cmd_brief)

    p_status = sub.add_parser("status", help="print the last stored snapshot")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument(
        "--weekly", action="store_true", help="deprecated compatibility no-op"
    )
    p_status.set_defaults(func=cmd_status)

    p_calibration = sub.add_parser(
        "calibration", help="show forecast bias and MAE by horizon"
    )
    p_calibration.add_argument("--json", action="store_true")
    p_calibration.set_defaults(func=cmd_calibration)

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
    except BrokenPipeError:  # `check --json | head` is not an error
        try:  # pragma: no cover - keep the interpreter from complaining at exit
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return EXIT_SILENT
    except Exception:  # noqa: BLE001 - the CLI itself crashed => exit 1
        traceback.print_exc(file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
