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
RISK_FOLLOWUP_MARKER = ".github/deploy-modes/strategy-small-position-followup-20261110"
STOP_FOLLOWUP_MARKER = ".github/deploy-modes/aggressive-stop-followup-20261109"
AI_TUNER_TRIAL_MARKER = ".github/deploy-modes/ai-tuner-trial-20261110"
INCOME_ATTRIBUTION_MARKER = ".github/deploy-modes/income-attribution-20261110"
GRID_PM_MARKER = ".github/deploy-modes/grid-pm-oneway-20261111"
SEVEN_GAPS_MARKER = ".github/deploy-modes/seven-code-gaps-20261112"
DASHBOARD_PERFORMANCE_MARKER = ".github/deploy-modes/dashboard-performance-20261112"
DASHBOARD_PERFORMANCE_BASE = "61c91a8fb128822bf0c0a8d388566089ccd35375"
SEVEN_GAPS_BASE = "caa0f1b2509774d28b765dd128a79f37482f4c9f"
GRID_PM_BASE = "ae9c7e5949586b54e96043eb18ebc1533e6b0203"
INCOME_ATTRIBUTION_REFRESH_MARKER = ".github/deploy-modes/income-attribution-refresh-20261111"
INCOME_ATTRIBUTION_BASE = "6821072aef1a021f3f2920e7c84fc07b09d21454"
INCOME_ATTRIBUTION_REFRESH_BASE = "2f9231f47a5654b4feecbbe997b952bdcd8e09fa"
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

    marker_changes = [(status, path) for status, path in changes
                      if path.startswith(".github/deploy-modes/")]
    if len(marker_changes) > 1:
        raise DeployModeError("ambiguous marker changes")
    if marker_changes and marker_changes[0][1] not in MARKERS | {EXECUTION_FACTS_MARKER, ALLOCATION_RECONCILE_MARKER, ALLOCATION_RECONCILE_RETRY_MARKER, REMEDIATION_MARKER, SUCCESSOR_MARKER, SUCCESSOR_RECOVERY_MARKER, RISK_FOLLOWUP_MARKER, STOP_FOLLOWUP_MARKER, AI_TUNER_TRIAL_MARKER, INCOME_ATTRIBUTION_MARKER, INCOME_ATTRIBUTION_REFRESH_MARKER, GRID_PM_MARKER, SEVEN_GAPS_MARKER, DASHBOARD_PERFORMANCE_MARKER}:
        raise DeployModeError("unrecognized deployment marker")
    if any(path == DASHBOARD_PERFORMANCE_MARKER for _, path in marker_changes):
        if marker_changes != [("A", DASHBOARD_PERFORMANCE_MARKER)]:
            raise DeployModeError("dashboard performance release requires one added marker")
        return "dashboard-performance"
    if any(path == SEVEN_GAPS_MARKER for _, path in marker_changes):
        if marker_changes != [("A", SEVEN_GAPS_MARKER)]:
            raise DeployModeError("seven gaps release requires one added marker")
        return "seven-code-gaps"
    if any(path == GRID_PM_MARKER for _, path in marker_changes):
        if marker_changes != [("A", GRID_PM_MARKER)]:
            raise DeployModeError("grid PM release requires one added marker")
        return "grid-pm-oneway"
    if any(path == INCOME_ATTRIBUTION_REFRESH_MARKER for _, path in marker_changes):
        if marker_changes != [("A", INCOME_ATTRIBUTION_REFRESH_MARKER)] or not re.fullmatch(r"[0-9a-f]{40}", base):
            raise DeployModeError("income attribution refresh requires one added marker and an exact push base")
        return "income-attribution"
    if any(path == INCOME_ATTRIBUTION_MARKER for _, path in marker_changes):
        if marker_changes != [("A", INCOME_ATTRIBUTION_MARKER)] or not re.fullmatch(r"[0-9a-f]{40}", base):
            raise DeployModeError("income attribution requires one added marker and an exact push base")
        return "income-attribution"
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
    if any(path == STOP_FOLLOWUP_MARKER for _, path in marker_changes):
        if marker_changes != [("A", STOP_FOLLOWUP_MARKER)] or not re.fullmatch(r"[0-9a-f]{40}", base):
            raise DeployModeError("STOP followup requires one added marker and an exact push base")
        return "strategy-stop-followup"
    if any(path == AI_TUNER_TRIAL_MARKER for _, path in marker_changes):
        if marker_changes != [("A", AI_TUNER_TRIAL_MARKER)] or not re.fullmatch(r"[0-9a-f]{40}", base):
            raise DeployModeError("AI Tuner trial requires one added marker and an exact push base")
        return "ai-tuner-trial"
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


def _validate_income_scope(base: str, changes: list[tuple[str, str]]) -> None:
    if ("A", INCOME_ATTRIBUTION_REFRESH_MARKER) in changes:
        if base != INCOME_ATTRIBUTION_REFRESH_BASE:
            raise DeployModeError("income attribution refresh must start from reviewed main")
        allowed = {INCOME_ATTRIBUTION_REFRESH_MARKER,
                   "scripts/select_deploy_mode.py", "scripts/reconcile_income_attribution.py",
                   "scripts/scoped_income_attribution_release.py"}
        for status, path in changes:
            if status not in {"A", "M"} or not (path in allowed or path.startswith("shared/accounting/")
                                                  or path.startswith("tests/") or path.startswith("docs/")):
                raise DeployModeError("income attribution refresh scope contains unreviewed path: " + path)
        return
    if base != INCOME_ATTRIBUTION_BASE:
        raise DeployModeError("income attribution push must start from reviewed main")
    exact = {
        INCOME_ATTRIBUTION_MARKER, ".github/workflows/deploy.yml", "docker-compose.yml",
        "dashboard/frontend/js/main.js", "shared/condition_orders.py",
        "database/postgres/init-scripts/19-derived-income-attribution.sql",
        "database/postgres/migrations/2026-11-09-derived-income-attribution.sql",
        "scripts/audit_algo_attribution.py", "scripts/reconcile_income_attribution.py",
        "scripts/report_economic_ledger.py", "scripts/select_deploy_mode.py",
        "scripts/render_remote_deploy.py", "scripts/remote_deploy_runtime.sh",
        "scripts/package_income_attribution_release.py",
        "scripts/scoped_income_attribution_release.py", "scripts/run_income_attribution.sh",
        "deployment/systemd/income-attribution.service",
        "deployment/systemd/income-attribution.timer",
    }
    if ("A", INCOME_ATTRIBUTION_MARKER) not in changes:
        raise DeployModeError("income attribution marker not added")
    for status, path in changes:
        if status not in {"A", "M"} or not (
            path in exact or path.startswith("shared/accounting/")
            or path.startswith("tests/") or path.startswith("docs/")
        ):
            raise DeployModeError("income attribution scope contains unreviewed path: " + path)


def _validate_dashboard_scope(base: str, changes: list[tuple[str, str]]) -> None:
    if base != DASHBOARD_PERFORMANCE_BASE or ("A", DASHBOARD_PERFORMANCE_MARKER) not in changes:
        raise DeployModeError("dashboard performance release must start from reviewed main")
    allowed = {
        DASHBOARD_PERFORMANCE_MARKER, ".github/workflows/deploy.yml",
        "dashboard/backend/services/exchange_ledger.py", "dashboard/backend/models/schemas.py",
        "dashboard/frontend/index.html", "dashboard/frontend/js/main.js",
        "shared/accounting/dashboard_performance.py", "scripts/select_deploy_mode.py",
        "scripts/render_remote_deploy.py", "scripts/remote_deploy_runtime.sh",
        "scripts/package_dashboard_performance_release.py",
        "scripts/scoped_dashboard_performance_release.py",
        "scripts/dashboard_release_exchange_snapshot.py",
    }
    for status, path in changes:
        if status not in {"A", "M"} or not (path in allowed or path.startswith("tests/")
                                          or path.startswith("docs/")):
            raise DeployModeError("dashboard performance scope contains unreviewed path: " + path)


def _validate_grid_scope(base: str, changes: list[tuple[str, str]]) -> None:
    if base != GRID_PM_BASE:
        raise DeployModeError("grid PM push must start from reviewed main")
    exact = {
        GRID_PM_MARKER, ".env.example", "docker-compose.yml", ".github/workflows/deploy.yml",
        "scripts/preflight_grid_scoped_release.py", "scripts/scoped_grid_release.py",
        "scripts/package_grid_release.py", "scripts/grid_account_readonly.py",
        "scripts/prepare_grid_subaccount.py", "scripts/set_grid_leverage.py",
        "scripts/select_deploy_mode.py",
        "scripts/render_remote_deploy.py", "scripts/remote_deploy_runtime.sh",
        "shared/binance_api.py",
    }
    if ("A", GRID_PM_MARKER) not in changes:
        raise DeployModeError("grid PM marker not added")
    for status, path in changes:
        if status not in {"A", "M"} or not (
            path in exact or path.startswith("strategies/grid/")
            or path.startswith("tests/") or path.startswith("docs/")
        ):
            raise DeployModeError("grid PM scope contains unreviewed path: " + path)


def _validate_seven_gaps_scope(base: str, changes: list[tuple[str, str]]) -> None:
    if base != SEVEN_GAPS_BASE:
        raise DeployModeError("seven gaps push must start from reviewed main")
    exact = {
        SEVEN_GAPS_MARKER, ".github/workflows/deploy.yml",
        "scripts/select_deploy_mode.py", "scripts/render_remote_deploy.py",
        "scripts/remote_deploy_runtime.sh", "scripts/package_seven_gaps_release.py",
        "scripts/scoped_seven_gaps_release.py", "dashboard/frontend/index.html",
        "dashboard/frontend/js/main.js", "dashboard/backend/models/schemas.py",
        "shared/accounting/cashflow_roi.py", "task_plan.md", "findings.md", "progress.md",
        "strategies/btc_eth_aggressive/strategy.py", "strategies/hrs/strategy.py",
        "strategies/new_coin/executor.py", "strategies/new_coin/strategy.py",
        "strategies/new_coin/recovery_evidence.py", "ai_tuner/monitor/reduced_trial.py",
        "ai_tuner/allocation/shadow_allocation.py",
    }
    for status, path in changes:
        if status not in {"A", "M"} or not (path in exact or path.startswith("tests/")
            or path.startswith("docs/") or path.startswith("strategies/") and "/tests/" in path
            or path.startswith("ai_tuner/") and "/tests/" in path):
            raise DeployModeError("seven gaps scope contains unreviewed path: " + path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--manual-mode", default="auto", choices=["auto","ledger-risk-remediation","strategy-risk-successor","strategy-risk-followup","strategy-stop-followup","ai-tuner-trial","income-attribution","grid-pm-oneway","seven-code-gaps","dashboard-performance"])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.event_name != "push":
            from scripts.ledger_risk_manifest import MANIFEST
            exists = subprocess.run(["git","cat-file","-e",f"{args.head}:{MANIFEST}"],capture_output=True).returncode == 0
            if exists and args.manual_mode not in ("ledger-risk-remediation", "strategy-risk-successor", "strategy-risk-followup", "strategy-stop-followup", "ai-tuner-trial", "income-attribution", "grid-pm-oneway", "seven-code-gaps", "dashboard-performance"):
                raise DeployModeError("manual remediation selection required for this release tree")
            mode = args.manual_mode if args.manual_mode != "auto" else "full"
        else:
            if not args.base or args.base == ZERO_SHA:
                raise DeployModeError("push base is missing or zero")
            _require_commit(args.base)
            _require_commit(args.head)
            changes = _read_changes(args.base, args.head)
            mode = select_deploy_mode(args.event_name, args.base, changes)
            if mode == "income-attribution":
                _validate_income_scope(args.base, changes)
            if mode == "dashboard-performance":
                _validate_dashboard_scope(args.base, changes)
            if mode == "grid-pm-oneway":
                _validate_grid_scope(args.base, changes)
            if mode == "seven-code-gaps":
                _validate_seven_gaps_scope(args.base, changes)
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
                    raise DeployModeError("small-position push must start from reviewed predecessor")
                manifest = json.loads(_git("show", f"{args.head}:{MANIFEST}"))
                validate_tree(manifest, _read_changes(BASE, args.head), lambda path: _git("show", f"{args.head}:{path}").encode())
            except (ValueError, KeyError, TypeError) as exc:
                raise DeployModeError(str(exc)) from exc
        if mode == "strategy-stop-followup":
            from scripts.stop_followup_manifest import BASE, MANIFEST, validate_tree
            try:
                if args.event_name == "push" and args.base != BASE:
                    raise DeployModeError("STOP followup push must start from reviewed base")
                manifest = json.loads(_git("show", f"{args.head}:{MANIFEST}"))
                validate_tree(manifest, _read_changes(BASE, args.head),
                              lambda path: _git("show", f"{args.head}:{path}").encode())
            except (ValueError, KeyError, TypeError) as exc:
                raise DeployModeError(str(exc)) from exc
        if mode == "ai-tuner-trial":
            from scripts.ai_tuner_trial_manifest import BASE, MANIFEST, validate_tree
            try:
                if args.event_name == "push" and args.base != BASE:
                    raise DeployModeError("AI Tuner trial push must start from reviewed base")
                manifest = json.loads(_git("show", f"{args.head}:{MANIFEST}"))
                validate_tree(manifest, _read_changes(BASE, args.head),
                              lambda path: _git("show", f"{args.head}:{path}").encode())
            except (ValueError, KeyError, TypeError) as exc:
                raise DeployModeError(str(exc)) from exc
    except DeployModeError as exc:
        print(f"deploy mode selection failed closed: {exc}", file=sys.stderr)
        return 2
    print(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
