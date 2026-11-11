#!/usr/bin/env python3
"""Read-only gate for a grid-only successor to the active production pin.

Prints no Compose values: those include credentials. This script cannot deploy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


EXPECTED_PIN_SHA = "503f7e562c9123eac14b6dfaba9028cd20fef247"
EXPECTED_PIN_OVERLAY_SHA256 = "c806f1bd2e0cb9e5ac1182b4977003531a22ef9a2ae970cd3e3fef87f637c8bf"
EXPECTED_BASE_SHA256 = "64b527867d9097651edaa19a8a35eab0c47fd9d8d40f6cad407770ea82ed784c"
EXPECTED_LOGGING_SHA256 = "304914aa828ce52f43286a5cf276f7006cec07b127a4e668bcb1bd01f44eae72"


class GateError(RuntimeError):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compose_config(root: Path, overlay: Path, extra: Path | None = None) -> dict:
    pin = json.loads((root / ".deploy_tmp/ledger-risk-active.json").read_text())
    args = ["docker", "compose", "-p", "trading_system", "--project-directory", str(root)]
    for path in (root / "docker-compose.yml", root / "docker-compose.logging.yml",
                 Path(pin["overlay"]), extra):
        if path is not None:
            args.extend(["-f", str(path)])
    args.extend(["config", "--format", "json"])
    env = dict(os.environ, COMPOSE_PROFILES="")
    result = subprocess.run(args, cwd=root, env=env, text=True, capture_output=True)
    if result.returncode:
        raise GateError("Compose config failed (details suppressed)")
    return json.loads(result.stdout)


def validate_delta(before: dict, after: dict) -> None:
    old = before["services"]
    new = after["services"]
    if set(old) != set(new):
        raise GateError("service inventory changed")
    changed = {name for name in old if old[name] != new[name]}
    if changed != {"grid-strategy"}:
        raise GateError("effective config changed outside grid-strategy")
    prior = old["grid-strategy"]
    candidate = new["grid-strategy"]
    fields = {key for key in set(prior) | set(candidate)
              if prior.get(key) != candidate.get(key)}
    if fields != {"environment", "image"}:
        raise GateError("grid-strategy change exceeds image and environment")
    old_env = prior["environment"]
    new_env = candidate["environment"]
    expected_env = dict(old_env, GRID_EXECUTION_HANDOFF_DB="/app/data/grid_execution.sqlite3")
    if new_env != expected_env:
        raise GateError("unexpected grid-strategy environment change")
    if not candidate["image"] or candidate["image"] == prior["image"]:
        raise GateError("candidate image missing or unchanged")


def require_execution_disabled(text: str, process_env: dict[str, str]) -> None:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'").lower()
    for key in ("GRID_EXECUTION_MODE", "GRID_EXECUTOR_DEPLOY_ENABLED"):
        if key in process_env:
            values[key] = process_env[key].strip().strip("\"'").lower()
    if values.get("GRID_EXECUTION_MODE", "off") != "off":
        raise GateError("grid execution mode must be off")
    if values.get("GRID_EXECUTOR_DEPLOY_ENABLED", "false") not in {"false", "0", ""}:
        raise GateError("grid executor deploy switch must be off")


def preflight(root: Path, overlay: Path) -> None:
    root = root.resolve()
    overlay = overlay.resolve()
    pin = json.loads((root / ".deploy_tmp/ledger-risk-active.json").read_text())
    if pin.get("sha") != EXPECTED_PIN_SHA or pin.get("status") != "active":
        raise GateError("active production pin changed")
    if digest(Path(pin["overlay"])) != EXPECTED_PIN_OVERLAY_SHA256:
        raise GateError("pinned image overlay changed")
    if digest(root / "docker-compose.yml") != EXPECTED_BASE_SHA256:
        raise GateError("production base Compose changed")
    if digest(root / "docker-compose.logging.yml") != EXPECTED_LOGGING_SHA256:
        raise GateError("production logging Compose changed")
    if not overlay.is_file():
        raise GateError("candidate overlay missing")
    require_execution_disabled((root / ".env").read_text(), os.environ)
    result = subprocess.run(["docker", "inspect", "trading_system-grid-executor"],
                            capture_output=True)
    if result.returncode == 0:
        raise GateError("grid executor container already exists")
    before = compose_config(root, Path(pin["overlay"]))
    after = compose_config(root, Path(pin["overlay"]), overlay)
    validate_delta(before, after)
    print("grid scoped release preflight passed: only grid-strategy image/environment differ")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/root/trading_system"))
    parser.add_argument("--candidate-overlay", required=True, type=Path)
    args = parser.parse_args()
    try:
        preflight(args.root, args.candidate_overlay)
    except (OSError, ValueError, KeyError, GateError) as exc:
        print("grid scoped release preflight blocked: " + type(exc).__name__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
