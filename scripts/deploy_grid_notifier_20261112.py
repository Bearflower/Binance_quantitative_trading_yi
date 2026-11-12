#!/usr/bin/env python3
"""One-shot isolated notifier activation under the current production pin."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

ROOT = Path("/root/trading_system")
SHA = "291d10b4d3df8a9fc4b0fa73fe03760247800ad7"
PIN_SHA = "5d121335d2a57e61959065f274b3c1063f8d390f"
IMAGE = "grid-notifier:291d10b4"
NAME = "trading_system-grid-notifier"
RELEASE = ROOT / ".releases/grid-notifier-20261112" / SHA
PROTECTED = (
    "ai-tuner", "trading_system-btc_eth_aggressive", "trading_system-btc_eth",
    "dashboard-api", "data-backend", "trading_system-execution-facts",
    "trading_system-grid", "trading_system-hrs", "trading_system-new_coin",
    "trading_system-grid-executor",
)


def call(args):
    return subprocess.check_output(args, text=True, cwd=ROOT).strip()


def inspect(name):
    return json.loads(call(["docker", "inspect", name]))[0]


def write_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, sort_keys=True, indent=2))
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def main():
    lock = ROOT / ".deploy_tmp/compose_deploy.lock"
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deploy()


def deploy():
    pin = json.loads((ROOT / ".deploy_tmp/ledger-risk-active.json").read_text())
    if (pin.get("status") != "active" or pin.get("sha") != PIN_SHA or
            json.loads(Path(pin["state"]).read_text()).get("status") != "complete"):
        raise RuntimeError("production pin changed or incomplete")
    if RELEASE.exists() or subprocess.run(
            ["docker", "inspect", NAME], capture_output=True).returncode == 0:
        raise RuntimeError("notifier release already attempted")
    secret = ROOT / ".secrets/grid-notifications.env"
    if secret.stat().st_mode & 0o077:
        raise RuntimeError("notification secret permissions too open")
    before = {name: inspect(name) for name in PROTECTED}
    if any(row["State"]["Status"] != "running" or
           row["State"].get("Health", {}).get("Status") != "healthy"
           for row in before.values()):
        raise RuntimeError("protected container unhealthy")
    networks = list(before["trading_system-grid-executor"]["NetworkSettings"]["Networks"])
    if len(networks) != 1:
        raise RuntimeError("executor network identity ambiguous")
    old_webhook = next(x.split("=", 1)[1] for x in
                       before["trading_system-grid"]["Config"]["Env"]
                       if x.startswith("FEISHU_WEBHOOK_GRID="))
    if not old_webhook or secret.read_text() != "FEISHU_WEBHOOK_GRID=" + old_webhook + "\n":
        raise RuntimeError("notifier webhook differs from grid")
    db = sqlite3.connect(ROOT / "data/grid/grid_execution.sqlite3")
    try:
        engine = db.execute("select state from engine_state where id=1").fetchone()
        fills = db.execute("select count(*) from fill").fetchone()[0]
    finally:
        db.close()
    if engine != ("FLAT",):
        raise RuntimeError("executor ledger not flat at notifier activation")
    image_id = call(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE])
    proof = json.loads(call(["docker", "run", "--rm", "--network", "none",
                             "--entrypoint", "python", IMAGE, "-c",
                             "import hashlib,json,pathlib; p=pathlib.Path('/app');"
                             "print(json.dumps({'version':(p/'VERSION').read_text().strip(),"
                             "'report':hashlib.sha256((p/'strategies/grid/execution/notification_report.py').read_bytes()).hexdigest(),"
                             "'worker':hashlib.sha256((p/'strategies/grid/execution/notification_worker.py').read_bytes()).hexdigest()}))"]))
    if proof != {
        "version": "GIT_SHA=" + SHA,
        "report": "a1968da4e3594f4eda9a886e8cfca63fb3ce6bfa50e12ea235054f332d07593f",
        "worker": "eb8b5b12189c6933fe0920ca60e5ae71a60645c276a5fae297f9257adbbaaebd",
    }:
        raise RuntimeError("notifier image proof mismatch")
    RELEASE.mkdir(parents=True, mode=0o700)
    write_json(RELEASE / "preflight.json", {
        "sha": SHA, "pin_sha": PIN_SHA, "image_id": image_id,
        "proof": proof, "existing_fills": fills,
        "protected_ids": {name: row["Id"] for name, row in before.items()},
    })
    call(["docker", "run", "-d", "--name", NAME,
          "--restart", "unless-stopped", "--network", networks[0],
          "--env-file", str(ROOT / ".secrets/grid-subaccount.env"),
          "--env-file", str(ROOT / ".secrets/grid-limits.env"),
          "--env-file", str(secret), "-e", "GRID_EXECUTION_MODE=live",
          "-e", "GRID_EXECUTION_DB=/app/data/grid_execution.sqlite3",
          "-e", "GRID_NOTIFICATION_DB=/app/data/grid_notifications.sqlite3",
          "-e", "GRID_NOTIFICATION_HEARTBEAT=/app/data/grid_notification_heartbeat.json",
          "-v", str(ROOT / "data/grid") + ":/app/data",
          "--health-cmd", "python -m strategies.grid.execution.notification_worker --healthcheck",
          "--health-interval", "30s", "--health-timeout", "10s",
          "--health-retries", "3", "--health-start-period", "60s",
          "--log-driver", "journald", IMAGE,
          "python", "-m", "strategies.grid.execution.notification_worker"])
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        row = inspect(NAME)
        health = row["State"].get("Health", {}).get("Status")
        if row["State"]["Status"] == "running" and health == "healthy":
            break
        if row["State"]["Status"] != "running" or health == "unhealthy":
            raise RuntimeError("notifier failed health check")
        time.sleep(3)
    else:
        raise RuntimeError("notifier health timeout")
    after = {name: inspect(name) for name in PROTECTED}
    if any(after[name]["Id"] != before[name]["Id"] for name in PROTECTED):
        raise RuntimeError("protected container identity changed")
    env = row["Config"]["Env"]
    if not any(x == "FEISHU_WEBHOOK_GRID=" + old_webhook for x in env):
        raise RuntimeError("notifier webhook not injected")
    heartbeat = json.loads((ROOT / "data/grid/grid_notification_heartbeat.json").read_text())
    if heartbeat.get("status") != "ok" or int(time.time() * 1000) - heartbeat["updated_at_ms"] > 120_000:
        raise RuntimeError("notifier heartbeat stale")
    notice_db = sqlite3.connect(ROOT / "data/grid/grid_notifications.sqlite3")
    try:
        facts = notice_db.execute("select count(*) from fact").fetchone()[0]
    finally:
        notice_db.close()
    result = {"status": "active", "sha": SHA, "image_id": image_id,
              "container_id": row["Id"], "protected_unchanged": True,
              "heartbeat": "ok", "existing_fills": fills,
              "recorded_notification_facts": facts}
    write_json(RELEASE / "result.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "container_id"}, sort_keys=True))


if __name__ == "__main__":
    main()
