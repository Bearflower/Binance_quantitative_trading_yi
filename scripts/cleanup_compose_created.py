"""Remove stale Compose replacement containers left by failed recreates.

Only a never-started replacement is eligible, and only while its canonical
service container is running and healthy. A recent replacement is left alone
so this script cannot interfere with an in-progress Compose operation.
"""

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable, List, Optional


_TEMP_NAME = re.compile(r"^[0-9a-f]{12}_(.+)$")
_PROJECT = "trading_system"
_MIN_AGE_SECONDS = 300


def cleanup_created_containers(
    run: Callable = subprocess.run,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> List[str]:
    """Return IDs removed; raise before touching an ambiguous replacement."""
    now = now or datetime.now(timezone.utc)

    def docker(*args):
        return run(["docker", *args], capture_output=True, text=True, check=True).stdout

    created_ids = docker("ps", "-a", "--filter", "status=created", "--format", "{{.ID}}").splitlines()
    removed = []
    for cid in created_ids:
        candidate = json.loads(docker("inspect", "--type", "container", cid))[0]
        name = candidate.get("Name", "").lstrip("/")
        match = _TEMP_NAME.fullmatch(name)
        if not match:
            continue
        labels = candidate.get("Config", {}).get("Labels") or {}
        if candidate.get("State", {}).get("Status") != "created" or labels.get(
            "com.docker.compose.project"
        ) != _PROJECT:
            continue
        if candidate["State"].get("StartedAt") != "0001-01-01T00:00:00Z":
            raise RuntimeError(f"Compose replacement {name} was previously started; abort cleanup")
        created_at = datetime.fromisoformat(candidate["Created"].replace("Z", "+00:00"))
        if (now - created_at).total_seconds() < _MIN_AGE_SECONDS:
            raise RuntimeError(f"Found recent Compose replacement {name}; abort deployment")

        canonical_name = match.group(1)
        canonical = json.loads(docker("inspect", "--type", "container", canonical_name))[0]
        canonical_labels = canonical.get("Config", {}).get("Labels") or {}
        state = canonical.get("State", {})
        health = state.get("Health", {}).get("Status")
        if (
            canonical.get("Name") != f"/{canonical_name}"
            or canonical.get("Id") == candidate.get("Id")
            or canonical_labels.get("com.docker.compose.project") != _PROJECT
            or canonical_labels.get("com.docker.compose.service")
            != labels.get("com.docker.compose.service")
            or state.get("Status") != "running"
            or health not in (None, "healthy")
        ):
            raise RuntimeError(f"Canonical container {canonical_name} is not healthy; abort cleanup")
        # Config hashes can differ: a failed recreate may target a newer config.
        key = "com.docker.compose.container-number"
        if not labels.get(key) or canonical_labels.get(key) != labels.get(key):
            raise RuntimeError(f"Compose replacement {name} does not match canonical container; abort cleanup")

        if not dry_run:
            rechecked = json.loads(docker("inspect", "--type", "container", cid))[0]
            if (
                rechecked.get("Id") != candidate.get("Id")
                or rechecked.get("Name") != candidate.get("Name")
                or rechecked.get("Config", {}).get("Labels") != labels
                or rechecked.get("State", {}).get("Status") != "created"
                or rechecked.get("State", {}).get("StartedAt") != "0001-01-01T00:00:00Z"
            ):
                raise RuntimeError(f"Compose replacement {name} changed before removal; abort cleanup")
            docker("rm", cid)  # No -f: a container that started meanwhile must not be removed.
        action = "Would remove" if dry_run else "Removed"
        print(f"{action} stale Compose replacement: {name} ({cid})")
        removed.append(cid)
    return removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cleanup_created_containers(dry_run=args.dry_run)
