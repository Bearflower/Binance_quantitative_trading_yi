#!/usr/bin/env python3
"""One-shot grid image release under the active ledger/risk pin.

The CI runtime holds compose_deploy.lock. This release leaves order execution off.
Any partial activation remains explicit for manual review; no trading container is
automatically rolled back while exchange state may be changing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.scoped_stop_followup import (  # noqa: E402
    Runtime as BaseRuntime, CONTAINERS, STACK_FILES, atomic_json,
    unchanged_exchange,
)


PIN_SHA = "503f7e562c9123eac14b6dfaba9028cd20fef247"
PIN_OVERLAY_SHA256 = "c806f1bd2e0cb9e5ac1182b4977003531a22ef9a2ae970cd3e3fef87f637c8bf"
BASE_SHA256 = "64b527867d9097651edaa19a8a35eab0c47fd9d8d40f6cad407770ea82ed784c"
LOGGING_SHA256 = "304914aa828ce52f43286a5cf276f7006cec07b127a4e668bcb1bd01f44eae72"
RELEASE = "grid-pm-oneway-20261111"
SERVICE = "grid-strategy"
IMAGE_PATHS = (
    "shared/binance_api.py", "strategies/grid/signal_bot.py",
    "strategies/grid/execution/main.py", "strategies/grid/execution/engine.py",
    "strategies/grid/execution/exchange.py", "strategies/grid/execution/store.py",
)


class ReleaseBlocked(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def disabled(env_text: str) -> bool:
    values: dict[str, str] = {}
    for line in env_text.splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'").lower()
    for key in ("GRID_EXECUTION_MODE", "GRID_EXECUTOR_DEPLOY_ENABLED"):
        if key in os.environ:
            values[key] = os.environ[key].strip().strip("\"'").lower()
    return (values.get("GRID_EXECUTION_MODE", "off") == "off"
            and values.get("GRID_EXECUTOR_DEPLOY_ENABLED", "false") in {"false", "0", ""})


def expected_grid_delta(old: dict, new: dict) -> bool:
    without_image_env = lambda row: {k: v for k, v in row.items() if k not in {"image", "environment"}}
    if without_image_env(old) != without_image_env(new):
        return False
    return (new.get("image") != old.get("image")
            and new.get("environment") == dict(
                old.get("environment", {}),
                GRID_EXECUTION_HANDOFF_DB="/app/data/grid_execution.sqlite3"))


class Runtime(BaseRuntime):
    def __init__(self, root: Path, bundle: Path, sha: str):
        self.root, self.bundle, self.sha = root.resolve(), bundle.resolve(), sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ReleaseBlocked("invalid release SHA")
        if (bundle / "release-sha").read_text().strip() != sha:
            raise ReleaseBlocked("bundle/SHA mismatch")
        self.image = json.loads((bundle / "image-digests.json").read_text())
        self.images = self.image
        if (set(self.image) != {"grid"} or not re.fullmatch(
                r"ghcr\.io/bearflower/trading-grid@sha256:[0-9a-f]{64}", self.image["grid"])):
            raise ReleaseBlocked("one immutable grid image required")
        self.hashes = json.loads((bundle / "source-checksums.json").read_text())
        script_paths = {"scripts/scoped_grid_release.py", "scripts/scoped_stop_followup.py",
                        "scripts/stop_followup_manifest.py"}
        if set(self.hashes) != set(IMAGE_PATHS) | script_paths:
            raise ReleaseBlocked("incomplete source proof")
        for path in script_paths:
            if sha256((bundle / path).read_bytes()) != self.hashes[path]:
                raise ReleaseBlocked("release script checksum mismatch: " + path)
        self.pin_file = self.root / ".deploy_tmp/ledger-risk-active.json"
        self.release_dir = self.root / ".releases" / RELEASE / sha
        self.overlay = self.release_dir / "images.json"
        self.executor_overlay = self.release_dir / "executor.json"
        self.state = self.release_dir / "state.json"
        self.changed: list[str] = []
        if self.release_dir.exists():
            raise ReleaseBlocked("release already attempted")

    def preflight(self) -> None:
        if sha256((self.root / "docker-compose.yml").read_bytes()) != BASE_SHA256:
            raise ReleaseBlocked("base Compose changed")
        if sha256((self.root / "docker-compose.logging.yml").read_bytes()) != LOGGING_SHA256:
            raise ReleaseBlocked("logging Compose changed")
        if not disabled((self.root / ".env").read_text()):
            raise ReleaseBlocked("grid execution must remain off")
        if subprocess.run(["docker", "inspect", "trading_system-grid-executor"],
                          capture_output=True).returncode == 0:
            raise ReleaseBlocked("executor container already exists")
        self.pin = json.loads(self.pin_file.read_text())
        if self.pin.get("sha") != PIN_SHA or self.pin.get("status") != "active":
            raise ReleaseBlocked("active pin changed")
        prior_state = Path(self.pin.get("state", ""))
        if not prior_state.is_file() or not prior_state.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("active pin state unavailable")
        if json.loads(prior_state.read_text()).get("status") != "complete":
            raise ReleaseBlocked("predecessor completion proof unavailable")
        prior_exchange = prior_state.parent / "exchange-after.json"
        proof = json.loads(prior_exchange.read_text())
        if proof.get("ok") is not True or proof.get("read_only") is not True:
            raise ReleaseBlocked("predecessor exchange proof invalid")
        self.stacks = self.pin.get("stacks")
        if not isinstance(self.stacks, dict) or set(self.stacks) != set(CONTAINERS):
            raise ReleaseBlocked("pinned service inventory changed")
        for stacks in self.stacks.values():
            if (not isinstance(stacks, list) or not stacks or stacks[0] != "docker-compose.yml"
                    or not set(stacks) <= STACK_FILES or len(stacks) != len(set(stacks))):
                raise ReleaseBlocked("unsafe Compose stack")
        predecessor = Path(self.pin.get("overlay", ""))
        if not predecessor.is_file() or not predecessor.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("active image overlay unavailable")
        if sha256(predecessor.read_bytes()) != PIN_OVERLAY_SHA256:
            raise ReleaseBlocked("active image overlay changed")
        original = json.loads(predecessor.read_text())
        if set(original) != {"services"} or set(original["services"]) != set(self.stacks):
            raise ReleaseBlocked("incomplete image pins")
        for row in original["services"].values():
            if set(row) != {"image"} or not re.fullmatch(
                    r"ghcr\.io/bearflower/trading-[a-z0-9-]+@sha256:[0-9a-f]{64}", row["image"]):
                raise ReleaseBlocked("invalid predecessor image pin")
        self.pre = self.gate()
        self.policy_before = self.policy_snapshot()
        self.all_before = {service: self.inspect(service) for service in self.stacks}
        self.inventory_before = self.project_inventory()
        if not set(CONTAINERS.values()) <= set(self.inventory_before):
            raise ReleaseBlocked("pinned container missing")
        for service, row in self.all_before.items():
            if row["State"]["Status"] != "running" or row["State"].get("Health", {}).get("Status") != "healthy":
                raise ReleaseBlocked("predecessor unhealthy: " + service)
            # Dashboard has an independently pinned income attribution image.
            if service != "dashboard-api":
                expected = self.run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                     original["services"][service]["image"]])
                if row["Image"] != expected:
                    raise ReleaseBlocked("predecessor image drift: " + service)
        self.release_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(self.release_dir, 0o700)
        successor = json.loads(json.dumps(original))
        successor["services"][SERVICE] = {
            "image": self.image["grid"],
            "environment": {"GRID_EXECUTION_HANDOFF_DB": "/app/data/grid_execution.sqlite3"},
        }
        atomic_json(self.overlay, successor)
        atomic_json(self.executor_overlay, {"services": {"grid-executor": {
            "profiles": ["grid-executor"],
            "image": self.image["grid"],
            "container_name": "trading_system-grid-executor",
            "restart": "unless-stopped",
            "command": ["python", "-m", "strategies.grid.execution.main"],
            "env_file": [str(self.root / ".secrets/grid-subaccount.env"),
                         str(self.root / ".secrets/grid-limits.env")],
            "environment": {
                "GRID_EXECUTION_DB": "/app/data/grid_execution.sqlite3",
                "GRID_EXECUTION_LOCK": "/app/data/grid_execution.lock",
                "GRID_EXECUTION_HEARTBEAT": "/app/data/grid_execution_heartbeat.json",
                "TZ": "Asia/Shanghai",
            },
            "volumes": [str(self.root / "data/grid") + ":/app/data",
                        str(self.root / "logs/grid") + ":/app/logs"],
            "networks": ["trading-network"],
            "depends_on": {"grid-strategy": {"condition": "service_started"}},
            "logging": {"driver": "journald"},
            "healthcheck": {
                "test": ["CMD", "python", "-m", "strategies.grid.execution.main", "--healthcheck"],
                "interval": "30s", "timeout": "10s", "retries": 3, "start_period": "90s",
            },
        }}})
        executor_config = json.loads(self.run(
            self.compose(SERVICE, overlay=self.overlay)[:]
            + ["-f", str(self.executor_overlay), "--profile", "grid-executor",
               "config", "--format", "json"]))["services"]
        if ("grid-executor" not in executor_config
                or executor_config["grid-executor"].get("image") != self.image["grid"]
                or executor_config["grid-executor"].get("environment", {}).get("GRID_EXECUTION_MODE") != "off"):
            raise ReleaseBlocked("disabled executor Compose overlay invalid")
        for service in self.stacks:
            old = json.loads(self.run(self.compose(service, overlay=predecessor) + ["config", "--format", "json"]))["services"][service]
            new = json.loads(self.run(self.compose(service, overlay=self.overlay) + ["config", "--format", "json"]))["services"][service]
            if service == SERVICE:
                if not expected_grid_delta(old, new):
                    raise ReleaseBlocked("grid effective config exceeds image and handoff path")
            elif old != new:
                raise ReleaseBlocked("excluded effective config changed: " + service)
        atomic_json(self.release_dir / "containers-before.json",
                    {s: {"identity": self.identity(row), "health": row["State"].get("Health", {}).get("Status")}
                     for s, row in self.all_before.items()})
        atomic_json(self.release_dir / "exchange-before.json", self.pre)
        self.record("in_progress", "preflight")

    def pull_verify_and_migrate(self) -> None:
        self.run(["docker", "pull", self.image["grid"]], timeout=600)
        code = ("import hashlib,json,pathlib; p=pathlib.Path('/app'); "
                "print(json.dumps({'version':(p/'VERSION').read_text(),"
                "'hashes':{x:hashlib.sha256((p/x).read_bytes()).hexdigest() for x in "
                + repr(IMAGE_PATHS) + "}}))")
        proof = json.loads(self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint",
                                     "python", self.image["grid"], "-c", code], timeout=180))
        if "GIT_SHA=" + self.sha not in proof["version"].splitlines():
            raise ReleaseBlocked("image commit mismatch")
        if proof["hashes"] != {path: self.hashes[path] for path in IMAGE_PATHS}:
            raise ReleaseBlocked("image source mismatch")
        atomic_json(self.release_dir / "image-proof.json", proof)
        db = self.root / "data/grid/grid_execution.sqlite3"
        if db.exists():
            # A previously active writer cannot be safely copied or migrated here.
            raise ReleaseBlocked("execution journal already exists; manual migration review required")
        self.run(["docker", "run", "--rm", "--network", "none", "-v",
                  str(self.root / "data/grid") + ":/app/data", "--entrypoint", "python",
                  self.image["grid"], "-m", "strategies.grid.execution.store", "--db-path",
                  "/app/data/grid_execution.sqlite3"], timeout=180)
        if not db.is_file():
            raise ReleaseBlocked("journal migration did not create database")
        self.record("in_progress", "image_and_journal_verified")

    def activate(self) -> None:
        unchanged_exchange(self.pre, self.gate())
        if self.policy_snapshot() != self.policy_before:
            raise ReleaseBlocked("risk policy changed before activation")
        atomic_json(self.pin_file, dict(self.pin, status="activating", successor_sha=self.sha))
        self.record("in_progress", "activation_started")
        old = self.all_before[SERVICE]
        self.run(self.compose(SERVICE, overlay=self.overlay) + ["up", "-d", "--no-deps", SERVICE], timeout=180)
        self.changed.append(SERVICE)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = self.inspect(SERVICE)
            if current["State"]["Status"] == "running" and current["State"].get("Health", {}).get("Status") == "healthy":
                break
            time.sleep(3)
        else:
            raise ReleaseBlocked("grid health gate failed")
        image_id = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", self.image["grid"]])
        if current["Image"] != image_id or self.identity(current) == self.identity(old):
            raise ReleaseBlocked("grid image or container identity mismatch")
        logs = self.run(["docker", "logs", "--since", current["State"]["StartedAt"], current["Id"]],
                        include_stderr=True)
        if "Traceback (most recent call last)" in logs:
            raise ReleaseBlocked("grid startup traceback")
        self.record("in_progress", "grid_healthy")
        after = self.gate()
        unchanged_exchange(self.pre, after)
        if self.policy_snapshot() != self.policy_before:
            raise ReleaseBlocked("risk policy changed during release")
        for service, before in self.all_before.items():
            current = self.inspect(service)
            if service != SERVICE and self.identity(current) != self.identity(before):
                raise ReleaseBlocked("excluded service changed: " + service)
        inventory_after = self.project_inventory()
        if set(inventory_after) != set(self.inventory_before):
            raise ReleaseBlocked("project container inventory changed")
        for name, identity in self.inventory_before.items():
            if name != CONTAINERS[SERVICE] and inventory_after[name] != identity:
                raise ReleaseBlocked("excluded project container changed: " + name)
        atomic_json(self.release_dir / "exchange-after.json", after)
        self.record("complete", "success")
        atomic_json(self.pin_file, dict(self.pin, sha=self.sha, status="active", overlay=str(self.overlay),
                                        state=str(self.state), predecessor_sha=PIN_SHA,
                                        successor_sha=self.sha))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/root/trading_system"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    runtime = None
    try:
        runtime = Runtime(args.root, args.bundle, args.sha)
        runtime.preflight()
        runtime.pull_verify_and_migrate()
        runtime.activate()
    except Exception as exc:
        if runtime is not None and runtime.release_dir.exists():
            runtime.record("failed", type(exc).__name__)
        print("grid scoped release blocked/failed: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
