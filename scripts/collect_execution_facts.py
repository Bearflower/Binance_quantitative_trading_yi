"""采集 PM 账户近期成交、已实现盈亏、手续费和资金费原始事实。

按最多七天分窗；先读账户收入中的币种，再合并本地订单币种采集成交。
只写 execution_fact_*，不修改策略交易记录或资金分配。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os

from scripts.export_execution_facts import (
    ExecutionFactExporter, _build_client_from_env, _maybe_open_sink,
    _maybe_close_sink,
)


MAX_CHUNK_MS = 7 * 24 * 60 * 60 * 1000
INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
BEIJING = timezone(timedelta(hours=8))
_SYMBOL_SQL = (
    "SELECT DISTINCT symbol FROM trading.trade_records "
    "WHERE executed_at >= $1 AND executed_at < $2 AND symbol IS NOT NULL"
)
_INCOME_SYMBOL_SQL = (
    "SELECT DISTINCT symbol FROM trading.execution_fact_income "
    "WHERE account_id = $1 AND event_time_utc >= $2 AND event_time_utc < $3 "
    "AND symbol IS NOT NULL AND symbol <> ''"
)


def _windows(start_ms: int, end_ms: int):
    if start_ms < 0 or start_ms >= end_ms:
        raise ValueError("采集窗口无效")
    left = start_ms
    while left < end_ms:
        right = min(left + MAX_CHUNK_MS, end_ms)
        yield left, right
        left = right


async def collect_window(
    exporter, db, start_ms: int, end_ms: int, *, pause_seconds: float = 0.5,
) -> dict:
    """按币安 PM 的七天成交查询上限分窗，返回明确的覆盖状态。"""
    if pause_seconds < 0:
        raise ValueError("pause_seconds 不能为负数")
    windows = list(_windows(start_ms, end_ms))
    start_local = datetime.fromtimestamp(start_ms / 1000, BEIJING).replace(tzinfo=None)
    end_local = datetime.fromtimestamp(end_ms / 1000, BEIJING).replace(tzinfo=None)
    rows = await db.fetch_all(_SYMBOL_SQL, start_local, end_local)
    symbols = {str(row["symbol"]) for row in rows if row.get("symbol")}
    gaps: list[str] = []
    income_count = 0
    fill_count = 0

    for left, right in windows:
        for kind in INCOME_TYPES:
            result = await exporter.export_income(left, right, income_type=kind)
            income_count += len(result.records)
            symbols.update(str(row.symbol) for row in result.records if row.symbol)
            if result.errors or not result.page_complete:
                gaps.append(f"income:{kind}:{left}-{right}")
            if pause_seconds:
                await asyncio.sleep(pause_seconds)

    # 已完成的收入游标会让重试返回空 records；仍需回查持久化收入的币种，
    # 否则首次成交采集失败的收入独有币种会在重试时被误判为完成。
    stored_income_symbols = await db.fetch_all(
        _INCOME_SYMBOL_SQL, exporter.account_id,
        datetime.fromtimestamp(start_ms / 1000, timezone.utc),
        datetime.fromtimestamp(end_ms / 1000, timezone.utc),
    )
    symbols.update(str(row["symbol"]) for row in stored_income_symbols if row.get("symbol"))

    for symbol in sorted(symbols):
        for left, right in windows:
            result = await exporter.export_fills(symbol, left, right)
            fill_count += len(result.records)
            if result.errors or not result.page_complete:
                gaps.append(f"fills:{symbol}:{left}-{right}")
            if pause_seconds:
                await asyncio.sleep(pause_seconds)

    return {
        "account_id": exporter.account_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "income_events_observed": income_count,
        "fill_events_observed": fill_count,
        "symbol_count": len(symbols),
        "complete": not gaps,
        "gaps": gaps,
    }


async def _run(args) -> int:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if (args.start_ms is None) != (args.end_ms is None):
        raise ValueError("必须同时提供 --start-ms 和 --end-ms")
    if args.start_ms is None:
        if args.days <= 0:
            raise ValueError("--days 必须大于零")
        start_ms, end_ms = now_ms - args.days * 24 * 60 * 60 * 1000, now_ms
    else:
        start_ms, end_ms = args.start_ms, args.end_ms
        if end_ms > now_ms:
            raise ValueError("窗口终点不能位于未来")
    if os.environ.get("EXECUTION_FACTS_DB_WRITE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("持续采集要求 EXECUTION_FACTS_DB_WRITE=true")

    sink, db = await _maybe_open_sink()
    client = None
    try:
        client, code = _build_client_from_env()
        if client is None:
            raise RuntimeError(f"币安客户端初始化失败，exit={code}")
        exporter = ExecutionFactExporter(
            account_id=args.account_id, api_client=client, sink=sink,
            page_limit=args.page_limit,
        )
        report = await collect_window(
            exporter, db, start_ms, end_ms, pause_seconds=args.pause_seconds,
        )
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["complete"] else 2
    finally:
        if client is not None:
            await client.close()
        await _maybe_close_sink(db)


def main() -> None:
    parser = argparse.ArgumentParser(description="采集 PM 账户收入和成交事实，不改策略交易记录")
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--account-id", default=os.environ.get("TRADE_ACCOUNT_ID", "default"))
    parser.add_argument("--page-limit", type=int, default=1000)
    parser.add_argument("--pause-seconds", type=float, default=0.5)
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(args))
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"采集失败：{exc}\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
