#!/usr/bin/env python3
"""One-shot pinned release of the seven-gap code changes.

The CI runtime holds the Compose deployment lock. Any partial activation stays
visible for manual exchange reconciliation; trading images are never auto-rolled
back across a changing market.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.scoped_stop_followup import (  # noqa: E402
    Runtime as BaseRuntime, CONTAINERS, STACK_FILES, atomic_json, image_only,
    unchanged_exchange,
)

RELEASE = "seven-code-gaps-20261112"
PIN_SHA = "4faafd876832e6b87dd7265a37a6fe203eb3d019"
PIN_OVERLAY_SHA256 = "d0345541b4731c25d986a85419306e1a2e09dadaa5edf6a77eab29331edb5f14"
INCOME_SHA = "97fb429a4c7f288e7f9506e54233f37d15cd780f"
BASE_SHA256 = "64b527867d9097651edaa19a8a35eab0c47fd9d8d40f6cad407770ea82ed784c"
LOGGING_SHA256 = "304914aa828ce52f43286a5cf276f7006cec07b127a4e668bcb1bd01f44eae72"
IMAGE_SERVICES = {
    "btc-eth-aggr": "btc-eth-aggressive-strategy",
    "new-coin": "new-coin-strategy",
    "hrs": "hrs-strategy",
    "ai-tuner": "ai-tuner",
    "dashboard-api": "dashboard-api",
}
IMAGE_PATHS = {
    "btc-eth-aggr": ("strategies/btc_eth_aggressive/strategy.py",),
    "new-coin": ("strategies/new_coin/executor.py", "strategies/new_coin/strategy.py",
                 "strategies/new_coin/recovery_evidence.py"),
    "hrs": ("strategies/hrs/strategy.py",),
    "ai-tuner": ("ai_tuner/monitor/reduced_trial.py", "ai_tuner/allocation/shadow_allocation.py",
                 "shared/accounting/cashflow_roi.py"),
    "dashboard-api": ("dashboard/backend/models/schemas.py", "dashboard/frontend/index.html",
                      "dashboard/frontend/js/main.js", "shared/accounting/cashflow_roi.py"),
}
SCRIPTS = ("scripts/scoped_seven_gaps_release.py", "scripts/scoped_stop_followup.py",
           "scripts/stop_followup_manifest.py")


class ReleaseBlocked(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Runtime(BaseRuntime):
    def __init__(self, root: Path, bundle: Path, sha: str):
        self.root, self.bundle, self.sha = root.resolve(), bundle.resolve(), sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or (bundle / "release-sha").read_text().strip() != sha:
            raise ReleaseBlocked("release SHA/bundle mismatch")
        self.images = json.loads((bundle / "image-digests.json").read_text())
        if set(self.images) != set(IMAGE_SERVICES):
            raise ReleaseBlocked("exact five-image release required")
        for name, ref in self.images.items():
            if not re.fullmatch(rf"ghcr\.io/bearflower/trading-{re.escape(name)}@sha256:[0-9a-f]{{64}}", ref):
                raise ReleaseBlocked("invalid immutable image digest: " + name)
        self.hashes = json.loads((bundle / "source-checksums.json").read_text())
        expected = set(SCRIPTS) | {p for paths in IMAGE_PATHS.values() for p in paths}
        if set(self.hashes) != expected:
            raise ReleaseBlocked("incomplete source proof")
        for path in SCRIPTS:
            if sha256((bundle / path).read_bytes()) != self.hashes[path]:
                raise ReleaseBlocked("release source mismatch: " + path)
        self.pin_file = self.root / ".deploy_tmp/ledger-risk-active.json"
        self.release_dir = self.root / ".releases" / RELEASE / sha
        self.overlay = self.release_dir / "images.json"
        self.state = self.release_dir / "state.json"
        self.changed: list[str] = []
        if self.release_dir.exists():
            raise ReleaseBlocked("release already attempted")

    def preflight(self) -> None:
        if sha256((self.root / "docker-compose.yml").read_bytes()) != BASE_SHA256:
            raise ReleaseBlocked("base Compose changed")
        if sha256((self.root / "docker-compose.logging.yml").read_bytes()) != LOGGING_SHA256:
            raise ReleaseBlocked("logging Compose changed")
        self.pin = json.loads(self.pin_file.read_text())
        if self.pin.get("sha") != PIN_SHA or self.pin.get("status") != "active":
            raise ReleaseBlocked("active predecessor pin changed")
        prior_state = Path(self.pin.get("state", ""))
        if not prior_state.is_file() or not prior_state.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("predecessor state unavailable")
        if json.loads(prior_state.read_text()).get("status") != "complete":
            raise ReleaseBlocked("predecessor incomplete")
        proof = json.loads((prior_state.parent / "exchange-after.json").read_text())
        if proof.get("ok") is not True or proof.get("read_only") is not True:
            raise ReleaseBlocked("predecessor exchange proof invalid")
        self.stacks = self.pin.get("stacks")
        if not isinstance(self.stacks, dict) or set(self.stacks) != set(CONTAINERS):
            raise ReleaseBlocked("pinned service inventory changed")
        for stacks in self.stacks.values():
            if not isinstance(stacks, list) or not stacks or stacks[0] != "docker-compose.yml" or not set(stacks) <= STACK_FILES:
                raise ReleaseBlocked("unsafe Compose stack")
        predecessor = Path(self.pin.get("overlay", ""))
        if not predecessor.is_file() or not predecessor.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("predecessor overlay unavailable")
        if sha256(predecessor.read_bytes()) != PIN_OVERLAY_SHA256:
            raise ReleaseBlocked("predecessor overlay changed")
        original = json.loads(predecessor.read_text())
        if set(original) != {"services"} or set(original["services"]) != set(self.stacks):
            raise ReleaseBlocked("incomplete image pins")
        income_dir = self.root / ".releases/income-attribution" / INCOME_SHA
        income_state = json.loads((income_dir / "state.json").read_text())
        if income_state.get("status") != "active" or income_state.get("sha") != INCOME_SHA:
            raise ReleaseBlocked("income attribution release changed")
        self.income_overlay = income_dir / "dashboard-overlay.yml"
        if self.run(["systemctl", "is-active", "income-attribution.timer"]) != "active":
            raise ReleaseBlocked("income attribution timer inactive")
        self.income_image = income_state["images"]["dashboard-api"]
        if self.income_image not in self.income_overlay.read_text():
            raise ReleaseBlocked("income dashboard proof drift")
        self.pre = self.gate()
        self.policy_before = self.policy_snapshot()
        self.all_before = {s: self.inspect(s) for s in self.stacks}
        self.inventory_before = self.project_inventory()
        if not set(CONTAINERS.values()) <= set(self.inventory_before):
            raise ReleaseBlocked("pinned project container missing")
        for service, row in self.all_before.items():
            if row["State"]["Status"] != "running" or row["State"].get("Health", {}).get("Status") != "healthy":
                raise ReleaseBlocked("predecessor unhealthy: " + service)
            ref = self.income_image if service == "dashboard-api" else original["services"][service]["image"]
            if row["Image"] != self.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref]):
                raise ReleaseBlocked("predecessor image drift: " + service)
        self.release_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(self.release_dir, 0o700)
        successor = json.loads(json.dumps(original))
        for name, service in IMAGE_SERVICES.items():
            successor["services"][service]["image"] = self.images[name]
        successor["services"]["dashboard-api"]["environment"] = {"LEDGER_DERIVED_ATTRIBUTION_ENABLED": "1"}
        atomic_json(self.overlay, successor)
        for service in self.stacks:
            old_overlay = self.income_overlay if service == "dashboard-api" else predecessor
            old = json.loads(self.run(self.compose(service, overlay=old_overlay) + ["config", "--format", "json"]))["services"][service]
            new = json.loads(self.run(self.compose(service, overlay=self.overlay) + ["config", "--format", "json"]))["services"][service]
            if service in IMAGE_SERVICES.values():
                if not image_only(old, new):
                    raise ReleaseBlocked("effective Compose drift beyond image: " + service)
            elif old != new:
                raise ReleaseBlocked("excluded Compose drift: " + service)
        atomic_json(self.release_dir / "exchange-before.json", self.pre)
        atomic_json(self.release_dir / "containers-before.json", {
            s: {"identity": self.identity(row), "health": row["State"].get("Health", {}).get("Status")}
            for s, row in self.all_before.items()})
        self.record("in_progress", "preflight")

    def pull_and_verify(self) -> None:
        proofs = {}
        for name, paths in IMAGE_PATHS.items():
            image = self.images[name]
            self.run(["docker", "pull", image], timeout=600)
            code = ("import hashlib,json,pathlib; p=pathlib.Path('/app'); "
                    "print(json.dumps({'version':(p/'VERSION').read_text(),"
                    "'hashes':{x:hashlib.sha256((p/x).read_bytes()).hexdigest() for x in "
                    + repr(paths) + "}}))")
            proof = json.loads(self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint",
                                         "python", image, "-c", code], timeout=180))
            if "GIT_SHA=" + self.sha not in proof["version"].splitlines():
                raise ReleaseBlocked("image commit mismatch: " + name)
            if proof["hashes"] != {path: self.hashes[path] for path in paths}:
                raise ReleaseBlocked("image source mismatch: " + name)
            proofs[name] = proof
        atomic_json(self.release_dir / "image-proof.json", proofs)
        self.record("in_progress", "images_verified")

    def activate(self) -> None:
        unchanged_exchange(self.pre, self.gate())
        if self.policy_snapshot() != self.policy_before:
            raise ReleaseBlocked("risk policy changed before activation")
        atomic_json(self.pin_file, dict(self.pin, status="activating", successor_sha=self.sha))
        self.record("in_progress", "activation_started")
        for name, service in IMAGE_SERVICES.items():
            old = self.all_before[service]
            self.run(self.compose(service, overlay=self.overlay) + ["up", "-d", "--no-deps", "--pull", "never", service], timeout=240)
            self.changed.append(service)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                row = self.inspect(service)
                if row["State"]["Status"] == "running" and row["State"].get("Health", {}).get("Status") == "healthy":
                    break
                time.sleep(3)
            else:
                raise ReleaseBlocked("successor health gate failed: " + service)
            if row["Image"] != self.run(["docker", "image", "inspect", "--format", "{{.Id}}", self.images[name]]) or self.identity(row) == self.identity(old):
                raise ReleaseBlocked("successor image/identity mismatch: " + service)
            logs = self.run(["docker", "logs", "--since", row["State"]["StartedAt"], row["Id"]], include_stderr=True)
            if "Traceback (most recent call last)" in logs:
                raise ReleaseBlocked("startup traceback: " + service)
            self.record("in_progress", "healthy_" + service)
        after = self.gate()
        unchanged_exchange(self.pre, after)
        if self.policy_snapshot() != self.policy_before:
            raise ReleaseBlocked("risk policy changed during release")
        if self.run(["systemctl", "is-active", "income-attribution.timer"]) != "active":
            raise ReleaseBlocked("income attribution timer changed")
        for service, before in self.all_before.items():
            if service not in IMAGE_SERVICES.values() and self.identity(self.inspect(service)) != self.identity(before):
                raise ReleaseBlocked("excluded service changed: " + service)
        inventory_after = self.project_inventory()
        if set(inventory_after) != set(self.inventory_before):
            raise ReleaseBlocked("project inventory changed")
        for name, identity in self.inventory_before.items():
            if name not in {CONTAINERS[s] for s in IMAGE_SERVICES.values()} and inventory_after[name] != identity:
                raise ReleaseBlocked("excluded project container changed: " + name)
        atomic_json(self.release_dir / "exchange-after.json", after)
        self.record("complete", "success")
        atomic_json(self.pin_file, dict(self.pin, sha=self.sha, status="active", overlay=str(self.overlay),
                                        state=str(self.state), predecessor_sha=PIN_SHA, successor_sha=self.sha))


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
        runtime.pull_and_verify()
        runtime.activate()
    except Exception as exc:
        if runtime is not None and runtime.release_dir.exists():
            runtime.record("failed", type(exc).__name__)
        print("seven gaps scoped release blocked/failed: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
