"""Install the separate HRS research observer using the existing read-only reporter identity."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit


SERVICE = "trading_system-hrs-research-observer"
REPORTER = "trading_system-hrs-acceptance"
MOUNT_FILES = ("research_liquidity.py", "research_export.py", "mode_report.py", "acceptance_report.py")
STATE = Path("/root/trading_system/research/hrs-mode-observer")
RECEIPTS = "/observations/receipts.sqlite"


def docker(*args, env=None, check=True):
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=True,
                          env=env, timeout=120)


def inspect(name):
    return json.loads(docker("inspect", name).stdout)[0]


def reader_environment(reporter):
    keys = dict(item.split("=", 1) for item in reporter["Config"]["Env"])
    dsn = keys["DATABASE_URL"]
    if urlsplit(dsn).username != "hrs_acceptance_reader":
        raise ValueError("expected existing HRS acceptance reader")
    return dict(os.environ, DATABASE_URL=dsn)


def verify(service, revision, files):
    instance = inspect(service)
    if not instance["State"]["Running"] or instance["Config"]["Labels"].get("hrs.observer.revision") != revision:
        raise ValueError("running observer revision mismatch")
    for path in files:
        actual = docker("exec", service, "sha256sum", "/app/strategies/hrs/" + path.name).stdout.split()[0]
        if actual != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("observer module digest mismatch")
    health = ("import sqlite3,datetime,sys; "
              "db=sqlite3.connect('file:/observations/receipts.sqlite?mode=ro',uri=True); "
              "seen=db.execute(\"select value from scan_state where key='scanned_at'\").fetchone(); "
              "now=datetime.datetime.now(datetime.timezone.utc); "
              "sys.exit(0 if seen and 0 <= (now-datetime.datetime.fromisoformat(seen[0])).total_seconds() < 45 else 1)")
    for attempt in range(15):
        if docker("exec", service, "python", "-c", health, check=False).returncode == 0:
            print(json.dumps(dict(observer=service, revision=revision, healthy=True, image=instance["Image"])))
            return
        time.sleep(2)
    raise RuntimeError("observer has no fresh database scan")


def activate(folder, revision, report_start=None, report_end=None):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid revision")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / "activation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not report_start:
            subprocess.run(["python3", str(folder / "scripts/database_permissions.py"), "audit",
                        "--manifest", str(folder / "database/postgres/permissions.json")], check=True, timeout=200)
        reporter = inspect(REPORTER)
        env = reader_environment(reporter)
        network, = reporter["NetworkSettings"]["Networks"]
        files = [folder / "strategies/hrs" / name for name in MOUNT_FILES]
        mounts = []
        for path in files:
            if not path.is_file():
                raise ValueError("missing release module")
            mounts.extend(["--mount", f"type=bind,src={path},dst=/app/strategies/hrs/{path.name},readonly"])
        common = ["--network", network, "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                  "--user", "65532:65532", "--env", "DATABASE_URL", "--env", "PYTHONDONTWRITEBYTECODE=1",
                  "--workdir", "/app", "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777",
                  "--entrypoint", "python", *mounts]
        command = ["-m", "strategies.hrs.research_liquidity", "--dsn-env", "DATABASE_URL", "--sqlite"]
        image = reporter["Image"]
        if report_start:
            instance = dict(item.split("=", 1) for item in inspect("trading_system-hrs")["Config"]["Env"])
            account = instance.get("TRADE_ACCOUNT_ID", "default")
            # Only the report CLI sees the instance's DB identity, inside a disposable
            # container. Its source reads share one read-only repeatable-read transaction.
            report_env = dict(os.environ, DATABASE_URL=instance["DATABASE_URL"], TRADE_ACCOUNT_ID=account)
            args = ["-m", "strategies.hrs.mode_report", "--start", report_start]
            if report_end:
                args.extend(["--end", report_end])
            result = docker("run", "--rm", *common, "--env", "TRADE_ACCOUNT_ID", image, *args, env=report_env)
            print(result.stdout, end="")
            return
        found = docker("ps", "-a", "--filter", f"name=^/{SERVICE}$", "--format", "{{.Names}}").stdout.strip()
        if found:
            # Idempotent retry. Preserve another live revision and its collection window.
            verify(SERVICE, revision, files)
            return
        directory = STATE / "observations"
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chown(directory, 65532, 65532)
        docker("run", "-d", "--name", SERVICE, "--label", "hrs.observer.revision=" + revision,
               "--restart", "unless-stopped", "--log-opt", "max-size=5m", "--log-opt", "max-file=2",
               *common, "--mount", f"type=bind,src={directory},dst=/observations", image,
               *command, RECEIPTS, env=env)
        try:
            verify(SERVICE, revision, files)
        except Exception:
            docker("rm", "-f", SERVICE)
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("revision")
    parser.add_argument("--report-start", help="run only the read-only mode report, inclusive ISO timestamp")
    parser.add_argument("--report-end", help="exclusive ISO timestamp")
    arguments = parser.parse_args()
    activate(arguments.release_dir.resolve(), arguments.revision, arguments.report_start, arguments.report_end)
