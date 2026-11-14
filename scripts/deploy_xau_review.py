#!/usr/bin/env python3
"""Pinned image-only release for the XAU collectors and their daily observer."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

TARGETS = {
    "ai-tuner": "ai-tuner",
    "mtcps-xau-shadow": "trading_system-mtcps-xau-shadow",
    "mtcps-xau-calibrated-shadow": "trading_system-mtcps-xau-calibrated-shadow",
}


def run(*args):
    return subprocess.check_output(args, text=True, timeout=600).strip()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def inspect(name):
    return json.loads(run("docker", "inspect", name))[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--digests", required=True)
    args = parser.parse_args()
    if not re.fullmatch("[0-9a-f]{40}", args.sha):
        raise ValueError("invalid release SHA")
    root = Path("/root/trading_system")
    os.chdir(root)
    release = root / ".releases/xau-review" / args.sha
    if release.exists():
        raise RuntimeError("release already attempted, inspect recorded evidence before retry")
    release.mkdir(parents=True)
    digests = json.loads(Path(args.digests).read_text())
    for service in ("btc-eth", "ai-tuner"):
        if not re.fullmatch(f"ghcr.io/bearflower/trading-{service}@sha256:[0-9a-f]{{64}}", digests[service]):
            raise ValueError("invalid digest")
    pin_path = root / ".deploy_tmp/ledger-risk-active.json"
    pin = json.loads(pin_path.read_text())
    if pin["status"] != "active":
        raise RuntimeError("active image pin missing")
    inventory = {row["Name"].lstrip("/"): row for row in
                 json.loads(run("docker", "inspect", *run("docker", "ps", "-q").split()))}
    write_json(release / "before.json", {name: {"id": row["Id"], "image": row["Image"]}
                                        for name, row in inventory.items()})
    old_overlay = Path(pin["overlay"])
    pin_overlay = json.loads(old_overlay.read_text())
    pin_overlay["services"]["ai-tuner"]["image"] = digests["ai-tuner"]
    write_json(release / "images.json", pin_overlay)
    commands = {}
    originals = {}
    for service, name in TARGETS.items():
        before = inventory[name]
        if before["State"]["Status"] != "running" or before["State"].get("Health", {}).get("Status") != "healthy":
            raise RuntimeError("target unhealthy before deployment")
        files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
        base = ["docker", "compose"]
        for path in files:
            if not Path(path).resolve().is_relative_to(root):
                raise RuntimeError("compose file outside project")
            base += ["-f", path]
        image = digests["ai-tuner" if service == "ai-tuner" else "btc-eth"]
        overlay = release / f"{service}.json"
        write_json(overlay, {"services": {service: {"image": image}}})
        command = base + ["-f", str(overlay)]
        old_config = json.loads(run(*base, "config", "--format", "json"))["services"][service]
        new_config = json.loads(run(*command, "config", "--format", "json"))["services"][service]
        old_config.pop("image", None)
        new_config.pop("image", None)
        if old_config != new_config:
            raise RuntimeError("effective configuration changed beyond image")
        commands[service] = command
        rollback = release / f"rollback-{service}.json"
        write_json(rollback, {"services": {service: {"image": before["Image"]}}})
        originals[service] = base + ["-f", str(rollback)]
    for image in digests.values():
        run("docker", "pull", image)
        version = run("docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", image, "/app/VERSION")
        if f"GIT_SHA={args.sha}" not in version.splitlines():
            raise RuntimeError("image commit mismatch")
    capture_script = root / "scripts/capture_xau_runtime.py"
    shutil.copyfile(Path(__file__).with_name("capture_xau_runtime.py"), capture_script)
    units = {
        "xau-runtime-evidence.service": (
            "[Unit]\nDescription=XAU read-only container evidence\nAfter=docker.service\n"
            "[Service]\nType=oneshot\n"
            "ExecStart=/usr/bin/python3 /root/trading_system/scripts/capture_xau_runtime.py "
            "--output /root/trading_system/logs/ai_tuner/xau_runtime.json\n"
        ),
        "xau-runtime-evidence.timer": (
            "[Unit]\nDescription=Capture XAU evidence before 07:45 patrol\n"
            "[Timer]\nOnCalendar=*-*-* 07:40:00 Asia/Shanghai\nPersistent=true\n"
            "[Install]\nWantedBy=timers.target\n"
        ),
    }
    for name, content in units.items():
        Path("/etc/systemd/system", name).write_text(content)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "xau-runtime-evidence.timer")
    run("systemctl", "start", "xau-runtime-evidence.service")
    evidence_path = root / "logs/ai_tuner/xau_runtime.json"
    previous_path = evidence_path.with_name("xau_runtime_previous.json")
    if not previous_path.exists():
        shutil.copyfile(evidence_path, previous_path)
    changed = []
    try:
        for service, name in TARGETS.items():
            changed.append(service)
            run(*commands[service], "up", "-d", "--no-deps", service)
            for _ in range(60):
                row = inspect(name)
                if row["State"]["Status"] == "running" and row["State"].get("Health", {}).get("Status") == "healthy":
                    break
                time.sleep(3)
            else:
                raise RuntimeError("health timeout: " + name)
            wanted = digests["ai-tuner" if service == "ai-tuner" else "btc-eth"]
            if row["Image"] != run("docker", "image", "inspect", "--format", "{{.Id}}", wanted):
                raise RuntimeError("active image mismatch")
        for name, before in inventory.items():
            if name not in TARGETS.values() and inspect(name)["Id"] != before["Id"]:
                raise RuntimeError("excluded container identity changed: " + name)
        run("systemctl", "start", "xau-runtime-evidence.service")
    except Exception:
        for service in reversed(changed):
            run(*originals[service], "up", "-d", "--no-deps", service)
        raise
    state = release / "state.json"
    write_json(state, {"sha": args.sha, "status": "complete", "images": digests})
    write_json(pin_path, dict(pin, sha=args.sha, predecessor_sha=pin["sha"],
                             overlay=str(release / "images.json"), state=str(state)))
    print("DEPLOY_SUCCESS xau-review sha=" + args.sha)


if __name__ == "__main__":
    main()
