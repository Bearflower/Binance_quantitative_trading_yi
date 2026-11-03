#!/usr/bin/env python3
"""只读报告 grid shadow 队列证据；仅判队列，不替代行情/参考/边界验收。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.grid.realtime.queue_audit import summarize_window


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--hours", type=int, default=72)
    parser.add_argument("--max-gap-ms", type=int, default=5000)
    parser.add_argument("--end-ms", type=int)
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours must be positive")
    end_ms = args.end_ms if args.end_ms is not None else int(time.time() * 1000)
    uri = f"file:{args.db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = summarize_window(
            conn, start_ms=end_ms - args.hours * 3600000,
            end_ms=end_ms, max_gap_ms=args.max_gap_ms)
    finally:
        conn.close()
    result["scope"] = "queue_only"
    result["m3_research_eligible"] = None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["queue_window_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
