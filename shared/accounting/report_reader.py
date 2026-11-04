"""从现有事实表只读生成月度交易经济核对报告。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any

from shared.accounting.economic_ledger import build_economic_report, coverage_gaps


BEIJING = timezone(timedelta(hours=8))


async def read_hrs_summary_provenance(
    db: Any, account_id: str, start_ms: int, end_ms: int,
) -> dict:
    """只读统计账户内 HRS 汇总来源；历史空来源单列，不推断真实性。"""
    if start_ms >= end_ms:
        raise ValueError("时间窗口无效")
    start = datetime.fromtimestamp(start_ms / 1000, BEIJING).replace(tzinfo=None)
    end = datetime.fromtimestamp(end_ms / 1000, BEIJING).replace(tzinfo=None)
    rows = await db.fetch_all(
        "SELECT pnl_source, COUNT(*) AS record_count, "
        "COALESCE(SUM(realized_pnl), 0) AS gross_pnl "
        "FROM trading.trade_records "
        "WHERE account_id = $1 AND strategy = 'hrs' "
        "AND order_type = 'PNL_SUMMARY' "
        "AND executed_at >= $2 AND executed_at < $3 "
        "GROUP BY pnl_source",
        account_id, start, end,
    )
    return {
        "account_id": account_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "sources": {
            str(row["pnl_source"] or "legacy_unknown"): {
                "record_count": int(row["record_count"]),
                "gross_pnl_usdt": str(row["gross_pnl"]),
            }
            for row in rows
        },
        "caliber": "hrs_trade_records_summary_only",
        "limitation": "仅含已标识账户的 HRS 汇总记录；不是交易所净收益或持仓生命周期核实",
    }


_INCOME_SQL = (
    "SELECT account_id, tran_id, symbol, income_type, income, asset, trade_id, event_time_utc "
    "FROM trading.execution_fact_income "
    "WHERE account_id = $1 AND event_time_utc >= $2 AND event_time_utc < $3 "
    "AND income_type = ANY($4::text[])"
)
_FILL_SQL = (
    "SELECT account_id, symbol, trade_id, order_id, commission, commission_asset, "
    "realized_pnl, trade_time_utc "
    "FROM trading.execution_fact_fill "
    "WHERE account_id = $1 AND ((trade_id = ANY($2::bigint[]) "
    "AND symbol = ANY($3::text[])) "
    "OR (trade_time_utc >= $4 AND trade_time_utc < $5))"
)
_OWNER_SQL = (
    "SELECT account_id, symbol, order_id, strategy FROM trading.trade_records "
    "WHERE account_id = $1 AND order_id = ANY($2::text[]) "
    "AND symbol = ANY($3::text[])"
)
_CURSOR_SQL = (
    "SELECT source, account_id, income_type, symbol, window_start_ms, "
    "covered_until_ms, page_complete, request_status "
    "FROM trading.execution_fact_cursor "
    "WHERE account_id = $1 AND window_start_ms < $3 "
    "AND covered_until_ms >= $2"
)
_OVERRIDE_SQL = (
    "SELECT account_id, tran_id, strategy, symbol, income_type, income, asset, "
    "event_time_utc, evidence, reviewed_by "
    "FROM trading.reviewed_income_attribution "
    "WHERE account_id = $1 AND tran_id = ANY($2::bigint[])"
)


async def read_monthly_economic_report(
    db: Any, account_id: str, start_ms: int, end_ms: int,
    *, include_reviewed_overrides: bool = False,
) -> dict:
    """只执行 SELECT；缺游标或未归属时报告不具备策略净额验收资格。"""
    if start_ms >= end_ms:
        raise ValueError("时间窗口无效")
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    incomes = [dict(row) for row in await db.fetch_all(
        _INCOME_SQL, account_id, start, end,
        ["REALIZED_PNL", "COMMISSION", "FUNDING_FEE"],
    )]
    trade_ids = sorted({int(row["trade_id"]) for row in incomes if row.get("trade_id") is not None})
    symbols = sorted({str(row["symbol"]) for row in incomes if row.get("symbol")})
    fills = [dict(row) for row in await db.fetch_all(
        _FILL_SQL, account_id, trade_ids, symbols, start, end,
    )]
    symbols = sorted(set(symbols) | {str(row["symbol"]) for row in fills if row.get("symbol")})
    order_ids = sorted({str(row["order_id"]) for row in fills if row.get("order_id") is not None})
    owners = [dict(row) for row in await db.fetch_all(
        _OWNER_SQL, account_id, order_ids, symbols,
    )]
    cursors = [dict(row) for row in await db.fetch_all(
        _CURSOR_SQL, account_id, start_ms, end_ms,
    )]
    overrides = []
    if include_reviewed_overrides:
        tran_ids = sorted({int(row["tran_id"]) for row in incomes})
        overrides = [dict(row) for row in await db.fetch_all(
            _OVERRIDE_SQL, account_id, tran_ids,
        )]
    report = build_economic_report(fills, incomes, owners, overrides)
    income_keys = {
        (str(row["symbol"]), str(row["trade_id"]), str(row["income_type"]))
        for row in incomes if row.get("trade_id") is not None
    }
    missing_income_fills = []
    for fill in fills:
        trade_time = fill.get("trade_time_utc")
        if trade_time is None or not (start <= trade_time < end):
            continue
        key = (str(fill["symbol"]), str(fill["trade_id"]))
        expected = (
            ("REALIZED_PNL", fill.get("realized_pnl")),
            ("COMMISSION", fill.get("commission")
             if fill.get("commission_asset") == "USDT" else None),
        )
        for kind, amount in expected:
            if amount is not None and Decimal(str(amount)) != 0 and (*key, kind) not in income_keys:
                missing_income_fills.append({
                    "symbol": key[0], "trade_id": key[1], "income_type": kind,
                })
    missing_income_fills.sort(key=lambda row: (row["symbol"], row["trade_id"], row["income_type"]))
    gaps = coverage_gaps(cursors, account_id, start_ms, end_ms, set(symbols))
    income_gaps = [gap for gap in gaps if gap.startswith("income:")]
    account_complete = not income_gaps and not missing_income_fills
    report.update({
        "account_id": account_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "account_coverage_complete": account_complete,
        "strategy_attribution_complete": account_complete and not gaps and not report["unattributed"],
        "coverage_gaps": gaps,
        "missing_income_fills": missing_income_fills,
        "id_match_and_coverage_complete": not gaps and not report["unattributed"] and not missing_income_fills,
        "caliber": "observed_usdt_income_only",
        "reviewed_overrides_enabled": include_reviewed_overrides,
        "limitations": [
            "不含未实现盈亏、划转和账户权益；不是权益守恒报告",
            "历史 trade_records 的 account_id 可能为空，未标识的订单保留未归属",
            "资金费率没有成交 ID 时保留未归属",
        ],
    })
    report["report_version"] = "exchange-income-v1"
    report["report_id"] = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return report
