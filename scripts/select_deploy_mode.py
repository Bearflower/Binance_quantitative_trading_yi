#!/usr/bin/env python3
"""Select the deploy path from an explicit Git push range."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from collections.abc import Iterable, Sequence


MARKERS = {
    ".github/deploy-modes/xau-calibrated-shadow-first-create",
    ".github/deploy-modes/xau-calibrated-shadow-first-create-retry-1",
}
EXECUTION_FACTS_MARKER = ".github/deploy-modes/execution-facts-first-create"
ALLOCATION_RECONCILE_MARKER = ".github/deploy-modes/allocation-reconcile"
ALLOCATION_RECONCILE_RETRY_MARKER = ".github/deploy-modes/allocation-reconcile-retry-2"
REMEDIATION_MARKER = ".github/deploy-modes/ledger-risk-remediation-20261104"
SUCCESSOR_MARKER = ".github/deploy-modes/strategy-risk-successor-20261105"
SUCCESSOR_RECOVERY_MARKER = ".github/deploy-modes/strategy-risk-successor-20261105-recovery-1"
RISK_FOLLOWUP_MARKER = ".github/deploy-modes/strategy-risk-followup-20261106"
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

    marker_changes = [(status, path) for status, path in changes if path in MARKERS | {EXECUTION_FACTS_MARKER, ALLOCATION_RECONCILE_MARKER, ALLOCATION_RECONCILE_RETRY_MARKER, REMEDIATION_MARKER, SUCCESSOR_MARKER, SUCCESSOR_RECOVERY_MARKER, RISK_FOLLOWUP_MARKER}]
    if len(marker_changes) > 1:
        raise DeployModeError("ambiguous marker changes")
    if any(path == REMEDIATION_MARKER for _, path in marker_changes):
        if marker_changes != [("A", REMEDIATION_MARKER)] or not re.fullmatch(r"[0-9a-f]{40}", base):
            raise DeployModeError("remediation requires one added marker and an exact push base")
        return "ledger-risk-remediation"
    if any(path in {SUCCESSOR_MARKER, SUCCESSOR_RECOVERY_MARKER} for _, path in marker_changes):
        if marker_changes not in ([("A", SUCCESSOR_MARKER)], [("A", SUCCESSOR_RECOVERY_MARKER)]) or not re.fullmatch(r"[0-9a-f]{40}", base):
            raise DeployModeError("successor requires one added marker and an exact push base")
        return "strategy-risk-successor"
    if any(path == RISK_FOLLOWUP_MARKER for _, path in marker_changes):
        if marker_changes != [("A", RISK_FOLLOWUP_MARKER)] or not re.fullmatch(r"[0-9a-f]{40}", base):
            raise DeployModeError("risk followup requires one added marker and an exact push base")
        return "strategy-risk-followup"
    if marker_changes and marker_changes[0][0] == "A":
        if marker_changes[0][1] == EXECUTION_FACTS_MARKER:
            return "execution-facts-first-create"
        if marker_changes[0][1] in {ALLOCATION_RECONCILE_MARKER, ALLOCATION_RECONCILE_RETRY_MARKER}:
            return "allocation-reconcile"
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
    parser.add_argument("--manual-mode", default="auto", choices=["auto","ledger-risk-remediation","strategy-risk-successor","strategy-risk-followup"])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.event_name != "push":
            from scripts.ledger_risk_manifest import MANIFEST
            exists = subprocess.run(["git","cat-file","-e",f"{args.head}:{MANIFEST}"],capture_output=True).returncode == 0
            if exists and args.manual_mode not in ("ledger-risk-remediation", "strategy-risk-successor", "strategy-risk-followup"):
                raise DeployModeError("manual remediation selection required for this release tree")
            mode = args.manual_mode if args.manual_mode != "auto" else "full"
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
        if mode == "ledger-risk-remediation":
            from scripts.ledger_risk_manifest import MANIFEST,validate_tree,ReleaseBlocked
            try:
                manifest=json.loads(_git("show",f"{args.head}:{MANIFEST}"))
                base=args.base if args.event_name=="push" else manifest['base_revision']
                if not re.fullmatch(r"[0-9a-f]{40}",base):raise DeployModeError("invalid remediation base")
                _git("merge-base","--is-ancestor",base,args.head)
                validate_tree(manifest,_read_changes(base,args.head),lambda path:_git("show",f"{args.head}:{path}").encode(),base)
            except (ValueError,KeyError,TypeError) as exc:
                raise DeployModeError(str(exc)) from exc
        if mode == "strategy-risk-successor":
            from scripts.strategy_successor_manifest import BASE, MANIFEST, validate_tree
            try:
                if args.event_name == "push" and args.base not in {BASE, "9d6c6c61da1e42ea378095ce6065ba463b2fb686"}:
                    raise DeployModeError("successor push must start from reviewed predecessor or failed attempt")
                manifest = json.loads(_git("show", f"{args.head}:{MANIFEST}"))
                _git("merge-base", "--is-ancestor", BASE, args.head)
                validate_tree(manifest, _read_changes(BASE, args.head), lambda path: _git("show", f"{args.head}:{path}").encode())
            except (ValueError, KeyError, TypeError) as exc:
                raise DeployModeError(str(exc)) from exc
        if mode == "strategy-risk-followup":
            from scripts.risk_followup_manifest import BASE, MANIFEST, validate_tree
            try:
                if args.event_name == "push" and args.base != BASE:
                    raise DeployModeError("risk followup push must start from reviewed base")
                manifest = json.loads(_git("show", f"{args.head}:{MANIFEST}"))
                validate_tree(manifest, _read_changes(BASE, args.head), lambda path: _git("show", f"{args.head}:{path}").encode())
            except (ValueError, KeyError, TypeError) as exc:
                raise DeployModeError(str(exc)) from exc
    except DeployModeError as exc:
        print(f"deploy mode selection failed closed: {exc}", file=sys.stderr)
        return 2
    print(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
