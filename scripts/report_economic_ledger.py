"""只读月度交易经济核对：python3 -m scripts.report_economic_ledger --month YYYY-MM。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import sys

from ai_tuner.accounting.report_reader import (
    read_monthly_economic_report, read_hrs_summary_provenance,
)


BEIJING = timezone(timedelta(hours=8))


def month_window_ms(month: str) -> tuple[int, int]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=BEIJING)
    except ValueError as exc:
        raise ValueError("月份格式必须为 YYYY-MM") from exc
    if start.strftime("%Y-%m") != month:
        raise ValueError("月份格式必须为 YYYY-MM")
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


async def _run(
    month: str, account_id: str, *, include_reviewed_overrides: bool = False,
    include_hrs_provenance: bool = False,
) -> int:
    from shared.database import DatabaseManager

    password = os.environ.get("DATABASE_PASSWORD")
    if not password:
        raise RuntimeError("缺少 DATABASE_PASSWORD，未连接数据库")
    start_ms, end_ms = month_window_ms(month)
    db = DatabaseManager(
        host=os.environ.get("DATABASE_HOST", "localhost"),
        port=int(os.environ.get("DATABASE_PORT", "5432")),
        database=os.environ.get("DATABASE_NAME", "trading_platform"),
        user=os.environ.get("DATABASE_USER", "trading_user"),
        password=password,
    )
    await db.connect()
    try:
        report = await read_monthly_economic_report(
            db, account_id, start_ms, end_ms,
            include_reviewed_overrides=include_reviewed_overrides,
        )
        if include_hrs_provenance:
            report["hrs_summary_provenance"] = await read_hrs_summary_provenance(
                db, account_id, start_ms, end_ms,
            )
    finally:
        await db.disconnect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["id_match_and_coverage_complete"] else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="只读核对收入、手续费与策略归属；不会修改月度分配")
    parser.add_argument("--month", required=True, help="北京时间自然月，YYYY-MM")
    parser.add_argument("--account-id", default=os.environ.get("TRADE_ACCOUNT_ID", "default"))
    parser.add_argument("--use-reviewed-overrides", action="store_true",
                        help="读取已复核的逐事件归属表；需要先部署对应表")
    parser.add_argument("--include-hrs-provenance", action="store_true",
                        help="附加 HRS 汇总来源分布；需要先部署 pnl_source 列")
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(
            args.month, args.account_id,
            include_reviewed_overrides=args.use_reviewed_overrides,
            include_hrs_provenance=args.include_hrs_provenance,
        ))
    except (ValueError, RuntimeError) as exc:
        print(f"核对失败：{exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
