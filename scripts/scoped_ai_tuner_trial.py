#!/usr/bin/env python3
"""One-shot pinned AI Tuner observer deployment; the workflow owns compose lock."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ai_tuner_trial_manifest import (  # noqa: E402
    ACTIVE_PIN_SHA, IMAGES, MANIFEST, PIN_OVERLAY_SHA256, RELEASE,
    ReleaseBlocked, sha256, validate_manifest,
)
from scripts.scoped_stop_followup import (  # noqa: E402
    Runtime as BaseRuntime, CONTAINERS, STACK_FILES, atomic_json, image_only,
    startup_marker_present, unchanged_exchange,
)

SERVICE = "ai-tuner"
INCOME_RELEASE_SHA = "97fb429a4c7f288e7f9506e54233f37d15cd780f"
IMAGE_RE = re.compile(r"ghcr\.io/bearflower/trading-ai-tuner@sha256:[0-9a-f]{64}\Z")
IMAGE_SOURCES = (
    "ai_tuner/config.yaml", "ai_tuner/main.py",
    "ai_tuner/monitor/daily_health_check.py",
    "ai_tuner/monitor/reduced_trial.py",
    "ai_tuner/monitor/xau_shadow_patrol.py",
    "shared/accounting/report_reader.py", "shared/readonly_client.py",
)


def validated_dashboard_image(state: dict, overlay: str) -> str:
    if state.get("sha") != INCOME_RELEASE_SHA or state.get("status") != "active":
        raise ReleaseBlocked("independent income attribution release not active")
    images = state.get("images")
    if (not isinstance(images, dict) or set(images) != {"dashboard-api", "hrs"}
            or not re.fullmatch(r"ghcr\.io/bearflower/trading-dashboard-api@sha256:[0-9a-f]{64}",
                                images["dashboard-api"])):
        raise ReleaseBlocked("independent dashboard image proof invalid")
    if (images["dashboard-api"] not in overlay
            or "LEDGER_DERIVED_ATTRIBUTION_ENABLED=1" not in overlay):
        raise ReleaseBlocked("independent dashboard overlay proof invalid")
    return images["dashboard-api"]


class Runtime(BaseRuntime):
    def __init__(self, root: Path, bundle: Path, sha: str):
        self.root, self.bundle, self.sha = root.resolve(), bundle.resolve(), sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ReleaseBlocked("invalid successor SHA")
        if (bundle / "release-sha").read_text().strip() != sha:
            raise ReleaseBlocked("bundle/SHA mismatch")
        self.manifest = validate_manifest(json.loads((bundle / MANIFEST).read_text()))
        for path in ("scripts/scoped_ai_tuner_trial.py", "scripts/ai_tuner_trial_manifest.py",
                     "scripts/scoped_stop_followup.py", "scripts/stop_followup_manifest.py"):
            if sha256((bundle / path).read_bytes()) != self.manifest["source_checksums"][path]:
                raise ReleaseBlocked("bundle source mismatch: " + path)
        self.images = json.loads((bundle / "image-digests.json").read_text())
        if set(self.images) != set(IMAGES) or not IMAGE_RE.fullmatch(self.images[SERVICE]):
            raise ReleaseBlocked("exactly one immutable AI Tuner image digest required")
        self.pin_file = self.root / ".deploy_tmp/ledger-risk-active.json"
        self.release_dir = self.root / ".releases" / RELEASE / sha
        self.overlay = self.release_dir / "images.json"
        self.state = self.release_dir / "state.json"
        if self.release_dir.exists():
            raise ReleaseBlocked("successor already attempted; manual review required")
        self.changed: list[str] = []

    def preflight(self) -> None:
        self.pin = json.loads(self.pin_file.read_text())
        if self.pin.get("sha") != ACTIVE_PIN_SHA or self.pin.get("status") != "active":
            raise ReleaseBlocked("expected active predecessor pin changed")
        prior_state = Path(self.pin.get("state", ""))
        if not prior_state.is_file() or not prior_state.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("active predecessor state missing or outside release tree")
        state = json.loads(prior_state.read_text())
        if state.get("sha") != ACTIVE_PIN_SHA or state.get("status") != "complete":
            raise ReleaseBlocked("active predecessor completion evidence drift")
        prior_exchange = prior_state.parent / "exchange-after.json"
        if not prior_exchange.is_file() or not prior_exchange.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("active predecessor exchange proof missing")
        exchange_proof = json.loads(prior_exchange.read_text())
        if exchange_proof.get("ok") is not True or exchange_proof.get("read_only") is not True:
            raise ReleaseBlocked("predecessor exchange proof invalid")
        self.stacks = self.pin.get("stacks")
        if not isinstance(self.stacks, dict) or set(self.stacks) != set(CONTAINERS):
            raise ReleaseBlocked("pinned service inventory drift")
        for stacks in self.stacks.values():
            if (not isinstance(stacks, list) or not stacks or stacks[0] != "docker-compose.yml"
                    or not set(stacks) <= STACK_FILES or len(stacks) != len(set(stacks))):
                raise ReleaseBlocked("unsafe pinned compose stack")
        predecessor = Path(self.pin.get("overlay", ""))
        if not predecessor.is_file() or not predecessor.resolve().is_relative_to(self.root / ".releases"):
            raise ReleaseBlocked("pinned overlay outside release tree")
        if sha256(predecessor.read_bytes()) != PIN_OVERLAY_SHA256:
            raise ReleaseBlocked("active overlay changed since review")
        original = json.loads(predecessor.read_text())
        if set(original) != {"services"} or set(original["services"]) != set(self.stacks):
            raise ReleaseBlocked("incomplete predecessor image pins")
        for service, image in original["services"].items():
            if (set(image) != {"image"} or not re.fullmatch(
                    r"ghcr\.io/bearflower/trading-[a-z0-9-]+@sha256:[0-9a-f]{64}", image["image"])):
                raise ReleaseBlocked("invalid predecessor image pin: " + service)
        self.pre = self.gate()
        if self.run(["systemctl", "is-active", "income-attribution.timer"]) != "active":
            raise ReleaseBlocked("independent income attribution timer not active")
        self.release_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(self.release_dir, 0o700)
        self.all_before = {service: self.inspect(service) for service in self.stacks}
        self.inventory_before = self.project_inventory()
        if not set(CONTAINERS.values()) <= set(self.inventory_before):
            raise ReleaseBlocked("pinned containers absent from project inventory")
        for service, row in self.all_before.items():
            if row["State"]["Status"] != "running" or row["State"].get("Health", {}).get("Status") != "healthy":
                raise ReleaseBlocked("predecessor not healthy: " + service)
            expected = original["services"][service]["image"]
            if row["Image"] != self.run(["docker", "image", "inspect", "--format", "{{.Id}}", expected]):
                raise ReleaseBlocked("running image differs from active pin: " + service)
        successor = json.loads(json.dumps(original))
        successor["services"][SERVICE]["image"] = self.images[SERVICE]
        atomic_json(self.overlay, successor)
        for service in self.stacks:
            old = json.loads(self.run(self.compose(service, overlay=predecessor) + ["config", "--format", "json"]))["services"][service]
            new = json.loads(self.run(self.compose(service, overlay=self.overlay) + ["config", "--format", "json"]))["services"][service]
            if not image_only(old, new) or (service != SERVICE and old != new):
                raise ReleaseBlocked("effective compose drift beyond AI Tuner image: " + service)
        atomic_json(self.release_dir / "preflight-containers.json",
                    {service: {"identity": self.identity(row), "health": row["State"].get("Health", {}).get("Status")}
                     for service, row in self.all_before.items()})
        atomic_json(self.release_dir / "exchange-before.json", self.pre)
        self.policy_before = self.policy_snapshot()
        self.record("in_progress", "preflight")

    def pull_and_verify(self) -> None:
        image = self.images[SERVICE]
        self.run(["docker", "pull", image], timeout=600)
        code = (
            "import hashlib,json,pathlib,yaml; p=pathlib.Path('/app'); "
            "c=yaml.safe_load((p/'ai_tuner/config.yaml').read_text()); "
            "print(json.dumps({'version':(p/'VERSION').read_text(),"
            "'mode':c['purpose_driven_mvp']['mode'],"
            "'selected':c['purpose_driven_mvp']['selected_strategy'],"
            "'trial_enabled':c['reduced_trial']['enabled'],"
            "'hashes':{x:hashlib.sha256((p/x).read_bytes()).hexdigest() for x in "
            + repr(IMAGE_SOURCES) + "}}))"
        )
        proof = json.loads(self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint",
                                     "python", image, "-c", code], timeout=180))
        if "GIT_SHA=" + self.sha not in proof["version"].splitlines():
            raise ReleaseBlocked("image VERSION does not match release commit")
        if proof["mode"] != "audit" or proof["selected"] is not None or proof["trial_enabled"] is not True:
            raise ReleaseBlocked("AI Tuner mode/observer configuration drift")
        for path in IMAGE_SOURCES:
            if proof["hashes"].get(path) != self.manifest["source_checksums"][path]:
                raise ReleaseBlocked("image source mismatch: " + path)
        atomic_json(self.release_dir / "ai-tuner-image-proof.json", proof)
        self.record("in_progress", "image_verified")

    def activate(self) -> None:
        current = self.gate()
        unchanged_exchange(self.pre, current)
        if self.policy_snapshot() != self.policy_before:
            raise ReleaseBlocked("risk policy changed before activation")
        atomic_json(self.pin_file, dict(self.pin, status="activating", successor_sha=self.sha))
        self.record("in_progress", "activation_started")
        old = self.all_before[SERVICE]
        self.run(["docker", "tag", old["Image"], "ai-tuner-trial-rollback:" + self.sha])
        self.run(self.compose(SERVICE, overlay=self.overlay) + ["up", "-d", "--no-deps", SERVICE], timeout=180)
        self.changed.append(SERVICE)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = self.inspect(SERVICE)
            if current["State"]["Status"] == "running" and current["State"].get("Health", {}).get("Status") == "healthy":
                break
            time.sleep(3)
        else:
            raise ReleaseBlocked("AI Tuner health gate failed")
        image_id = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", self.images[SERVICE]])
        if current["Image"] != image_id or self.identity(current) == self.identity(old):
            raise ReleaseBlocked("AI Tuner container image/identity mismatch")
        startup_deadline = time.monotonic() + 300
        while True:
            logs = self.run(["docker", "logs", "--since", current["State"]["StartedAt"], current["Id"]], include_stderr=True)
            if "Traceback (most recent call last)" in logs:
                raise ReleaseBlocked("AI Tuner startup traceback")
            if (startup_marker_present(logs, "XAU 影子四小时巡检任务已注册")
                    and startup_marker_present(logs, "调度器已启动")):
                break
            if time.monotonic() >= startup_deadline:
                raise ReleaseBlocked("AI Tuner scheduler startup evidence missing")
            time.sleep(3)
        (self.release_dir / "ai-tuner-startup.log").write_text(logs)
        os.chmod(self.release_dir / "ai-tuner-startup.log", 0o600)
        self.record("in_progress", "activated")
        after = self.gate()
        atomic_json(self.release_dir / "exchange-after.json", after)
        unchanged_exchange(self.pre, after)
        if self.policy_snapshot() != self.policy_before:
            raise ReleaseBlocked("risk policy changed during AI Tuner release")
        for service, before in self.all_before.items():
            current = self.inspect(service)
            if service != SERVICE and self.identity(current) != self.identity(before):
                raise ReleaseBlocked("excluded service changed: " + service)
        inventory_after = self.project_inventory()
        for name, before in self.inventory_before.items():
            if name != CONTAINERS[SERVICE] and inventory_after.get(name) != before:
                raise ReleaseBlocked("excluded project container changed: " + name)
        if set(inventory_after) != set(self.inventory_before):
            raise ReleaseBlocked("project container inventory changed")
        atomic_json(self.pin_file, dict(self.pin, sha=self.sha, status="active", overlay=str(self.overlay),
                                        state=str(self.state), predecessor_sha=ACTIVE_PIN_SHA,
                                        stacks=self.stacks))
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
        print("AI Tuner trial release blocked/failed: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
