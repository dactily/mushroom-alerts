"""Check a production checkout before a main-only fast-forward deployment."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def check(repo: Path, expected_main: str, *, audit: bool = False) -> str:
    """Reject stale refs, local edits, feature deployments and lost history."""
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE,
        ).strip()

    if len(expected_main) != 40 or any(c not in "0123456789abcdef" for c in expected_main):
        raise ValueError("Expected main must be a full SHA from the remote repository")
    main = git("rev-parse", "refs/remotes/origin/main")
    if main != expected_main:
        raise ValueError("origin/main is stale; fetch and verify the remote SHA again")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked files have local changes; preserve and review them first")
    if git("branch", "--show-current") != "main":
        raise ValueError("Production must use main; reconcile the deployed branch first")
    head = git("rev-parse", "HEAD")
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", head, main],
        capture_output=True,
    )
    if result.returncode:
        raise ValueError("Deployed HEAD is not in main; deployment would discard history")
    if audit and head != main:
        raise ValueError("Production HEAD differs from current main; review pending changes")
    return f"Release check passed: deployed={head} main={main}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--expected-main", required=True, help="Fresh remote main SHA")
    parser.add_argument("--audit", action="store_true", help="Also require HEAD to equal main")
    args = parser.parse_args()
    try:
        print(check(args.repo, args.expected_main, audit=args.audit))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
