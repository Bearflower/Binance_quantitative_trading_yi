"""Export current unassigned exchange income as a reviewable JSON source.

The output is private input for a workbook; no attribution or ledger row is changed.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time

from scripts.report_economic_ledger import month_window_ms


_DETAIL_SQL = (
    "SELECT i.account_id, i.income_type, i.tran_id, i.symbol, i.income, "
    "i.asset, i.event_time_utc, i.trade_id, f.order_id "
    "FROM trading.execution_fact_income i LEFT JOIN trading.execution_fact_fill f "
    "ON f.account_id=i.account_id AND f.symbol=i.symbol AND f.trade_id=i.trade_id "
    "WHERE i.account_id=$1 AND i.event_time_utc >= $2 AND i.event_time_utc < $3 "
    "AND i.income_type IN ('REALIZED_PNL','COMMISSION','FUNDING_FEE')"
)
_LEGACY_SQL = (
    "SELECT symbol, order_id, MIN(strategy) AS strategy, "
    "COUNT(DISTINCT strategy) AS strategy_count FROM trading.trade_records "
    "WHERE account_id IS NULL AND order_id IS NOT NULL "
    "GROUP BY symbol, order_id"
)
IDENTITY_FIELDS = (
    "account_id", "income_type", "tran_id", "symbol", "income_usdt",
    "asset", "event_time_utc", "trade_id", "order_id",
)


def digest_identity(row):
    payload = {key: row.get(key, "") for key in IDENTITY_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def make_review_rows(unattributed, details, candidates, legacy_owners):
    detail_map = {}
    for detail in details:
        key = (str(detail["account_id"]), str(detail["income_type"]), str(detail["tran_id"]))
        if key in detail_map:
            raise ValueError(f"收入事实键重复: {key}")
        detail_map[key] = detail
    candidate_map = {
        (str(item["account_id"]), str(item["income_type"]), str(item["tran_id"])): item
        for item in candidates
    }
    legacy_map = {}
    for owner in legacy_owners:
        if int(owner["strategy_count"]) == 1:
            legacy_map[(str(owner["symbol"]), str(owner["order_id"]))] = str(owner["strategy"])
    rows = []
    for event in unattributed:
        key = (str(event["account_id"]), str(event["income_type"]), str(event["tran_id"]))
        if key not in detail_map:
            raise ValueError(f"未归属事件缺少事实: {key}")
        detail = detail_map[key]
        if (str(detail["symbol"]) != str(event["symbol"])
                or str(detail["asset"]) != str(event["asset"])
                or Decimal(str(detail["income"])) != Decimal(str(event["amount"]))):
            raise ValueError(f"未归属事件与事实金额不一致: {key}")
        row = {
            "account_id": key[0], "income_type": key[1], "tran_id": key[2],
            "symbol": str(detail["symbol"]), "income_usdt": str(detail["income"]),
            "asset": str(detail["asset"]),
            "event_time_utc": detail["event_time_utc"].isoformat(),
            "trade_id": str(detail["trade_id"]) if detail["trade_id"] is not None else "",
            "order_id": str(detail["order_id"]) if detail["order_id"] is not None else "",
            "unattributed_reason": str(event["reason"]),
            "suggested_strategy": "", "suggestion_basis": "", "evidence_ref": "",
        }
        candidate = candidate_map.get(key)
        if candidate is not None:
            same = (str(candidate.get("symbol")) == row["symbol"]
                    and Decimal(str(candidate.get("income"))) == Decimal(row["income_usdt"])
                    and str(candidate.get("order_id")) == row["order_id"]
                    and str(candidate.get("event_time_utc")) == row["event_time_utc"])
            if not same:
                raise ValueError(f"条件单候选与收入事实不一致: {key}")
            row.update(suggested_strategy=str(candidate["strategy"]),
                       suggestion_basis="币安条件单触发订单+成交金额一致",
                       evidence_ref=f"algo:{candidate['algo_id']} sha256:{candidate['algo_payload_sha256']}")
        elif row["order_id"]:
            legacy = legacy_map.get((row["symbol"], row["order_id"]))
            if legacy:
                row.update(suggested_strategy=legacy,
                           suggestion_basis="旧订单记录缺账户标识，须核实账户",
                           evidence_ref=f"legacy_order:{row['order_id']}")
        row["identity_sha256"] = digest_identity(row)
        row.update(review_decision="", confirmed_strategy="", reviewed_by="", evidence_note="")
        rows.append(row)
    rows.sort(key=lambda row: (row["event_time_utc"], row["income_type"], row["tran_id"]))
    if len({(r["account_id"], r["income_type"], r["tran_id"]) for r in rows}) != len(rows):
        raise ValueError("导出存在重复收入身份")
    return rows


async def _run(month, account_id, candidate_json, output):
    from shared.database import DatabaseManager
    from shared.accounting.report_reader import read_monthly_economic_report

    start_ms, end_ms = month_window_ms(month)
    end_ms = min(end_ms, int(time.time() * 1000) - 1000)
    if start_ms >= end_ms:
        raise ValueError("月份尚未开始")
    password = os.environ.get("DATABASE_PASSWORD")
    if not password:
        raise RuntimeError("缺少 DATABASE_PASSWORD")
    candidates = []
    if candidate_json:
        audit = json.loads(Path(candidate_json).read_text())
        if audit.get("account_id") != account_id or audit.get("month") != month:
            raise ValueError("候选审计文件账户或月份不符")
        candidates = audit["candidates"]
    db = DatabaseManager(host=os.environ.get("DATABASE_HOST", "localhost"),
                         port=int(os.environ.get("DATABASE_PORT", "5432")),
                         database=os.environ.get("DATABASE_NAME", "trading_platform"),
                         user=os.environ.get("DATABASE_USER", "trading_user"),
                         password=password, min_pool_size=1, max_pool_size=2)
    await db.connect()
    try:
        report = await read_monthly_economic_report(db, account_id, start_ms, end_ms,
                                                     include_reviewed_overrides=True)
        start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
        end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
        details = [dict(row) for row in await db.fetch_all(_DETAIL_SQL, account_id, start, end)]
        legacy = [dict(row) for row in await db.fetch_all(_LEGACY_SQL)]
        rows = make_review_rows(report["unattributed"], details, candidates, legacy)
        result = {"month": month, "account_id": account_id,
                  "start_ms": start_ms, "end_ms": end_ms,
                  "report_id": report["report_id"], "rows": rows}
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(json.dumps({"output": output, "unattributed_count": len(rows),
                          "suggested_count": sum(bool(r["suggested_strategy"]) for r in rows)},
                         ensure_ascii=False))
    finally:
        await db.disconnect()


def main():
    parser = argparse.ArgumentParser(description="导出未归属收入复核源；只读数据库")
    parser.add_argument("--month", required=True)
    parser.add_argument("--account-id", default=os.environ.get("TRADE_ACCOUNT_ID", "default"))
    parser.add_argument("--candidate-json")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    asyncio.run(_run(args.month, args.account_id, args.candidate_json, args.output))


if __name__ == "__main__":
    main()
