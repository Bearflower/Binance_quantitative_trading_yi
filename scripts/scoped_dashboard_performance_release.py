#!/usr/bin/env python3
"""Pinned dashboard-only release; trading containers and attribution timer stay put."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.scoped_stop_followup import (  # noqa: E402
    Runtime as BaseRuntime, CONTAINERS, STACK_FILES, atomic_json, image_only,
)

PREDECESSOR_SHA = "1d0a71a94f8ca1a4a9d77594899db0aa250d8fe1"
PREDECESSOR_OVERLAY_SHA256 = "b48984e0b91c883c963c981cf01f5a4e310285bcbda484e72abb62dfe529bc00"
SERVICE = "dashboard-api"
SOURCE_PATHS = (
    "dashboard/backend/services/exchange_ledger.py",
    "dashboard/backend/models/schemas.py",
    "dashboard/frontend/index.html",
    "dashboard/frontend/js/main.js",
    "shared/accounting/dashboard_performance.py",
)
SCRIPT_PATHS = (
    "scripts/scoped_dashboard_performance_release.py",
    "scripts/dashboard_release_exchange_snapshot.py",
    "scripts/scoped_stop_followup.py", "scripts/stop_followup_manifest.py",
)


class ReleaseBlocked(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_overlay(predecessor: dict, image: str) -> dict:
    if not re.fullmatch(r"ghcr\.io/bearflower/trading-dashboard-api@sha256:[0-9a-f]{64}", image):
        raise ReleaseBlocked("invalid immutable dashboard image")
    if set(predecessor) != {"services"} or SERVICE not in predecessor["services"]:
        raise ReleaseBlocked("predecessor overlay missing dashboard")
    result = json.loads(json.dumps(predecessor))
    result["services"][SERVICE]["image"] = image
    return result


def _overview_ready() -> dict:
    with urlopen("http://127.0.0.1:8767/api/overview?type=monthly", timeout=20) as response:
        payload = json.load(response)
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise ReleaseBlocked("dashboard overview API failed")
    data = payload["data"]
    if (data.get("publication_status") != "published" or
            data.get("report_version") != "exchange-income-v1-derived-v1" or
            data.get("total_pnl") is None or
            data.get("total_perf_sample_count", 0) < 2 or
            data.get("total_max_drawdown") is None or
            data.get("total_sharpe") is None or
            data.get("total_closed", 0) < 2 or
            data.get("win_rate") is None or
            data.get("win_sample_partial") is not True):
        raise ReleaseBlocked("dashboard monthly accounting/performance gate failed")
    return {key: data.get(key) for key in (
        "report_id", "report_version", "publication_status", "total_pnl",
        "total_perf_sample_count", "total_sharpe", "total_max_drawdown",
        "total_closed", "win_rate", "win_rate_reason",
    )}


class Runtime(BaseRuntime):
    def __init__(self, root: Path, bundle: Path, sha: str):
        self.root, self.bundle, self.sha = root.resolve(), bundle.resolve(), sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or (bundle / "release-sha").read_text().strip() != sha:
            raise ReleaseBlocked("bundle/SHA mismatch")
        self.images = json.loads((bundle / "image-digests.json").read_text())
        if set(self.images) != {SERVICE}:
            raise ReleaseBlocked("exactly one dashboard image required")
        self.image = self.images[SERVICE]
        if not re.fullmatch(r"ghcr\.io/bearflower/trading-dashboard-api@sha256:[0-9a-f]{64}", self.image):
            raise ReleaseBlocked("invalid dashboard image digest")
        self.hashes = json.loads((bundle / "source-checksums.json").read_text())
        if set(self.hashes) != set(SOURCE_PATHS) | set(SCRIPT_PATHS):
            raise ReleaseBlocked("incomplete source proof")
        for path in SCRIPT_PATHS:
            if sha256((bundle / path).read_bytes()) != self.hashes[path]:
                raise ReleaseBlocked("release script source mismatch: " + path)
        self.pin_file = self.root / ".deploy_tmp/ledger-risk-active.json"
        self.release_dir = self.root / ".releases/dashboard-performance" / sha
        self.overlay = self.release_dir / "images.json"
        self.state = self.release_dir / "state.json"
        self.changed = []
        if self.release_dir.exists():
            raise ReleaseBlocked("release already attempted")

    def gate(self) -> dict:
        source = (self.bundle / "scripts/dashboard_release_exchange_snapshot.py").read_text()
        output = self.run(["docker", "exec", "-i", "trading_system-hrs", "python", "-"],
                          input=source, timeout=180)
        proof = json.loads(output.splitlines()[-1])
        if proof.get("ok") is not True or proof.get("read_only") is not True:
            raise ReleaseBlocked("read-only exchange snapshot unavailable")
        return proof

    def preflight(self) -> None:
        self.pin = json.loads(self.pin_file.read_text())
        if self.pin.get("sha") != PREDECESSOR_SHA or self.pin.get("status") != "active":
            raise ReleaseBlocked("active release pin changed")
        prior_state = Path(self.pin.get("state", ""))
        if not prior_state.is_file() or not prior_state.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("predecessor state unavailable")
        if json.loads(prior_state.read_text()).get("status") != "complete":
            raise ReleaseBlocked("predecessor release incomplete")
        prior_proof = prior_state.parent / "exchange-after.json"
        if not prior_proof.is_file() or json.loads(prior_proof.read_text()).get("ok") is not True:
            raise ReleaseBlocked("predecessor exchange proof missing")
        self.stacks = self.pin.get("stacks")
        if not isinstance(self.stacks, dict) or set(self.stacks) != set(CONTAINERS):
            raise ReleaseBlocked("pinned service inventory changed")
        for stacks in self.stacks.values():
            if not isinstance(stacks, list) or not stacks or stacks[0] != "docker-compose.yml" or not set(stacks) <= STACK_FILES:
                raise ReleaseBlocked("unsafe Compose stack")
        self.previous_overlay = Path(self.pin.get("overlay", ""))
        if (not self.previous_overlay.is_file() or
                not self.previous_overlay.resolve().is_relative_to(self.root / ".releases") or
                sha256(self.previous_overlay.read_bytes()) != PREDECESSOR_OVERLAY_SHA256):
            raise ReleaseBlocked("predecessor overlay drift")
        original = json.loads(self.previous_overlay.read_text())
        if set(original) != {"services"} or set(original["services"]) != set(self.stacks):
            raise ReleaseBlocked("predecessor image inventory drift")
        self.before = self.inspect(SERVICE)
        if (self.before["State"]["Status"] != "running" or
                self.before["State"].get("Health", {}).get("Status") != "healthy"):
            raise ReleaseBlocked("dashboard predecessor unhealthy")
        old_ref = original["services"][SERVICE]["image"]
        if self.before["Image"] != self.run(["docker", "image", "inspect", "--format", "{{.Id}}", old_ref]):
            raise ReleaseBlocked("dashboard predecessor image drift")
        if self.run(["systemctl", "is-active", "income-attribution.timer"]) != "active":
            raise ReleaseBlocked("income attribution timer inactive")
        self.all_before = {name: self.inspect(name) for name in self.stacks}
        self.inventory_before = self.project_inventory()
        self.policy_before = self.policy_snapshot()
        self.pre = self.gate()
        self.release_dir.mkdir(parents=True, exist_ok=False)
        self.overlay.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.overlay, build_overlay(original, self.image))
        old_config = json.loads(self.run(self.compose(SERVICE, overlay=self.previous_overlay) +
                                         ["config", "--format", "json"]))["services"][SERVICE]
        new_config = json.loads(self.run(self.compose(SERVICE, overlay=self.overlay) +
                                         ["config", "--format", "json"]))["services"][SERVICE]
        if not image_only(old_config, new_config):
            raise ReleaseBlocked("effective dashboard Compose drift beyond image")
        atomic_json(self.release_dir / "exchange-before.json", self.pre)
        self.record("in_progress", "preflight")

    def pull_and_verify(self) -> None:
        self.run(["docker", "pull", self.image], timeout=600)
        code = ("import hashlib,json,pathlib; p=pathlib.Path('/app'); "
                "print(json.dumps({'version':(p/'VERSION').read_text(),"
                "'hashes':{x:hashlib.sha256((p/x).read_bytes()).hexdigest() for x in "
                + repr(SOURCE_PATHS) + "}}))")
        proof = json.loads(self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint",
                                     "python", self.image, "-c", code], timeout=180))
        if ("GIT_SHA=" + self.sha not in proof["version"].splitlines() or
                proof["hashes"] != {path: self.hashes[path] for path in SOURCE_PATHS}):
            raise ReleaseBlocked("dashboard image source proof mismatch")
        atomic_json(self.release_dir / "image-proof.json", proof)
        self.record("in_progress", "image_verified")

    def _healthy(self) -> dict:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            row = self.inspect(SERVICE)
            if row["State"]["Status"] == "running" and row["State"].get("Health", {}).get("Status") == "healthy":
                return row
            time.sleep(3)
        raise ReleaseBlocked("dashboard container health timeout")

    def activate(self) -> None:
        try:
            self.run(self.compose(SERVICE, overlay=self.overlay) +
                     ["up", "-d", "--no-deps", "--pull", "never", SERVICE], timeout=240)
            self.changed.append(SERVICE)
            self.record("in_progress", "dashboard_replaced")
            row = self._healthy()
            if (row["Image"] != self.run(["docker", "image", "inspect", "--format", "{{.Id}}", self.image])
                    or self.identity(row) == self.identity(self.before)):
                raise ReleaseBlocked("dashboard image/identity mismatch")
            proof = _overview_ready()
            for report_type in ("daily", "weekly", "yearly"):
                with urlopen("http://127.0.0.1:8767/api/overview?type=" + report_type,
                             timeout=20) as response:
                    if json.load(response).get("code") != 0:
                        raise ReleaseBlocked("dashboard period API failed: " + report_type)
            if self.run(["systemctl", "is-active", "income-attribution.timer"]) != "active":
                raise ReleaseBlocked("income attribution timer changed")
            for service, before in self.all_before.items():
                if service != SERVICE and self.identity(self.inspect(service)) != self.identity(before):
                    raise ReleaseBlocked("trading or auxiliary container changed: " + service)
            inventory_after = self.project_inventory()
            if set(inventory_after) != set(self.inventory_before):
                raise ReleaseBlocked("project container inventory changed")
            for name, before in self.inventory_before.items():
                if name != CONTAINERS[SERVICE] and inventory_after[name] != before:
                    raise ReleaseBlocked("excluded project container identity changed: " + name)
            if self.policy_snapshot() != self.policy_before:
                raise ReleaseBlocked("risk policy changed during dashboard release")
            after = self.gate()
            atomic_json(self.release_dir / "exchange-after.json", after)
            atomic_json(self.release_dir / "dashboard-proof.json", proof)
            self.record("complete", "success")
            atomic_json(self.pin_file, dict(self.pin, sha=self.sha, status="active",
                                            overlay=str(self.overlay), state=str(self.state),
                                            predecessor_sha=PREDECESSOR_SHA))
        except Exception:
            try:
                self.run(self.compose(SERVICE, overlay=self.previous_overlay) +
                         ["up", "-d", "--no-deps", "--pull", "never", SERVICE], timeout=240)
                restored = self._healthy()
                if restored["Image"] != self.before["Image"]:
                    raise ReleaseBlocked("dashboard rollback image mismatch")
                self.record("failed", "rolled_back")
            except Exception:
                atomic_json(self.pin_file, dict(self.pin, status="dashboard_rollback_failed",
                                                attempted_sha=self.sha))
                self.record("failed", "rollback_failed")
                raise
            raise


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
        if runtime is not None and runtime.release_dir.exists() and not runtime.state.exists():
            runtime.record("failed", type(exc).__name__)
        print("dashboard performance release blocked/failed: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
