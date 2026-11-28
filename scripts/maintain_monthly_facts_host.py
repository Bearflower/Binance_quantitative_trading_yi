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


def windows(now: datetime, *, pm: bool, cutover_ms: int | None) -> list[tuple[int, int]]:
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    local = now.astimezone(BEIJING)
    current = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    current_ms = int(current.timestamp() * 1000)
    result = []
    if local.day <= 2:
        previous = (current - timedelta(days=1)).replace(day=1)
        result.append((int(previous.timestamp() * 1000), current_ms - 1))
    result.append((current_ms, int(now.timestamp() * 1000) - 1000))
    if cutover_ms is None:
        if not pm:
            raise ValueError("经典账户缺少切点")
    elif type(cutover_ms) is not int or cutover_ms <= 0:
        raise ValueError("切点格式无效")
    elif pm:
        result = [(start, min(end, cutover_ms - 1)) for start, end in result]
    else:
        result = [(max(start, cutover_ms), end) for start, end in result]
    return [(start, end) for start, end in result if start <= end]


def main() -> int:
    probe = ("import json; from shared.binance_account_mode import configured_pm; "
             "from shared.accounting.account_cutover import classic_cutover_ms; "
             "print(json.dumps([configured_pm(), classic_cutover_ms()]))")
    pm, cutover = json.loads(subprocess.check_output(
        ['/usr/bin/docker', 'exec', CONTAINER, 'python', '-c', probe], text=True, timeout=30))
    if type(pm) is not bool:
        raise ValueError("采集器账户模式未知")
    for start_ms, end_ms in windows(datetime.now(timezone.utc), pm=pm, cutover_ms=cutover):
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
