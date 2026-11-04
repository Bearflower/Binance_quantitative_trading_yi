#!/usr/bin/env python3
"""Conservatively classify a complete push range for the funding collector."""
from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable, Sequence

ZERO_SHA = "0" * 40
SAFE_PREFIXES = ("docs/", "strategies/", "tests/", "backtest/", "dashboard/", "services/", "ai_tuner/")
SENSITIVE_PREFIXES = (
    "ai_tuner/funding_schedule/", "shared/", "scripts/",
    "database/postgres/init-scripts/", "database/postgres/migrations/", ".github/",
)
SENSITIVE_EXACT = {
    "ai_tuner/Dockerfile", "ai_tuner/requirements.txt",
    "docker-compose.yml", "docker-compose.logging.yml", "requirements.txt", "VERSION",
}


def select_funding_action(event_name: str, base: str, changes: Iterable[tuple[str, str]]) -> str:
    if event_name != "push" or not re.fullmatch(r"[0-9a-fA-F]{40}", base or "") or base == ZERO_SHA:
        return "rotate"
    changes = list(changes)
    if not changes:
        return "rotate"
    for status, path in changes:
        if not re.fullmatch(r"(?:[ACDMT]|R\d{1,3}|C\d{1,3})", status):
            return "rotate"
        if not path or path in SENSITIVE_EXACT or path.startswith(SENSITIVE_PREFIXES):
            return "rotate"
        if not path.startswith(SAFE_PREFIXES):
            return "rotate"
    return "keep"


def _changes(base: str, head: str) -> list[tuple[str, str]]:
    for revision in (base, head):
        check = subprocess.run(["git", "cat-file", "-e", f"{revision}^{{commit}}"], capture_output=True)
        if check.returncode:
            raise ValueError("push revision unavailable")
    result = subprocess.run(
        ["git", "diff", "--name-status", "-z", "--find-renames", base, head],
        capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError("push diff unavailable")
    fields = result.stdout.decode("utf-8", errors="strict").split("\0")
    if fields[-1:] == [""]:
        fields.pop()
    changes: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        count = 3 if status.startswith(("R", "C")) else 2
        if index + count > len(fields):
            raise ValueError("malformed push diff")
        changes.extend((status, path) for path in fields[index + 1:index + count])
        index += count
    return changes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    action = "rotate"
    if args.event_name == "push" and args.base and args.base != ZERO_SHA:
        try:
            action = select_funding_action(args.event_name, args.base, _changes(args.base, args.head))
        except (OSError, ValueError, UnicodeError):
            pass
    print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
