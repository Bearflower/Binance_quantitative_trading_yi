"""Pinned-release bridge: keep exchange fact coverage current without replacing images.

The deployed collector still runs a moving two-day window. This host-side job
uses its existing explicit-window CLI until a reviewed successor release ships
the calendar-month collector mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys

BEIJING = timezone(timedelta(hours=8))
CONTAINER = 'trading_system-execution-facts'


def windows(now: datetime) -> list[tuple[int, int]]:
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    local = now.astimezone(BEIJING)
    current = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    current_ms = int(current.timestamp() * 1000)
    result = []
    if local.day <= 2:
        previous = (current - timedelta(days=1)).replace(day=1)
        result.append((int(previous.timestamp() * 1000), current_ms))
    result.append((current_ms, int(now.timestamp() * 1000) - 1000))
    return result


def main() -> int:
    for start_ms, end_ms in windows(datetime.now(timezone.utc)):
        command = [
            '/usr/bin/docker', 'exec', CONTAINER, 'python', '-m',
            'scripts.collect_execution_facts', '--start-ms', str(start_ms),
            '--end-ms', str(end_ms),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        print(json.dumps({
            'start_ms': start_ms, 'end_ms': end_ms, 'exit_code': result.returncode,
            'summary': [line for line in result.stdout.splitlines() if line.startswith('{')][-1:],
            'error_tail': result.stderr.splitlines()[-3:] if result.returncode else [],
        }, ensure_ascii=False), flush=True)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    sys.exit(main())
