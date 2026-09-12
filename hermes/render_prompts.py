#!/usr/bin/env python3
"""Render portable Hermes prompt templates with deployment-time bindings."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def render(template: str, *, job_id: str, hermes_cli: str) -> str:
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("job ID contains unsupported characters")
    cli = Path(hermes_cli)
    if not cli.is_absolute():
        raise ValueError("Hermes CLI path must be absolute")
    result = template.replace("{{JOB_ID}}", job_id).replace(
        "{{HERMES_CLI}}", str(cli)
    )
    if "{{JOB_ID}}" in result or "{{HERMES_CLI}}" in result:
        raise ValueError("prompt template was not fully rendered")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily-job-id", required=True)
    parser.add_argument("--weekend-job-id", required=True)
    parser.add_argument("--hermes-cli", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.daily_job_id == args.weekend_job_id:
        parser.error("daily and weekend job IDs must be different")
    daily = render(
        (ROOT / "daily_prompt.txt.in").read_text(encoding="utf-8"),
        job_id=args.daily_job_id,
        hermes_cli=args.hermes_cli,
    )
    weekend = render(
        (ROOT / "friday_prompt.txt.in").read_text(encoding="utf-8"),
        job_id=args.weekend_job_id,
        hermes_cli=args.hermes_cli,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "daily_prompt.txt").write_text(daily, encoding="utf-8")
    (args.output_dir / "friday_prompt.txt").write_text(weekend, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
