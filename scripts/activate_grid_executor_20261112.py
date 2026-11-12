#!/usr/bin/env python3
"""Activate the reviewed grid executor image under the existing production pin."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time


ROOT = Path("/root/trading_system")
SHA = "814af50855623dd93d8abdd29cef0f566d1011c8"
PRIOR = "4faafd876832e6b87dd7265a37a6fe203eb3d019"
IMAGE = "grid-executor:814af508"
PIN = ROOT / ".deploy_tmp/ledger-risk-active.json"
RELEASE = ROOT / ".releases/grid-executor-activation-20261112" / SHA
CONTAINER = "trading_system-grid-executor"


def run(args, *, env=None):
    return subprocess.check_output(args, cwd=ROOT, env=env, text=True).strip()


def env_values(path):
    if path.stat().st_mode & 0o077:
        raise RuntimeError("secret file permissions too open")
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def atomic_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, indent=2))
    os.replace(tmp, path)


def compose_args(pin, executor):
    return (["docker", "compose", "-f", "docker-compose.yml",
             "-f", "docker-compose.logging.yml", "-f", pin["overlay"],
             "-f", str(executor), "--profile", "grid-executor"])


def identity(name):
    return json.loads(run(["docker", "inspect", name]))[0]["Id"]


def main():
    pin = json.loads(PIN.read_text())
    if pin.get("status") != "active" or pin.get("sha") != PRIOR:
        raise RuntimeError("production pin changed")
    if json.loads(Path(pin["state"]).read_text()).get("status") != "complete":
        raise RuntimeError("predecessor proof incomplete")
    if subprocess.run(["docker", "inspect", CONTAINER], capture_output=True).returncode == 0:
        raise RuntimeError("executor already exists")
    if RELEASE.exists() and {p.name for p in RELEASE.iterdir()} != {"executor.json"}:
        raise RuntimeError("activation already attempted")
    limits = env_values(ROOT / ".secrets/grid-limits.env")
    keys = env_values(ROOT / ".secrets/grid-subaccount.env")
    expected = {"GRID_EXECUTION_MODE": "off", "GRID_MAX_MARGIN_USDT": "240",
                "GRID_MAX_NOTIONAL_USDT": "2400", "GRID_MAX_DAILY_LOSS_USDT": "15",
                "GRID_MAX_DRAWDOWN_USDT": "30", "GRID_MAX_OPEN_ORDERS": "7",
                "GRID_EXPECTED_LEVERAGE": "5"}
    if any(limits.get(k) != v for k, v in expected.items()):
        raise RuntimeError("risk limits changed")
    if not keys.get("GRID_SUBACCOUNT_API_KEY") or not keys.get("GRID_SUBACCOUNT_API_SECRET"):
        raise RuntimeError("dedicated API credentials missing")
    cfg_env = {**os.environ, **limits, **keys, "GRID_EXECUTION_MODE": "live"}
    image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE])
    proof = json.loads(run(["docker", "run", "--rm", "--network", "none",
                            "--entrypoint", "python", IMAGE, "-c",
                            "import hashlib,json,pathlib; p=pathlib.Path('/app'); "
                            "print(json.dumps({'version':(p/'VERSION').read_text().strip(),"
                            "'main':hashlib.sha256((p/'strategies/grid/execution/main.py').read_bytes()).hexdigest(),"
                            "'planner':hashlib.sha256((p/'strategies/grid/execution/planner.py').read_bytes()).hexdigest()}))"] ))
    if proof != {"version": "GIT_SHA=" + SHA,
                 "main": "f5728d39e4d877efd3a11454286c17edff87900c2479b07b8c13c24fcb203b5a",
                 "planner": "fdd887453f2cf89dfcb9f970a611bc967584a166024f9b3bc571f95a978bc47b"}:
        raise RuntimeError("image source proof mismatch")
    original = json.loads((Path(pin["state"]).parent / "executor.json").read_text())
    if set(original.get("services", {})) != {"grid-executor"}:
        raise RuntimeError("predecessor executor overlay invalid")
    successor = json.loads(json.dumps(original))
    successor["services"]["grid-executor"]["image"] = IMAGE
    successor["services"]["grid-executor"]["environment"]["GRID_EXECUTION_MODE"] = "live"
    RELEASE.mkdir(parents=True, mode=0o700, exist_ok=True)
    overlay = RELEASE / "executor.json"
    atomic_json(overlay, successor)
    old_config = json.loads(run(compose_args(pin, Path(pin["state"]).parent / "executor.json") +
                                ["config", "--format", "json"], env=cfg_env))["services"]
    new_config = json.loads(run(compose_args(pin, overlay) +
                                ["config", "--format", "json"], env=cfg_env))["services"]
    if set(old_config) != set(new_config) or any(
            old_config[name] != new_config[name] for name in old_config
            if name != "grid-executor"):
        raise RuntimeError("existing service configuration drift")
    exe = new_config["grid-executor"]
    if (exe["image"] != IMAGE or exe["environment"].get("GRID_EXECUTION_MODE") != "live"
            or exe["environment"].get("GRID_SUBACCOUNT_API_KEY") != keys["GRID_SUBACCOUNT_API_KEY"]
            or exe["environment"].get("GRID_SUBACCOUNT_API_SECRET") != keys["GRID_SUBACCOUNT_API_SECRET"]
            or any(exe["environment"].get(k) != v for k, v in expected.items()
                   if k != "GRID_EXECUTION_MODE")):
        raise RuntimeError("executor effective configuration invalid")
    before = {name: identity(name) for name in (
        "ai-tuner", "trading_system-btc_eth_aggressive",
        "trading_system-btc_eth", "dashboard-api", "data-backend",
        "trading_system-execution-facts", "trading_system-grid",
        "trading_system-hrs", "trading_system-new_coin")}
    db = sqlite3.connect(ROOT / "data/grid/grid_execution.sqlite3")
    try:
        state = db.execute("select state from engine_state where id=1").fetchone()
        epochs = db.execute("select count(*) from grid_epoch where status='ACTIVE'").fetchone()[0]
    finally:
        db.close()
    if state != ("FLAT",) or epochs:
        raise RuntimeError("execution ledger not flat")
    atomic_json(RELEASE / "preflight.json", {
        "sha": SHA, "pin_sha": PRIOR, "image_id": image_id,
        "source_proof": proof, "existing_container_ids": before,
        "mode": "live", "limits": {k: v for k, v in expected.items() if k != "GRID_EXECUTION_MODE"}})
    run(compose_args(pin, overlay) + ["up", "-d", "--no-deps", "grid-executor"], env=cfg_env)
    deadline = time.monotonic() + 90
    health = None
    while time.monotonic() < deadline:
        row = json.loads(run(["docker", "inspect", CONTAINER]))[0]
        health = row["State"].get("Health", {}).get("Status")
        if row["State"]["Status"] == "running" and health == "healthy":
            break
        if row["State"]["Status"] != "running" or health == "unhealthy":
            break
        time.sleep(3)
    heartbeat = ROOT / "data/grid/grid_execution_heartbeat.json"
    payload = json.loads(heartbeat.read_text()) if heartbeat.exists() else {}
    after = {name: identity(name) for name in before}
    result = {"executor_health": health, "executor_status": row["State"]["Status"],
              "container_id": row["Id"], "existing_unchanged": before == after,
              "heartbeat_state": payload.get("state"),
              "heartbeat_risk": payload.get("risk_status"),
              "heartbeat_stream": payload.get("stream_ready"),
              "heartbeat_age_ms": int(time.time() * 1000) - payload.get("updated_at_ms", 0)}
    atomic_json(RELEASE / "activation.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "container_id"}, sort_keys=True))
    if (health != "healthy" or result["executor_status"] != "running"
            or not result["existing_unchanged"] or result["heartbeat_state"] == "LOCKED"
            or result["heartbeat_risk"] != "ALLOW" or not result["heartbeat_stream"]
            or result["heartbeat_age_ms"] > 120000):
        raise RuntimeError("executor activation needs review")
    atomic_json(ROOT / ".deploy_tmp/grid-executor-active.json", {
        "sha": SHA, "image_id": image_id, "release": str(RELEASE),
        "container_id": row["Id"], "status": "active"})


if __name__ == "__main__":
    main()
