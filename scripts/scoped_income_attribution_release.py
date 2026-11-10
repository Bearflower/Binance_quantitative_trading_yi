#!/usr/bin/env python3
"""Promote the verified ledger reader and hourly writer without trading restarts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.request import urlopen


ROOT = Path("/root/trading_system")
OLD_RELEASE = ROOT / ".releases/funding-auto-attribution-20261109/dde4b068"
OLD_TIMER = "income-attribution-20261110.timer"
NEW_TIMER = "income-attribution.timer"
NEW_SERVICE = "income-attribution.service"
STRATEGY_CONTAINERS = (
    "trading_system-btc_eth", "trading_system-btc_eth_aggressive",
    "trading_system-new_coin", "trading_system-hrs", "trading_system-grid",
)


def run(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def require_images(bundle: Path) -> dict[str, str]:
    images = json.loads((bundle / "image-digests.json").read_text())
    if set(images) != {"hrs", "dashboard-api"}:
        raise ValueError("exactly two immutable images required")
    for name, ref in images.items():
        if not isinstance(ref, str) or not re.fullmatch(
            rf"ghcr\.io/bearflower/trading-{name}@sha256:[0-9a-f]{{64}}", ref
        ):
            raise ValueError("invalid image digest: " + name)
    return images


def render_service(template: str, release: Path, image: str) -> str:
    service = template.replace("@RELEASE_DIR@", str(release)).replace("@ATTRIBUTION_IMAGE@", image)
    if re.search(r"@[A-Z_]+@", service):
        raise ValueError("unresolved systemd template token")
    return service


def container_ids() -> dict[str, str]:
    return {name: run("docker", "inspect", "--format", "{{.Id}}", name)
            for name in STRATEGY_CONTAINERS}


def dashboard_ready() -> bool:
    if run("docker", "inspect", "--format", "{{.State.Health.Status}}", "dashboard-api") != "healthy":
        return False
    with urlopen("http://127.0.0.1:8767/api/overview?type=monthly", timeout=15) as response:
        overview = json.load(response)
    return "exchange-income-v1-derived-v1" in json.dumps(overview)


def compose_up(overlay: Path) -> None:
    run("docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.logging.yml",
        "-f", str(overlay), "up", "-d", "--no-deps", "--pull", "never", "dashboard-api")


def wait_inactive(service: str) -> None:
    for _ in range(180):
        state = subprocess.run(["systemctl", "is-active", service], capture_output=True, text=True).stdout.strip()
        if state in {"", "inactive", "failed", "unknown"}:
            return
        time.sleep(3)
    raise RuntimeError(service + " remained active beyond the release gate")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    sha, bundle = args.sha, args.bundle.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or (bundle / "release-sha").read_text().strip() != sha:
        raise ValueError("bundle and commit SHA do not match")
    images = require_images(bundle)
    if not (ROOT / ".deploy_tmp/ledger-risk-active.json").is_file():
        raise ValueError("expected active strategy image pins missing")
    old_overlay = OLD_RELEASE / "dashboard-overlay.yml"
    if not old_overlay.is_file() or run("systemctl", "is-active", OLD_TIMER) != "active":
        raise ValueError("verified predecessor dashboard or timer missing")
    before = container_ids()
    release = ROOT / ".releases/income-attribution" / sha
    release.mkdir(parents=True, exist_ok=False)
    for source in ("scripts/run_income_attribution.sh", "deployment/systemd/income-attribution.service",
                   "deployment/systemd/income-attribution.timer"):
        destination = release / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / source, destination)
    overlay = release / "dashboard-overlay.yml"
    overlay.write_text(
        "services:\n  dashboard-api:\n    image: " + images["dashboard-api"] +
        "\n    environment:\n      - LEDGER_DERIVED_ATTRIBUTION_ENABLED=1\n"
    )
    template = (bundle / "deployment/systemd/income-attribution.service").read_text()
    service = render_service(template, release, images["hrs"])
    timer = (bundle / "deployment/systemd/income-attribution.timer").read_text()
    (release / "income-attribution.service").write_text(service)
    (release / "income-attribution.timer").write_text(timer)
    activated = False
    try:
        run("docker", "pull", images["dashboard-api"])
        run("docker", "pull", images["hrs"])
        run("systemctl", "disable", "--now", OLD_TIMER)
        wait_inactive("income-attribution-20261110.service")
        compose_up(overlay)
        for _ in range(60):
            try:
                if dashboard_ready():
                    break
            except Exception:
                pass
            time.sleep(3)
        else:
            raise RuntimeError("new dashboard did not publish verified derived ledger")
        shutil.copyfile(release / "income-attribution.service", Path("/etc/systemd/system") / NEW_SERVICE)
        shutil.copyfile(release / "income-attribution.timer", Path("/etc/systemd/system") / NEW_TIMER)
        run("systemctl", "daemon-reload")
        run("systemctl", "start", NEW_SERVICE)
        run("systemctl", "enable", "--now", NEW_TIMER)
        if run("systemctl", "is-active", NEW_TIMER) != "active" or container_ids() != before:
            raise RuntimeError("timer or trading-container isolation check failed")
        activated = True
        (release / "state.json").write_text(json.dumps({"sha": sha, "status": "active", "images": images}))
    finally:
        if not activated:
            subprocess.run(["systemctl", "disable", "--now", NEW_TIMER], check=False)
            try:
                wait_inactive(NEW_SERVICE)
                compose_up(old_overlay)
                run("systemctl", "enable", "--now", OLD_TIMER)
            except Exception as exc:
                print("ROLLBACK NEEDS MANUAL ATTENTION:", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
