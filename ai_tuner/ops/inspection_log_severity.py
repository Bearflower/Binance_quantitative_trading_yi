"""巡检按事件级别分类，不将异常类名中的 Error 当成事件级别。"""

from __future__ import annotations

import argparse
import json
import re
import sys


def severity(line: str) -> str:
    line = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line).strip()
    # HTTP 5xx 的日志经常仍使用 INFO 级别，必须保持告警。
    if re.search(r'^INFO:\s+\S+\s+-\s+"[^"]* HTTP/[0-9.]+"\s+5[0-9]{2}\b', line):
        return "error"
    if re.match(r"^\d{4}-\d\d-\d\d .*\[\d+\] .*\b(?:ERROR|FATAL|PANIC):", line):
        return "error"
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        event = None
    level = event.get("level", event.get("levelname")) if isinstance(event, dict) else None
    if not isinstance(level, str):
        match = re.match(r"^(?:\d{4}-\d\d-\d\d(?:[T ][0-9:.+Z-]+)?\s+)?\[(debug|info|warning|warn|error|critical|fatal)\s*\]", line, re.I)
        if match is None:
            match = re.match(r"^(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL):", line)
        level = match.group(1) if match else None
    if level:
        level = level.lower()
        if level in {"warning", "warn"}:
            return "warning"
        if level in {"error", "critical", "fatal"}:
            return "error"
        if level in {"info", "debug"}:
            return level
    # 该采集器使用 logging 的裸消息渲染；只识别已核实的精确重试事件。
    if re.fullmatch(r"(?:XAUUSDT funding|Funding schedule) refresh unavailable for \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00: \w+", line):
        return "warning"
    return "error"  # 无级别的异常、未知格式保持可见。


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--level", choices=("error", "warning"), required=True)
    args = parser.parse_args()
    for line in sys.stdin:
        if line.strip() and severity(line) == args.level:
            print(line.rstrip())
