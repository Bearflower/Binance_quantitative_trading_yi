#!/usr/bin/env python3
"""Select the deploy path from an explicit Git push range."""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable, Sequence


MARKERS = {
    ".github/deploy-modes/xau-calibrated-shadow-first-create",
    ".github/deploy-modes/xau-calibrated-shadow-first-create-retry-1",
}
ZERO_SHA = "0" * 40


class DeployModeError(RuntimeError):
    """The push range cannot safely select a deployment mode."""


def select_deploy_mode(
    event_name: str,
    base: str,
    changes: Iterable[tuple[str, str]],
) -> str:
    if event_name != "push":
        return "full"
    if not base or base == ZERO_SHA:
        raise DeployModeError("push base is missing or zero")

    marker_changes = [(status, path) for status, path in changes if path in MARKERS]
    if len(marker_changes) > 1:
        raise DeployModeError("ambiguous marker changes")
    if marker_changes and marker_changes[0][0] == "A":
        return "calibrated-first-create"
    return "full"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DeployModeError(detail or f"git {' '.join(args)} failed")
    return result.stdout


def _require_commit(revision: str) -> None:
    _git("cat-file", "-e", f"{revision}^{{commit}}")


def _read_changes(base: str, head: str) -> list[tuple[str, str]]:
    lines = _git("diff", "--name-status", "--find-renames", base, head).splitlines()
    changes: list[tuple[str, str]] = []
    for line in lines:
        columns = line.split("\t")
        if len(columns) < 2:
            raise DeployModeError(f"invalid git diff record: {line!r}")
        status = columns[0]
        for path in columns[1:]:
            changes.append((status, path))
    return changes


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.event_name != "push":
            mode = select_deploy_mode(args.event_name, args.base, [])
        else:
            if not args.base or args.base == ZERO_SHA:
                raise DeployModeError("push base is missing or zero")
            _require_commit(args.base)
            _require_commit(args.head)
            mode = select_deploy_mode(
                args.event_name,
                args.base,
                _read_changes(args.base, args.head),
            )
    except DeployModeError as exc:
        print(f"deploy mode selection failed closed: {exc}", file=sys.stderr)
        return 2
    print(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
