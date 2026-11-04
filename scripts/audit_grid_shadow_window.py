#!/usr/bin/env python3
"""只读汇总 grid shadow 参考资格、队列与聚合成交样本证据。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.grid.realtime.queue_audit import (
    summarize_shadow_window, summarize_window)


def summarize_market_window(conn: sqlite3.Connection, start_ms: int,
                            end_ms: int, symbol: str) -> dict:
    """验证整秒时间轴；逐笔证据只覆盖当前仍保留的 ID 范围。"""
    if end_ms <= start_ms or start_ms % 1000 or end_ms % 1000:
        raise ValueError("行情窗口须为正且按整秒对齐")
    sample = conn.execute(
        "SELECT MIN(sample_ms), MAX(sample_ms), COUNT(*), "
        "SUM(CASE WHEN sample_ms%1000!=0 OR anchor_time_ms>sample_ms "
        "OR materialized_at_ms<sample_ms OR trade_count<0 THEN 1 ELSE 0 END), "
        "SUM(trade_count) FROM price_samples_1s "
        "WHERE symbol=? AND sample_ms>=? AND sample_ms<?",
        (symbol, start_ms, end_ms)).fetchone()
    first, last, count, invalid, trade_count = sample
    expected = (end_ms - start_ms) // 1000
    dense = (first == start_ms and last == end_ms - 1000 and
             count == expected and not invalid)
    raw = conn.execute(
        "SELECT MIN(agg_trade_id), MAX(agg_trade_id), COUNT(*), "
        "MIN(trade_time_ms), MAX(trade_time_ms) FROM agg_trades "
        "WHERE symbol=? AND trade_time_ms>? AND trade_time_ms<?",
        (symbol, start_ms - 1000, end_ms)).fetchone()
    first_id, last_id, raw_count, first_raw_ms, last_raw_ms = raw
    contiguous = (raw_count > 0 and raw_count == last_id - first_id + 1)
    raw_full = (contiguous and first_raw_ms is not None and
                first_raw_ms <= start_ms and last_raw_ms is not None and
                last_raw_ms >= end_ms - 1000)
    try:
        attestations = conn.execute(
            "SELECT first_id,last_id,first_trade_ms,last_trade_ms,"
            "id_contiguous FROM aggtrade_integrity_checkpoint "
            "WHERE symbol=? AND last_trade_ms>=? AND first_trade_ms<? "
            "ORDER BY first_trade_ms,checked_at_ms",
            (symbol, start_ms - 1000, end_ms))
        attested = False
        covered_to = None
        previous_last_id = None
        for first_attested_id, last_attested_id, first_attested_ms, last_attested_ms, ok in attestations:
            if not ok or first_attested_ms is None:
                continue
            if covered_to is None:
                if first_attested_ms > start_ms:
                    continue
                covered_to = last_attested_ms
                previous_last_id = last_attested_id
            elif (first_attested_ms <= covered_to + 1000 and
                  first_attested_id <= previous_last_id + 1):
                covered_to = max(covered_to, last_attested_ms)
                previous_last_id = max(previous_last_id, last_attested_id)
            if covered_to is not None and covered_to >= end_ms - 1000:
                attested = True
                break
    except sqlite3.OperationalError:
        attested = False  # 旧 schema 不可补造历史连续性证据
    return {
        "start_ms": start_ms, "end_ms": end_ms,
        "sample_first_ms": first, "sample_last_ms": last,
        "sample_count": count, "expected_sample_count": expected,
        "invalid_sample_count": invalid or 0,
        "sample_trade_count": trade_count or 0,
        "sample_coverage_complete": dense,
        "raw_first_id": first_id, "raw_last_id": last_id,
        "raw_trade_count": raw_count,
        "raw_id_contiguous": contiguous,
        "raw_window_full": raw_full,
        "raw_history_attested": attested,
        "raw_history_limit": None if raw_full or attested else
        "逐笔仅保留滚动窗口，不能单凭当前库证明完整历史逐笔连续",
    }


def _read_only(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid-db", required=True)
    parser.add_argument("--agg-db", required=True)
    parser.add_argument("--start-ms", required=True, type=int)
    parser.add_argument("--end-ms", required=True, type=int)
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--max-gap-ms", type=int, default=5000)
    args = parser.parse_args(argv)
    grid = _read_only(args.grid_db)
    agg = _read_only(args.agg_db)
    try:
        queue = summarize_window(
            grid, start_ms=args.start_ms, end_ms=args.end_ms,
            max_gap_ms=args.max_gap_ms)
        shadow = summarize_shadow_window(
            grid, start_ms=args.start_ms, end_ms=args.end_ms,
            max_gap_ms=args.max_gap_ms)
        market = summarize_market_window(
            agg, args.start_ms, args.end_ms, args.symbol)
    finally:
        grid.close()
        agg.close()
    result = {"queue": queue, "shadow": shadow, "market": market}
    result["evidence_complete"] = (
        queue["queue_window_eligible"] and
        shadow["coverage_complete"] and
        market["sample_coverage_complete"] and
        (market["raw_window_full"] or market["raw_history_attested"]))
    result["data_window_eligible"] = (
        result["evidence_complete"] and
        shadow["research_window_eligible"])
    result["formal_m3_eligible"] = False
    result["formal_m3_blockers"] = [
        "本报告不证明 shadow 边界审计、预注册目标冻结或独立验证段已通过"]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["evidence_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
