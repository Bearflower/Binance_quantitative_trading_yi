#!/usr/bin/env python3
"""Run on the Docker host before the daily patrol. Writes only evidence JSON."""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def capture(output):
    names = ["trading_system-mtcps-xau-shadow", "trading_system-mtcps-xau-calibrated-shadow"]
    raw = subprocess.run(["docker", "inspect", *names], check=True, capture_output=True, text=True)
    containers = {}
    for item in json.loads(raw.stdout):
        state = item["State"]
        environment = dict(value.split("=", 1) for value in item["Config"].get("Env", []) if "=" in value)
        version_result = subprocess.run(
            ["docker", "exec", item["Name"].lstrip("/"), "cat", "/app/VERSION"],
            check=False, capture_output=True, text=True, timeout=15,
        )
        version = dict(line.split("=", 1) for line in version_result.stdout.splitlines() if "=" in line)
        containers[item["Name"].lstrip("/")] = dict(
            id=item["Id"], started_at=state["StartedAt"], image=item["Image"],
            git_sha=version.get("GIT_SHA") or environment.get("GIT_SHA") or (item["Config"].get("Labels") or {}).get("org.opencontainers.image.revision"),
            status=state["Status"], health=state.get("Health", {}).get("Status"),
            restart_count=item["RestartCount"],
        )
    journal = subprocess.run(["journalctl", "--disk-usage"], check=True, capture_output=True, text=True)
    evidence = dict(sampled_at=datetime.now(timezone.utc).isoformat(),
                    containers=containers, journal_usage=journal.stdout.strip())
    output = Path(output)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(evidence))
    os.replace(temporary, output)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    capture(parser.parse_args().output)
