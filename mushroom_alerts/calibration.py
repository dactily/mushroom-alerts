"""Read-only forecast error report; it never adjusts forecasts or rules."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from .base import Location
from .store import Store

TZ = ZoneInfo("Europe/Prague")
BUCKETS = (("1-3", 1, 3), ("4-7", 4, 7), ("8-16", 8, 16))
METRICS = (
    ("precipitation", "openmeteo", "precip_mm", "chmi_station", "sra_mm"),
    ("api30", "api30_forecast", "api30_mm", "chmi_station", "api30_mm"),
)


def _issued_day(value: str) -> date:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TZ)
    return stamp.astimezone(TZ).date()


def _stats(errors: Iterable[float]) -> dict[str, float | int | None]:
    values = list(errors)
    if not values:
        return {"n": 0, "bias": None, "mae": None}
    return {
        "n": len(values),
        "bias": round(sum(values) / len(values), 3),
        "mae": round(sum(abs(value) for value in values) / len(values), 3),
    }


def build(store: Store, locations: Sequence[Location]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "locations": {},
        "automatic_adjustment": False,
        "chmi_map_role": "model_reference_not_mushroom_evidence",
    }
    for location in locations:
        location_report: dict[str, Any] = {}
        for label, source, metric, actual_source, actual_metric in METRICS:
            by_bucket: dict[str, list[float]] = {name: [] for name, _, _ in BUCKETS}
            rows = store.forecast_error_pairs(
                location.slug, source, metric, actual_source, actual_metric
            )
            for row in rows:
                horizon = (date.fromisoformat(row["target_date"]) - _issued_day(row["retrieved_at"])).days
                for bucket, start, end in BUCKETS:
                    if start <= horizon <= end:
                        by_bucket[bucket].append(float(row["predicted"]) - float(row["actual"]))
                        break
            location_report[label] = {
                bucket: _stats(by_bucket[bucket]) for bucket, _, _ in BUCKETS
            }
        report["locations"][location.slug] = location_report
    return report


def render(report: dict[str, Any], locations: Sequence[Location]) -> str:
    names = {location.slug: location.name for location in locations}
    lines = ["КАЛИБРОВОЧНЫЙ ОТЧЁТ", "bias = прогноз − наблюдение; MAE в мм"]
    for slug, metrics in report["locations"].items():
        lines.append("")
        lines.append(names.get(slug, slug))
        for metric, label in (("precipitation", "осадки"), ("api30", "API30")):
            values = metrics[metric]
            parts = []
            for bucket, _, _ in BUCKETS:
                stats = values[bucket]
                if stats["n"]:
                    parts.append(
                        f"{bucket} д: n={stats['n']}, bias={stats['bias']:.2f}, MAE={stats['mae']:.2f}"
                    )
                else:
                    parts.append(f"{bucket} д: n=0")
            lines.append(f"  {label}: " + "; ".join(parts))
    lines += [
        "",
        "Карта ČHMÚ используется только как модельный ориентир, не как доказательство наличия грибов.",
        "Автоматические поправки прогнозов и порогов не применяются.",
    ]
    return "\n".join(lines) + "\n"
