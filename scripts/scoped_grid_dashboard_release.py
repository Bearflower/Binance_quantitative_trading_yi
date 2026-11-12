#!/usr/bin/env python3
"""Pinned grid snapshot exporter plus dashboard-only presentation release."""
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
    Runtime as BaseRuntime, CONTAINERS, STACK_FILES, atomic_json,
)

PREDECESSOR_SHA = "7825607cb047e049d31c3b168373fa8959fbb78f"
PREDECESSOR_OVERLAY_SHA256 = "3a0cdca740307ec7824aa3a7d107e0a72722a8f425cbf11cde82a9e2c7b42654"
EXPORTER = "trading_system-grid-dashboard-exporter"
EXTRA_CONTAINERS = ("trading_system-grid-executor", "trading_system-grid-notifier")
MOUNT_TARGET = "/app/grid-dashboard"
DASHBOARD_SOURCE_PATHS = (
    "dashboard/backend/api/routes_docker.py",
    "dashboard/backend/services/grid_subaccount.py",
    "dashboard/frontend/index.html", "dashboard/frontend/css/style.css",
    "dashboard/frontend/js/api.js", "dashboard/frontend/js/main.js",
)
EXPORTER_SOURCE_PATHS = ("strategies/grid/execution/dashboard_snapshot.py",)
SOURCE_PATHS = DASHBOARD_SOURCE_PATHS + EXPORTER_SOURCE_PATHS
SCRIPT_PATHS = (
    "scripts/scoped_grid_dashboard_release.py",
    "scripts/dashboard_release_exchange_snapshot.py",
    "scripts/scoped_stop_followup.py", "scripts/stop_followup_manifest.py",
)


class ReleaseBlocked(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_overlay(predecessor: dict, dashboard_image: str, mount_source: Path) -> dict:
    if (not re.fullmatch(r"ghcr\.io/bearflower/trading-dashboard-api@sha256:[0-9a-f]{64}",
                         dashboard_image) or set(predecessor) != {"services"}
            or "dashboard-api" not in predecessor["services"]):
        raise ReleaseBlocked("invalid dashboard overlay input")
    result = json.loads(json.dumps(predecessor))
    service = result["services"]["dashboard-api"]
    if set(service) - {"image", "environment"} or "image" not in service:
        raise ReleaseBlocked("predecessor dashboard overlay drift")
    service["image"] = dashboard_image
    service["volumes"] = [str(mount_source) + ":" + MOUNT_TARGET + ":ro"]
    return result


def _api(path):
    with urlopen("http://127.0.0.1:8767" + path, timeout=25) as response:
        value = json.load(response)
    if value.get("code") != 0:
        raise ReleaseBlocked("dashboard API failure: " + path)
    return value.get("data") or {}


class Runtime(BaseRuntime):
    def __init__(self, root: Path, bundle: Path, sha: str):
        self.root, self.bundle, self.sha = root.resolve(), bundle.resolve(), sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or (bundle / "release-sha").read_text().strip() != sha:
            raise ReleaseBlocked("bundle/SHA mismatch")
        self.images = json.loads((bundle / "image-digests.json").read_text())
        if set(self.images) != {"dashboard-api", "grid-dashboard-exporter"}:
            raise ReleaseBlocked("exactly two image digests required")
        for name, image in self.images.items():
            if not re.fullmatch(r"ghcr\.io/bearflower/trading-" + name + r"@sha256:[0-9a-f]{64}", image):
                raise ReleaseBlocked("invalid image digest: " + name)
        self.hashes = json.loads((bundle / "source-checksums.json").read_text())
        if set(self.hashes) != set(SOURCE_PATHS) | set(SCRIPT_PATHS):
            raise ReleaseBlocked("incomplete source proof")
        for path in SCRIPT_PATHS:
            if sha256((bundle / path).read_bytes()) != self.hashes[path]:
                raise ReleaseBlocked("release script source mismatch: " + path)
        self.pin_file = self.root / ".deploy_tmp/ledger-risk-active.json"
        self.release_dir = self.root / ".releases/grid-subaccount-dashboard" / sha
        self.overlay = self.release_dir / "images.json"
        self.state = self.release_dir / "state.json"
        self.output_dir = self.root / "data/grid-dashboard"
        self.changed = []
        if self.release_dir.exists():
            raise ReleaseBlocked("release already attempted")

    def gate(self):
        source = (self.bundle / "scripts/dashboard_release_exchange_snapshot.py").read_text()
        output = self.run(["docker", "exec", "-i", "trading_system-hrs", "python", "-"],
                          input=source, timeout=180)
        proof = json.loads(output.splitlines()[-1])
        if proof.get("ok") is not True or proof.get("read_only") is not True:
            raise ReleaseBlocked("read-only main account snapshot unavailable")
        return proof

    def _inspect_name(self, name):
        rows = json.loads(self.run(["docker", "inspect", name]))
        if len(rows) != 1:
            raise ReleaseBlocked("ambiguous container: " + name)
        return rows[0]

    def preflight(self):
        self.pin = json.loads(self.pin_file.read_text())
        if self.pin.get("sha") != PREDECESSOR_SHA or self.pin.get("status") != "active":
            raise ReleaseBlocked("active release pin changed")
        prior_state = Path(self.pin.get("state", ""))
        if (not prior_state.is_file() or not prior_state.resolve().is_relative_to(self.root / ".releases")
                or json.loads(prior_state.read_text()).get("status") != "complete"):
            raise ReleaseBlocked("predecessor release incomplete")
        self.stacks = self.pin.get("stacks")
        if not isinstance(self.stacks, dict) or set(self.stacks) != set(CONTAINERS):
            raise ReleaseBlocked("pinned service inventory drift")
        for files in self.stacks.values():
            if not isinstance(files, list) or not files or files[0] != "docker-compose.yml" or not set(files) <= STACK_FILES:
                raise ReleaseBlocked("unsafe Compose stack")
        self.previous_overlay = Path(self.pin.get("overlay", ""))
        if (not self.previous_overlay.is_file() or
                not self.previous_overlay.resolve().is_relative_to(self.root / ".releases") or
                sha256(self.previous_overlay.read_bytes()) != PREDECESSOR_OVERLAY_SHA256):
            raise ReleaseBlocked("predecessor overlay drift")
        original = json.loads(self.previous_overlay.read_text())
        if set(original) != {"services"} or set(original["services"]) != set(self.stacks):
            raise ReleaseBlocked("predecessor image inventory drift")
        self.before = self.inspect("dashboard-api")
        if self.before["State"].get("Health", {}).get("Status") != "healthy":
            raise ReleaseBlocked("dashboard predecessor unhealthy")
        self.extra_before = {name: self._inspect_name(name) for name in EXTRA_CONTAINERS}
        if any(row["State"].get("Health", {}).get("Status") != "healthy"
               for row in self.extra_before.values()):
            raise ReleaseBlocked("grid executor or notifier unhealthy")
        if self.run(["systemctl", "is-active", "income-attribution.timer"]) != "active":
            raise ReleaseBlocked("income attribution timer inactive")
        self.all_before = {name: self.inspect(name) for name in self.stacks}
        self.inventory_before = self.project_inventory()
        self.policy_before = self.policy_snapshot()
        self.pre = self.gate()
        if self.run(["docker", "ps", "-a", "--filter", "name=^/" + EXPORTER + "$", "--format", "{{.Names}}"]):
            raise ReleaseBlocked("grid dashboard exporter already exists")
        self.release_dir.mkdir(parents=True, exist_ok=False)
        atomic_json(self.overlay, build_overlay(original, self.images["dashboard-api"], self.output_dir))
        old = json.loads(self.run(self.compose("dashboard-api", overlay=self.previous_overlay) +
                                  ["config", "--format", "json"]))["services"]["dashboard-api"]
        new = json.loads(self.run(self.compose("dashboard-api", overlay=self.overlay) +
                                  ["config", "--format", "json"]))["services"]["dashboard-api"]
        if ({k: v for k, v in old.items() if k not in ("image", "volumes")} !=
                {k: v for k, v in new.items() if k not in ("image", "volumes")}):
            raise ReleaseBlocked("dashboard Compose drift beyond image and read-only mount")
        volumes = new.get("volumes", [])
        if (len(volumes) != len(old.get("volumes", [])) + 1 or
                not any(v.get("source") == str(self.output_dir) and
                        v.get("target") == MOUNT_TARGET and v.get("read_only") is True
                        for v in volumes)):
            raise ReleaseBlocked("grid snapshot mount not read-only")
        networks = list(self.extra_before["trading_system-grid-executor"]["NetworkSettings"]["Networks"])
        if len(networks) != 1:
            raise ReleaseBlocked("grid executor network ambiguous")
        self.network = networks[0]
        for filename in ("grid-subaccount.env", "grid-limits.env"):
            secret = self.root / ".secrets" / filename
            if not secret.is_file() or secret.stat().st_mode & 0o077:
                raise ReleaseBlocked("grid secret file unavailable")
        atomic_json(self.release_dir / "exchange-before.json", self.pre)
        self.record("in_progress", "preflight")

    def pull_and_verify(self):
        for name, image in self.images.items():
            self.run(["docker", "pull", image], timeout=600)
            paths = DASHBOARD_SOURCE_PATHS if name == "dashboard-api" else EXPORTER_SOURCE_PATHS
            code = ("import hashlib,json,pathlib; p=pathlib.Path('/app'); "
                    "print(json.dumps({'version':(p/'VERSION').read_text(),"
                    "'hashes':{x:hashlib.sha256((p/x).read_bytes()).hexdigest() for x in "
                    + repr(paths) + "}}))")
            proof = json.loads(self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint",
                                         "python", image, "-c", code], timeout=180))
            if ("GIT_SHA=" + self.sha not in proof["version"].splitlines() or
                    proof["hashes"] != {path: self.hashes[path] for path in paths}):
                raise ReleaseBlocked("image source proof mismatch: " + name)
            atomic_json(self.release_dir / (name + "-proof.json"), proof)
        self.record("in_progress", "images_verified")

    def _healthy(self, name):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            row = self._inspect_name(name)
            if row["State"]["Status"] == "running" and row["State"].get("Health", {}).get("Status") == "healthy":
                return row
            if row["State"]["Status"] != "running" or row["State"].get("Health", {}).get("Status") == "unhealthy":
                raise ReleaseBlocked("container failed health: " + name)
            time.sleep(3)
        raise ReleaseBlocked("container health timeout: " + name)

    def activate(self):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.run(["docker", "run", "-d", "--name", EXPORTER, "--restart", "unless-stopped",
                      "--network", self.network,
                      "--env-file", str(self.root / ".secrets/grid-subaccount.env"),
                      "--env-file", str(self.root / ".secrets/grid-limits.env"),
                      "-e", "GRID_EXECUTION_MODE=live",
                      "-v", str(self.root / "data/grid") + ":/app/data:ro",
                      "-v", str(self.output_dir) + ":/app/output:rw",
                      "--health-cmd", "python -m strategies.grid.execution.dashboard_snapshot --healthcheck",
                      "--health-interval", "30s", "--health-timeout", "10s",
                      "--health-retries", "3", "--health-start-period", "60s",
                      "--log-driver", "journald", self.images["grid-dashboard-exporter"],
                      "python", "-m", "strategies.grid.execution.dashboard_snapshot"], timeout=120)
            self.changed.append(EXPORTER)
            self.record("in_progress", "exporter_started")
            exporter = self._healthy(EXPORTER)
            if exporter["Image"] != self.run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                              self.images["grid-dashboard-exporter"]]):
                raise ReleaseBlocked("exporter image mismatch")
            mounts = {row["Destination"]: row for row in exporter.get("Mounts", [])}
            if (mounts.get("/app/data", {}).get("RW") is not False or
                    mounts.get("/app/output", {}).get("RW") is not True):
                raise ReleaseBlocked("exporter mount permissions invalid")
            self.run(self.compose("dashboard-api", overlay=self.overlay) +
                     ["up", "-d", "--no-deps", "--pull", "never", "dashboard-api"], timeout=240)
            self.changed.append("dashboard-api")
            self.record("in_progress", "dashboard_replaced")
            dashboard = self._healthy("dashboard-api")
            if (self.identity(dashboard) == self.identity(self.before) or
                    dashboard["Image"] != self.run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                                   self.images["dashboard-api"]])):
                raise ReleaseBlocked("dashboard identity unchanged")
            dashboard_mounts = {row["Destination"]: row for row in dashboard.get("Mounts", [])}
            if dashboard_mounts.get(MOUNT_TARGET, {}).get("RW") is not False:
                raise ReleaseBlocked("dashboard snapshot mount is writable")
            grid = _api("/api/grid-subaccount")
            if (grid.get("status") != "ok" or grid.get("account_scope") != "grid_subaccount_pm"
                    or grid.get("symbol") != "ETHUSDT"):
                raise ReleaseBlocked("grid subaccount API unverified")
            overview = _api("/api/overview?type=monthly")
            if (overview.get("publication_status") != "published" or
                    overview.get("report_version") != "exchange-income-v1-derived-v1" or
                    overview.get("total_pnl") is None or overview.get("total_sharpe") is None):
                raise ReleaseBlocked("main account monthly overview changed")
            for period in ("daily", "weekly", "yearly"):
                _api("/api/overview?type=" + period)
            for name, row in self.all_before.items():
                if name != "dashboard-api" and self.identity(self.inspect(name)) != self.identity(row):
                    raise ReleaseBlocked("protected Compose container changed: " + name)
            for name, row in self.extra_before.items():
                if self.identity(self._inspect_name(name)) != self.identity(row):
                    raise ReleaseBlocked("grid executor or notifier changed: " + name)
            if self.project_inventory() != {**self.inventory_before,
                                            CONTAINERS["dashboard-api"]: self.identity(dashboard)}:
                raise ReleaseBlocked("Compose project inventory drift")
            if self.policy_snapshot() != self.policy_before or self.run(
                    ["systemctl", "is-active", "income-attribution.timer"]) != "active":
                raise ReleaseBlocked("risk policy or attribution timer changed")
            after = self.gate()
            atomic_json(self.release_dir / "exchange-after.json", after)
            atomic_json(self.release_dir / "grid-proof.json", grid)
            atomic_json(self.release_dir / "dashboard-proof.json",
                        {k: overview.get(k) for k in ("report_id", "total_pnl", "total_sharpe")})
            self.record("complete", "success")
            atomic_json(self.pin_file, dict(self.pin, sha=self.sha, status="active",
                                            overlay=str(self.overlay), state=str(self.state),
                                            predecessor_sha=PREDECESSOR_SHA))
        except Exception:
            try:
                if "dashboard-api" in self.changed:
                    self.run(self.compose("dashboard-api", overlay=self.previous_overlay) +
                             ["up", "-d", "--no-deps", "--pull", "never", "dashboard-api"], timeout=240)
                    restored = self._healthy("dashboard-api")
                    if restored["Image"] != self.before["Image"]:
                        raise ReleaseBlocked("dashboard rollback image mismatch")
                if EXPORTER in self.changed:
                    self.run(["docker", "rm", "-f", EXPORTER], timeout=60)
                self.record("failed", "rolled_back")
            except Exception:
                atomic_json(self.pin_file, dict(self.pin, status="grid_dashboard_rollback_failed",
                                                attempted_sha=self.sha))
                self.record("failed", "rollback_failed")
                raise
            raise


def main():
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
        print("grid dashboard release blocked/failed: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
