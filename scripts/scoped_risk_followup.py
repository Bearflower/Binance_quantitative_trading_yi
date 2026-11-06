#!/usr/bin/env python3
"""One-shot successor for the active risk pin: HRS verifier and aggressive entry.

The workflow holds compose_deploy.lock before invoking this file. A failed partial
activation remains pinned for manual exchange/order reconciliation; it is never
replayed or automatically rolled back into a potentially different live market.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.risk_followup_manifest import (
    ACTIVE_PIN_SHA, IMAGES, MANIFEST, PIN_OVERLAY_SHA256, RELEASE,
    ReleaseBlocked, sha256, validate_manifest,
)

SERVICES = tuple(IMAGES.values())
CONTAINERS = {
    "ai-tuner": "ai-tuner",
    "btc-eth-aggressive-strategy": "trading_system-btc_eth_aggressive",
    "btc-eth-strategy": "trading_system-btc_eth",
    "dashboard-api": "dashboard-api",
    "data-backend": "data-backend",
    "execution-facts-collector": "trading_system-execution-facts",
    "hrs-strategy": "trading_system-hrs",
    "new-coin-strategy": "trading_system-new_coin",
}
STACK_FILES = {"docker-compose.yml", "docker-compose.override.yml", "docker-compose.logging.yml"}
IMAGE_RE = re.compile(r"ghcr\.io/bearflower/trading-(hrs|btc-eth-aggr)@sha256:[0-9a-f]{64}\Z")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def image_only(before: dict, after: dict) -> bool:
    return {k: v for k, v in before.items() if k != "image"} == {k: v for k, v in after.items() if k != "image"}


def unchanged_exchange(before: dict, after: dict) -> None:
    for key in ("position_exposure", "ordinary_orders_sha256", "algo_orders_sha256", "allocation_sha256"):
        if after.get(key) != before.get(key):
            raise ReleaseBlocked("exchange/allocation transition requires manual review: " + key)


def startup_marker_present(logs: str, marker: str) -> bool:
    return marker in logs or json.dumps(marker, ensure_ascii=True)[1:-1] in logs


def validate_active_state(pin: dict, state: dict) -> None:
    if pin.get("sha") != ACTIVE_PIN_SHA or pin.get("status") != "active":
        raise ReleaseBlocked("expected active predecessor pin changed")
    if state.get("sha") != ACTIVE_PIN_SHA or state.get("status") != "complete":
        raise ReleaseBlocked("active predecessor completion evidence drift")
    if state.get("exchange_after", {}).get("ok") is not True:
        raise ReleaseBlocked("predecessor exchange gate did not complete")


class Runtime:
    def __init__(self, root: Path, bundle: Path, sha: str):
        self.root, self.bundle, self.sha = root.resolve(), bundle.resolve(), sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ReleaseBlocked("invalid successor SHA")
        if (bundle / "release-sha").read_text().strip() != sha:
            raise ReleaseBlocked("bundle/SHA mismatch")
        self.manifest = validate_manifest(json.loads((bundle / MANIFEST).read_text()))
        for path in ("scripts/scoped_risk_followup.py", "scripts/risk_followup_manifest.py"):
            if sha256((bundle / path).read_bytes()) != self.manifest["source_checksums"][path]:
                raise ReleaseBlocked("bundle source mismatch: " + path)
        self.images = json.loads((bundle / "image-digests.json").read_text())
        if set(self.images) != set(IMAGES):
            raise ReleaseBlocked("exactly two image digests required")
        for name, ref in self.images.items():
            if not isinstance(ref, str) or not IMAGE_RE.fullmatch(ref) or not ref.startswith(f"ghcr.io/bearflower/trading-{name}@"):
                raise ReleaseBlocked("invalid immutable image digest")
        self.pin_file = self.root / ".deploy_tmp/ledger-risk-active.json"
        self.release_dir = self.root / ".releases" / RELEASE / sha
        self.overlay = self.release_dir / "images.json"
        self.state = self.release_dir / "state.json"
        if self.release_dir.exists():
            raise ReleaseBlocked("successor already attempted; manual review required")
        self.before: dict[str, dict] = {}
        self.all_before: dict[str, dict] = {}
        self.changed: list[str] = []

    def run(self, argv: list[str], *, timeout: int = 120, include_stderr: bool = False) -> str:
        env = dict(os.environ)
        env.pop("COMPOSE_FILE", None)
        env["COMPOSE_PROFILES"] = ""
        result = subprocess.run(argv, cwd=self.root, env=env, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise ReleaseBlocked("command failed: " + " ".join(argv[:2]) + " (output suppressed)")
        return (result.stdout + result.stderr if include_stderr else result.stdout).strip()

    def compose(self, service: str, *, overlay: Path) -> list[str]:
        stacks = self.pin["stacks"][service]
        args = ["docker", "compose", "--project-directory", str(self.root), "-p", "trading_system"]
        for stack in stacks:
            args.extend(["-f", str(self.root / stack)])
        args.extend(["-f", str(overlay)])
        return args

    def inspect(self, service: str) -> dict:
        name = CONTAINERS[service]
        rows = json.loads(self.run(["docker", "inspect", name]))
        if len(rows) != 1:
            raise ReleaseBlocked("ambiguous container: " + service)
        return rows[0]

    def project_inventory(self) -> dict[str, tuple]:
        names = self.run(["docker", "ps", "-a", "--no-trunc", "--filter",
                          "label=com.docker.compose.project=trading_system", "--format", "{{.Names}}"])
        result = {}
        for name in names.splitlines():
            rows = json.loads(self.run(["docker", "inspect", name]))
            if len(rows) != 1 or name in result:
                raise ReleaseBlocked("ambiguous project inventory")
            result[name] = self.identity(rows[0])
        return result

    @staticmethod
    def identity(row: dict) -> tuple:
        return row["Id"], row["Image"], row["State"]["StartedAt"]

    def record(self, status: str, step: str) -> None:
        atomic_json(self.state, dict(sha=self.sha, status=status, step=step, changed=self.changed,
                                     images=self.images, updated_at=time.time(), no_automatic_rollback=True))

    def gate(self) -> dict:
        output = self.run(["docker", "exec", "trading_system-hrs", "python", "/app/scripts/verify_ledger_risk_release.py",
                           "--stage", "strategy_gate", "--account", "default", "--revision", "1", "--require-reduced"], timeout=180)
        result = json.loads(output.splitlines()[-1])
        if result.get("ok") is not True or result.get("read_only") is not True:
            raise ReleaseBlocked("read-only exchange/policy gate failed")
        return result

    def preflight(self) -> None:
        self.pin = json.loads(self.pin_file.read_text())
        prior_state = Path(self.pin.get("state", ""))
        if not prior_state.is_file() or not prior_state.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("active predecessor state missing or outside release tree")
        validate_active_state(self.pin, json.loads(prior_state.read_text()))
        if set(self.pin.get("stacks", {})) != {"ai-tuner", "btc-eth-aggressive-strategy", "btc-eth-strategy",
                                              "dashboard-api", "data-backend", "execution-facts-collector", "hrs-strategy", "new-coin-strategy"}:
            raise ReleaseBlocked("pinned service inventory drift")
        for stacks in self.pin["stacks"].values():
            if not isinstance(stacks, list) or not stacks or stacks[0] != "docker-compose.yml" or not set(stacks) <= STACK_FILES or len(stacks) != len(set(stacks)):
                raise ReleaseBlocked("unsafe pinned compose stack")
        predecessor = Path(self.pin["overlay"])
        if not predecessor.is_file() or not predecessor.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("pinned overlay outside release tree")
        if sha256(predecessor.read_bytes()) != PIN_OVERLAY_SHA256:
            raise ReleaseBlocked("active overlay changed since review")
        original = json.loads(predecessor.read_text())
        if set(original) != {"services"} or set(original["services"]) != set(self.pin["stacks"]):
            raise ReleaseBlocked("incomplete predecessor image pins")
        for service, image in original["services"].items():
            if set(image) != {"image"} or not re.fullmatch(r"ghcr\.io/bearflower/trading-[a-z0-9-]+@sha256:[0-9a-f]{64}", image["image"]):
                raise ReleaseBlocked("invalid predecessor image pin: " + service)
        self.release_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(self.release_dir, 0o700)
        self.all_before = {service: self.inspect(service) for service in self.pin["stacks"]}
        self.inventory_before = self.project_inventory()
        if not set(CONTAINERS.values()) <= set(self.inventory_before):
            raise ReleaseBlocked("pinned containers absent from project inventory")
        for service, row in self.all_before.items():
            if row["State"]["Status"] != "running" or row["State"].get("Health", {}).get("Status") != "healthy":
                raise ReleaseBlocked("predecessor not healthy: " + service)
            expected_image = original["services"][service]["image"]
            image_id = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", expected_image])
            if row["Image"] != image_id:
                raise ReleaseBlocked("running image differs from active pin: " + service)
        self.before = {service: self.all_before[service] for service in SERVICES}
        successor = json.loads(json.dumps(original))
        for image, service in IMAGES.items():
            successor["services"][service]["image"] = self.images[image]
        atomic_json(self.overlay, successor)
        for service in self.pin["stacks"]:
            old = json.loads(self.run(self.compose(service, overlay=predecessor) + ["config", "--format", "json"]))["services"][service]
            new = json.loads(self.run(self.compose(service, overlay=self.overlay) + ["config", "--format", "json"]))["services"][service]
            if not image_only(old, new) or (service not in SERVICES and old != new):
                raise ReleaseBlocked("effective compose drift beyond two image references: " + service)
        atomic_json(self.release_dir / "preflight-containers.json",
                    {service: {"identity": self.identity(row), "health": row["State"].get("Health", {}).get("Status")}
                     for service, row in self.all_before.items()})
        self.pre = self.gate()
        atomic_json(self.release_dir / "exchange-before.json", self.pre)
        self.record("in_progress", "preflight")

    def pull_and_verify(self) -> None:
        for name, image in self.images.items():
            self.run(["docker", "pull", image], timeout=600)
            paths = ["shared/entry_risk_policy.py"] + (["strategies/btc_eth_aggressive/strategy.py"] if name == "btc-eth-aggr" else ["scripts/verify_ledger_risk_release.py"])
            code = "import hashlib,json,pathlib; p=pathlib.Path('/app'); print(json.dumps({'version':(p/'VERSION').read_text(),'hashes':{x:hashlib.sha256((p/x).read_bytes()).hexdigest() for x in " + repr(paths) + "}}))"
            proof = json.loads(self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image, "-c", code], timeout=180))
            if "GIT_SHA=" + self.sha not in proof["version"].splitlines():
                raise ReleaseBlocked("image VERSION does not match successor commit")
            for path in paths:
                if proof["hashes"].get(path) != self.manifest["source_checksums"][path]:
                    raise ReleaseBlocked("image source mismatch: " + path)
            atomic_json(self.release_dir / (name + "-image-proof.json"), proof)
        self.record("in_progress", "images_verified")

    def activate(self) -> None:
        atomic_json(self.pin_file, dict(self.pin, status="activating", successor_sha=self.sha))
        self.record("in_progress", "activation_started")
        for service in SERVICES:
            old = self.before[service]
            self.run(["docker", "tag", old["Image"], "strategy-successor-rollback/" + service + ":" + self.sha])
            self.run(self.compose(service, overlay=self.overlay) + ["up", "-d", "--no-deps", service], timeout=180)
            self.changed.append(service)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                current = self.inspect(service)
                if current["State"]["Status"] == "running" and current["State"].get("Health", {}).get("Status") == "healthy":
                    break
                time.sleep(3)
            else:
                raise ReleaseBlocked("successor health gate failed: " + service)
            target_image = self.images[next(name for name, target in IMAGES.items() if target == service)]
            expected_id = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", target_image])
            if current["Image"] != expected_id or self.identity(current) == self.identity(old):
                raise ReleaseBlocked("successor container image/identity mismatch: " + service)
            marker = "调度器已启动" if service == "btc-eth-aggressive-strategy" else "策略初始化完成，开始运行"
            startup_deadline = time.monotonic() + 300
            while True:
                logs = self.run(["docker", "logs", "--since", current["State"]["StartedAt"], current["Id"]], include_stderr=True)
                if "Traceback (most recent call last)" in logs or "策略执行失败" in logs:
                    raise ReleaseBlocked("strategy startup failed: " + service)
                if startup_marker_present(logs, marker):
                    break
                if time.monotonic() >= startup_deadline:
                    raise ReleaseBlocked("strategy startup evidence missing: " + service)
                time.sleep(3)
            startup_log = self.release_dir / (service + "-startup.log")
            startup_log.write_text(logs)
            os.chmod(startup_log, 0o600)
            self.record("in_progress", "activated_" + service)
        after = self.gate()
        atomic_json(self.release_dir / "exchange-after.json", after)
        unchanged_exchange(self.pre, after)
        for service, before in self.all_before.items():
            current = self.inspect(service)
            if service not in SERVICES and self.identity(current) != self.identity(before):
                raise ReleaseBlocked("excluded service changed: " + service)
        inventory_after = self.project_inventory()
        target_names = {CONTAINERS[service] for service in SERVICES}
        for name, before in self.inventory_before.items():
            if name not in target_names and inventory_after.get(name) != before:
                raise ReleaseBlocked("excluded project container changed: " + name)
        if set(inventory_after) != set(self.inventory_before):
            raise ReleaseBlocked("project container inventory changed")
        atomic_json(self.pin_file, dict(self.pin, sha=self.sha, status="active", overlay=str(self.overlay),
                                        state=str(self.state), predecessor_sha=ACTIVE_PIN_SHA))
        self.record("complete", "success")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/root/trading_system")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    runtime = None
    try:
        runtime = Runtime(Path(args.root), Path(args.bundle), args.sha)
        runtime.preflight()
        runtime.pull_and_verify()
        runtime.activate()
    except Exception as exc:
        if runtime is not None and runtime.release_dir.exists():
            runtime.record("failed", type(exc).__name__)
        print("Strategy successor blocked/failed: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
