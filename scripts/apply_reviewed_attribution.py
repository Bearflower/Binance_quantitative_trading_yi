"""Validate and persist operator-reviewed exchange income ownership.

Dry-run by default. --apply inserts only exact, reviewed events in one transaction;
it never modifies execution facts, strategy trade records or existing overrides.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path


STRATEGIES = {"hrs", "btc_eth", "btc_eth_aggressive", "new_coin", "grid"}
IDENTITY_FIELDS = (
    "account_id", "income_type", "tran_id", "symbol", "income_usdt",
    "asset", "event_time_utc", "trade_id", "order_id",
)
_FACT_SQL = (
    "SELECT i.account_id, i.income_type, i.tran_id, i.symbol, i.income, "
    "i.asset, i.event_time_utc, i.trade_id, f.order_id "
    "FROM trading.execution_fact_income i LEFT JOIN trading.execution_fact_fill f "
    "ON f.account_id=i.account_id AND f.symbol=i.symbol AND f.trade_id=i.trade_id "
    "WHERE i.account_id=$1 AND i.income_type=$2 AND i.tran_id=$3"
)
_EXISTING_SQL = (
    "SELECT strategy, symbol, income, asset, event_time_utc, reviewed_by "
    "FROM trading.reviewed_income_attribution "
    "WHERE account_id=$1 AND income_type=$2 AND tran_id=$3"
)
_INSERT_SQL = (
    "INSERT INTO trading.reviewed_income_attribution "
    "(account_id, income_type, tran_id, strategy, symbol, income, asset, "
    "event_time_utc, evidence, reviewed_by) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10) "
    "ON CONFLICT (account_id, income_type, tran_id) DO NOTHING RETURNING tran_id"
)


def identity_digest(row):
    payload = {key: row.get(key, "") for key in IDENTITY_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def validate_entry(row, live):
    if identity_digest(row) != row.get("identity_sha256"):
        raise ValueError("收入身份摘要不一致")
    if row.get("strategy") not in STRATEGIES:
        raise ValueError("确认策略无效")
    if not str(row.get("reviewed_by") or "").strip():
        raise ValueError("审核人缺失")
    if len(str(row.get("evidence_note") or "").strip()) < 8:
        raise ValueError("证据备注不足")
    if live is None:
        raise ValueError("数据库中找不到收入事实")
    expected = {
        "account_id": str(live["account_id"]),
        "income_type": str(live["income_type"]),
        "tran_id": str(live["tran_id"]),
        "symbol": str(live["symbol"]),
        "asset": str(live["asset"]),
        "event_time_utc": live["event_time_utc"].isoformat(),
        "trade_id": str(live["trade_id"]) if live["trade_id"] is not None else "",
        "order_id": str(live["order_id"]) if live["order_id"] is not None else "",
    }
    for key, expected_value in expected.items():
        if str(row.get(key, "")) != expected_value:
            label = "订单号" if key == "order_id" else key
            raise ValueError(f"交易所事实{label}不一致")
    if Decimal(str(row["income_usdt"])) != Decimal(str(live["income"])):
        raise ValueError("交易所收入金额不一致")
    if row["asset"] != "USDT":
        raise ValueError("仅允许 USDT 收入复核")
    return row


def _no_duplicate_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复: {key}")
        result[key] = value
    return result


async def _run(batch_path, apply):
    from shared.database import DatabaseManager

    batch = json.loads(Path(batch_path).read_text(), object_pairs_hook=_no_duplicate_json_pairs)
    rows = batch.get("rows")
    if not isinstance(rows, list):
        raise ValueError("复核批次缺少 rows")
    if apply and not rows:
        raise ValueError("没有确认的收入事件，拒绝空批次写入")
    keys = [(str(row.get("account_id")), str(row.get("income_type")), str(row.get("tran_id")))
            for row in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("复核批次存在重复收入键")
    if any(key[0] != batch.get("account_id") for key in keys):
        raise ValueError("复核批次账户不一致")
    password = os.environ.get("DATABASE_PASSWORD")
    if not password:
        raise RuntimeError("缺少 DATABASE_PASSWORD")
    db = DatabaseManager(host=os.environ.get("DATABASE_HOST", "localhost"),
                         port=int(os.environ.get("DATABASE_PORT", "5432")),
                         database=os.environ.get("DATABASE_NAME", "trading_platform"),
                         user=os.environ.get("DATABASE_USER", "trading_user"),
                         password=password, min_pool_size=1, max_pool_size=2)
    await db.connect()
    inserted = skipped = 0
    try:
        async with db.pool.acquire() as connection:
            async with connection.transaction(isolation="serializable"):
                checked = []
                for row in rows:
                    key = (row["account_id"], row["income_type"], int(row["tran_id"]))
                    live = await connection.fetchrow(_FACT_SQL, *key)
                    validate_entry(row, live)
                    existing = await connection.fetchrow(_EXISTING_SQL, *key)
                    if existing:
                        same = (existing["strategy"] == row["strategy"]
                                and existing["symbol"] == row["symbol"]
                                and Decimal(str(existing["income"])) == Decimal(row["income_usdt"])
                                and existing["asset"] == row["asset"]
                                and existing["event_time_utc"].isoformat() == row["event_time_utc"])
                        if not same:
                            raise ValueError(f"已有人工归属与复核批次冲突: {key}")
                        skipped += 1
                    else:
                        checked.append((key, row, live))
                if apply:
                    for key, row, live in checked:
                        evidence = {
                            "source": "manual_workbook_review",
                            "source_report_id": batch.get("source_report_id"),
                            "source_end_ms": batch.get("source_end_ms"),
                            "identity_sha256": row["identity_sha256"],
                            "review_note": row["evidence_note"],
                            "suggested_strategy": row.get("suggested_strategy"),
                            "suggestion_basis": row.get("suggestion_basis"),
                            "evidence_ref": row.get("evidence_ref"),
                        }
                        added = await connection.fetchrow(_INSERT_SQL,
                            key[0], key[1], key[2], row["strategy"], row["symbol"],
                            Decimal(row["income_usdt"]), row["asset"],
                            datetime.fromisoformat(row["event_time_utc"].replace("Z", "+00:00")),
                            json.dumps(evidence, ensure_ascii=False), row["reviewed_by"])
                        if not added:
                            raise ValueError(f"并发写入冲突: {key}")
                        inserted += 1
                else:
                    inserted = len(checked)
    finally:
        await db.disconnect()
    return {"mode": "apply" if apply else "dry_run", "reviewed": len(rows),
            "would_insert" if not apply else "inserted": inserted,
            "already_present": skipped}


def main():
    parser = argparse.ArgumentParser(description="逐笔复核归属入库；默认仅校验，--apply 才写入")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args.batch, args.apply)), ensure_ascii=False))


if __name__ == "__main__":
    main()
