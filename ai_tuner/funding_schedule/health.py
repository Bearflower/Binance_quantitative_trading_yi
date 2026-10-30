"""Local freshness probe for successful persisted-slot monitoring."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import time


HEARTBEAT_PATH = Path("/tmp/ai-tuner-funding-monitor.heartbeat")


def clear_monitor_heartbeat(*, path: Path = HEARTBEAT_PATH) -> None:
    """A restarted process must not inherit the previous process's health."""
    path.unlink(missing_ok=True)


def write_monitor_heartbeat(*, now: float | None = None, path: Path = HEARTBEAT_PATH) -> None:
    """Atomically publish a heartbeat after a successful database check only."""
    stamp = time.time() if now is None else now
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(str(stamp), encoding="ascii")
    os.replace(temporary, path)


def is_monitor_fresh(*, now: float | None = None, path: Path = HEARTBEAT_PATH,
                     max_age_seconds: float = 180) -> bool:
    stamp = time.time() if now is None else now
    try:
        recorded = float(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return False
    return 0 <= stamp - recorded <= max_age_seconds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=("check",))
    parser.parse_args()
    raise SystemExit(0 if is_monitor_fresh() else 1)


if __name__ == "__main__":
    main()
