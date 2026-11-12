import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

ROOT = Path("/root/trading_system")
PIN_SHA = "5d121335d2a57e61959065f274b3c1063f8d390f"
OLD = "grid-notifier:291d10b4"
NEW = "grid-notifier:996bad18"
SHA = "996bad186d83c0dce7e7c06c24d9c381b5967281"
NAME = "trading_system-grid-notifier"
RELEASE = ROOT / ".releases/grid-notifier-ordering-20261112" / SHA
PROTECTED = ("trading_system-grid", "trading_system-grid-executor", "dashboard-api")


def call(args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def inspect(name):
    return json.loads(call(["docker", "inspect", name]))[0]


def write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main():
    with (ROOT / ".deploy_tmp/compose_deploy.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pin = json.loads((ROOT / ".deploy_tmp/ledger-risk-active.json").read_text())
        if (pin.get("status") != "active" or pin.get("sha") != PIN_SHA or
                json.loads(Path(pin["state"]).read_text()).get("status") != "complete"):
            raise RuntimeError("production pin changed")
        if RELEASE.exists():
            raise RuntimeError("successor already attempted")
        old = inspect(NAME)
        if old["State"].get("Health", {}).get("Status") != "healthy" or old["Config"]["Image"] != OLD:
            raise RuntimeError("notifier predecessor invalid")
        before = {name: inspect(name)["Id"] for name in PROTECTED}
        db = sqlite3.connect(ROOT / "data/grid/grid_notifications.sqlite3")
        try:
            count = db.execute("select count(*) from fact").fetchone()[0]
        finally:
            db.close()
        image_id = call(["docker", "image", "inspect", "--format", "{{.Id}}", NEW])
        proof = json.loads(call(["docker", "run", "--rm", "--network", "none",
                                 "--entrypoint", "python", NEW, "-c",
                                 "import hashlib,json,pathlib; p=pathlib.Path('/app');"
                                 "print(json.dumps({'version':(p/'VERSION').read_text().strip(),"
                                 "'worker':hashlib.sha256((p/'strategies/grid/execution/notification_worker.py').read_bytes()).hexdigest()}))"]))
        if proof != {"version": "GIT_SHA=" + SHA,
                     "worker": "dcf51daac2d0a0ad06df524fc9d0ca28038d558f0b2c1d5d9730b37c0af5cec3"}:
            raise RuntimeError("successor image mismatch")
        networks = list(old["NetworkSettings"]["Networks"])
        if len(networks) != 1:
            raise RuntimeError("notifier network ambiguous")
        RELEASE.mkdir(parents=True, mode=0o700)
        write(RELEASE / "preflight.json", {"sha": SHA, "pin_sha": PIN_SHA,
                                            "old_id": old["Id"],
                                            "new_image_id": image_id,
                                            "existing_facts": count,
                                            "protected_ids": before})
        call(["docker", "stop", NAME])
        call(["docker", "rm", NAME])
        call(["docker", "run", "-d", "--name", NAME,
              "--restart", "unless-stopped", "--network", networks[0],
              "--env-file", str(ROOT / ".secrets/grid-subaccount.env"),
              "--env-file", str(ROOT / ".secrets/grid-limits.env"),
              "--env-file", str(ROOT / ".secrets/grid-notifications.env"),
              "-e", "GRID_EXECUTION_MODE=live",
              "-e", "GRID_EXECUTION_DB=/app/data/grid_execution.sqlite3",
              "-e", "GRID_NOTIFICATION_DB=/app/data/grid_notifications.sqlite3",
              "-e", "GRID_NOTIFICATION_HEARTBEAT=/app/data/grid_notification_heartbeat.json",
              "-v", str(ROOT / "data/grid") + ":/app/data",
              "--health-cmd", "python -m strategies.grid.execution.notification_worker --healthcheck",
              "--health-interval", "30s", "--health-timeout", "10s",
              "--health-retries", "3", "--health-start-period", "60s",
              "--log-driver", "journald", NEW,
              "python", "-m", "strategies.grid.execution.notification_worker"])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            row = inspect(NAME)
            if row["State"]["Status"] == "running" and row["State"].get("Health", {}).get("Status") == "healthy":
                break
            if row["State"]["Status"] != "running":
                raise RuntimeError("notifier successor failed")
            time.sleep(3)
        else:
            raise RuntimeError("notifier successor health timeout")
        after = {name: inspect(name)["Id"] for name in PROTECTED}
        if before != after:
            raise RuntimeError("protected container changed")
        heartbeat = json.loads((ROOT / "data/grid/grid_notification_heartbeat.json").read_text())
        if heartbeat.get("status") != "ok" or time.time() * 1000 - heartbeat["updated_at_ms"] > 120_000:
            raise RuntimeError("successor heartbeat invalid")
        result = {"status": "active", "sha": SHA, "image_id": image_id,
                  "container_id": row["Id"], "protected_unchanged": True,
                  "prior_facts": count, "heartbeat": "ok"}
        write(RELEASE / "result.json", result)
        print(json.dumps({k: v for k, v in result.items() if k != "container_id"}, sort_keys=True))


main()
