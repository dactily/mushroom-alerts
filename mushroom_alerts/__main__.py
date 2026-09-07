"""CLI: ``python -m mushroom_alerts check|brief|status|add|list|del``.

Exit-code contract for Hermes (PLAN §2)::

    0   nothing to say, stay silent
    10  positive signal -- forward stdout to Telegram verbatim
    1   error: the script itself crashed, or *every* source failed

A single dead source is never an error: it becomes a note in the report and
the other sources carry on (PLAN §6).

``check`` fetches, upserts every reading, and only then hands the results
to ``rules.decide`` -- that order is load-bearing: the triggers ask the
store what yesterday looked like, and ``rules`` derives and stores the
API30 forecast curve on top of what was just written.  Without a ``rules``
module the CLI falls back to printing one collapsed snapshot line per
location and exiting 0.

``brief`` runs the very same pipeline (:func:`run_pipeline` + :func:`_decide`)
and then prints the facts at length instead of collapsing them: it is what
Hermes Agent reads and interprets (PLAN §2b, ``mushroom_alerts/brief.py``,
``hermes/PROMPT.md``).  It always exits ``0`` unless every source failed.

``status`` never fetches: it renders what is in SQLite, through
``rules.describe`` when that is importable and through ``format_snapshot``
otherwise.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
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


# ----------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------
def _fmt_value(reading: Reading) -> str:
    if reading.metric == "level":
        return f"{int(reading.value)}/5"
    if reading.metric == "score":
        return f"{reading.value:.2f}"
    if reading.metric == "rh":
        return f"{reading.value:.0f} %"
    if reading.metric.endswith("_mm"):  # sra_mm, precip_mm, api30_mm
        return f"{reading.value:.0f} mm" if abs(reading.value) >= 10 else f"{reading.value:.1f} mm"
    if reading.metric.startswith("t_"):
        return f"{reading.value:.1f} °C"
    return f"{reading.value:g}"


def _newest_per_metric(readings: Iterable[Reading]) -> list[Reading]:
    """One reading per metric: the newest observation, forecasts last.

    The station publishes 35 days x 9 metrics and Open-Meteo 19 days x 4;
    printing all of that is not a report, it is a data dump.
    """
    best: dict[str, Reading] = {}
    for r in readings:
        current = best.get(r.metric)
        if current is None:
            best[r.metric] = r
            continue
        # prefer an observation over a forecast, then the newer day
        rank = (not r.is_forecast, r.date)
        if rank > (not current.is_forecast, current.date):
            best[r.metric] = r
    return [best[m] for m in sorted(best)]


def _fmt_segment(source: str, readings: list[Reading]) -> str:
    label = SOURCE_LABELS.get(source, source)
    parts = []
    for r in _newest_per_metric(readings):
        if r.metric == "level":
            parts.append(_fmt_value(r))
        else:
            parts.append(f"{r.metric} {_fmt_value(r)}")
    flag = " (starý)" if any((r.meta or {}).get("stale") for r in readings) else ""
    return f"{label} {' '.join(parts)}{flag}"


def _forecast_segment(source: str, readings: list[Reading], today: date) -> str:
    """Compact one-liner for the two multi-day forecast sources.

    ``api30_forecast`` -> today's value, the peak, and the day the threshold
    is crossed (or ``—``); ``openmeteo`` -> the next day with >= 5 mm of
    rain.  Both are curves; the newest-value-per-metric rule above would say
    nothing useful about them.
    """
    from . import api30 as api30_lib

    label = SOURCE_LABELS.get(source, source)
    if source == api30_lib.FORECAST_SOURCE:
        curve = sorted((r.date, float(r.value)) for r in readings if r.metric == api30_lib.METRIC)
        if not curve:
            return f"{label} —"
        threshold = api30_lib.threshold_mm()
        now = next((v for d, v in curve if d == today), None)
        peak_day, peak = max(curve, key=lambda pair: (pair[1], pair[0]))
        bits = []
        if now is not None:
            bits.append(f"сегодня {now:.0f} mm")
        bits.append(f"max {peak:.0f} mm {peak_day.day}.{peak_day.month}.")
        cross = api30_lib.crossing(curve, threshold, today=today)
        if cross is not None:
            bits.append(f"порог {threshold:.0f} mm {cross.day}.{cross.month}.")
        elif now is not None and now >= threshold:
            bits.append(f"порог {threshold:.0f} mm уже сегодня")
        else:
            bits.append(f"порог {threshold:.0f} mm —")
        return f"{label} " + ", ".join(bits)

    rain = sorted((r.date, float(r.value)) for r in readings if r.metric == "precip_mm")
    nxt = next(((d, v) for d, v in rain if d > today and v >= 5.0), None)
    total = sum(v for d, v in rain if d > today)
    bits = [f"дождь +{total:.0f} mm/16 дн"] if rain else []
    if nxt is not None:
        bits.append(f"ближайший ≥5 mm {nxt[1]:.0f} mm {nxt[0].day}.{nxt[0].month}.")
    elif rain:
        bits.append("ближайший ≥5 mm —")
    return f"{label} " + ", ".join(bits) if bits else f"{label} —"


def format_snapshot(
    location: Location, readings: list[Reading], today: date | None = None
) -> str:
    """PLAN §3 shape: one line per location, one segment per source.

    Multi-day sources are collapsed: the station and HoubyMapa to their
    newest value per metric, the two forecast curves to a summary.  This is
    the fallback rendering -- ``rules.describe`` does a better job and is
    what ``status`` uses when ``rules`` is importable.
    """
    day = today or date.today()
    by_source: dict[str, list[Reading]] = {}
    for r in readings:
        by_source.setdefault(r.source, []).append(r)
    if not by_source:
        return f"🍄 {location.name}: нет данных"
    order = [s for s in SOURCE_LABELS if s in by_source] + [
        s for s in sorted(by_source) if s not in SOURCE_LABELS
    ]
    segments = [
        _forecast_segment(s, by_source[s], day)
        if s in ("openmeteo", "api30_forecast")
        else _fmt_segment(s, by_source[s])
        for s in order
    ]
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
        if decision is None:  # no rules module yet -> plain snapshot
            lines = [
                format_snapshot(
                    loc,
                    [r for res in results for r in res.for_location(loc.slug)],
                    today,
                )
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


def cmd_brief(args: argparse.Namespace) -> int:
    """The Hermes brief: same pipeline as ``check``, facts instead of a verdict.

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

    with Http() as http, Store() as store:
        run = run_pipeline(locations, store=store, http=http, today=today)
        if run is None:
            print("no fetchers available", file=sys.stderr)
            return EXIT_ERROR
        # The decision is what derives and stores the API30 curve and names
        # the triggers that fired; the brief only reports them.
        decision = _decide(locations, run.results, store=store, today=today)
        calculation_error = None
        if decision is None:
            calculation_error = "rules module is unavailable"
        elif decision.exit_code == EXIT_ERROR:
            calculation_error = decision.text or "rules calculation failed"
        notes = list(run.notes)
        if calculation_error:
            notes.append(f"расчёт: {calculation_error}")
        payload = brief_lib.build(
            store,
            locations,
            today,
            days=max(int(getattr(args, "days", brief_lib.DEFAULT_DAYS) or 0), 0),
            decision_data=None if decision is None else decision.data,
            notes=notes,
            failed_sources=[r.source for r in run.results if not r.ok],
        )

    exit_code = EXIT_ERROR if run.all_failed or calculation_error else EXIT_SILENT
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
        message = "rules module failed to import"
        return Decision(exit_code=EXIT_ERROR, text=message, data={"error": message})
    decide = getattr(rules, "decide", None)
    if not callable(decide):
        return None
    try:
        decision = decide(locations, results, store=store, today=today)
    except Exception:  # noqa: BLE001 - a broken rules module is an error
        traceback.print_exc(file=sys.stderr)
        message = "rules.decide() failed"
        return Decision(exit_code=EXIT_ERROR, text=message, data={"error": message})
    if not isinstance(decision, Decision):
        message = "rules.decide() returned a non-Decision"
        return Decision(exit_code=EXIT_ERROR, text=message, data={"error": message})
    return decision


def _rules_module() -> Any | None:
    """The ``rules`` module, or ``None`` if it is absent or broken.

    ``status`` must never fail because of it; ``check`` uses ``_decide``,
    which is stricter on purpose (a broken ``rules`` there is exit 1).
    """
    package = __package__ or __name__.rsplit(".", 1)[0]
    try:
        if importlib.util.find_spec(f"{package}.rules") is None:
            return None
        return importlib.import_module(f"{package}.rules")
    except Exception:  # noqa: BLE001 - degrade to the plain snapshot
        return None


def cmd_status(args: argparse.Namespace) -> int:
    """Print the last stored snapshot.  Never fetches -- ``check`` does that."""
    today = date.today()
    locations = load_locations()
    rules = _rules_module()
    describe = getattr(rules, "describe", None) if rules is not None else None
    with Store() as store:
        payload = []
        lines = []
        for loc in locations:
            readings = store.latest_snapshot(loc.slug)
            line = None
            if callable(describe):
                try:
                    line = describe(store, loc, today)
                except Exception:  # noqa: BLE001 - fall back, never crash
                    traceback.print_exc(file=sys.stderr)
                    line = None
            lines.append(line or format_snapshot(loc, readings, today))
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
        if getattr(args, "weekly", False):
            # PLAN §2b / stage 2: the Friday weekend digest is not implemented yet.
            print("(недельный дайджест: TODO, этап 2)")
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
    p_brief.set_defaults(func=cmd_brief)

    p_status = sub.add_parser("status", help="print the last stored snapshot")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument(
        "--weekly", action="store_true", help="weekly digest (TODO, stage 2)"
    )
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
