"""Read-only evidence manifest for PM algo-triggered income attribution.

This command never writes a strategy owner. A candidate still requires review
before it can enter the published exchange ledger.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import sys
import time

from scripts.export_execution_facts import _build_client_from_env
from scripts.report_economic_ledger import month_window_ms


MAX_ALGO_WINDOW_MS = 6 * 24 * 60 * 60 * 1000
_EVENT_SQL = (
    "SELECT i.account_id, i.symbol, i.trade_id, i.tran_id, i.income_type, "
    "i.income, i.asset, i.event_time_utc, f.order_id, f.realized_pnl, "
    "f.commission, f.commission_asset FROM trading.execution_fact_income i "
    "JOIN trading.execution_fact_fill f ON f.account_id=i.account_id "
    "AND f.symbol=i.symbol AND f.trade_id=i.trade_id "
    "WHERE i.account_id=$1 AND i.event_time_utc >= $2 AND i.event_time_utc < $3 "
    "AND i.income_type IN ('REALIZED_PNL','COMMISSION') AND i.asset='USDT'"
)
_CONDITION_SQL = (
    "SELECT symbol, algo_id, strategy_name FROM btc_eth.condition_orders "
    "WHERE algo_id IS NOT NULL AND symbol=ANY($1::text[])"
)
_OWNER_SQL = (
    "SELECT symbol, order_id, strategy FROM trading.trade_records "
    "WHERE account_id=$1 AND symbol=ANY($2::text[]) AND order_id IS NOT NULL"
)


def assemble_candidates(events, links, condition_owners, existing_owners):
    """Require a unique exchange algo link, unique local strategy and exact amount."""
    by_order = defaultdict(dict)
    for link in links:
        key = (str(link["symbol"]), str(link["actual_order_id"]))
        by_order[key][str(link["algo_id"])] = link
    by_algo = defaultdict(set)
    for row in condition_owners:
        by_algo[(str(row["symbol"]), str(row["algo_id"]))].add(str(row["strategy_name"]))
    existing = defaultdict(set)
    for row in existing_owners:
        existing[(str(row["symbol"]), str(row["order_id"]))].add(str(row["strategy"]))

    candidates, rejected = [], []
    for row in events:
        symbol, order_id = str(row["symbol"]), str(row["order_id"])
        identity = {"account_id": str(row["account_id"]), "symbol": symbol,
                    "income_type": str(row["income_type"]), "tran_id": str(row["tran_id"])}
        order_key = (symbol, order_id)
        linked = by_order.get(order_key, {})
        if len(linked) > 1:
            reason = "ambiguous_algo_link"
        elif not linked:
            reason = "already_owned" if existing.get(order_key) else "no_algo_link"
        else:
            algo_id, link = next(iter(linked.items()))
            strategies = by_algo.get((symbol, algo_id), set())
            if len(strategies) != 1:
                reason = "missing_or_ambiguous_strategy"
            elif existing.get(order_key) and existing[order_key] != strategies:
                reason = "existing_owner_conflict"
            elif existing.get(order_key):
                reason = "already_owned"
            elif row["income_type"] == "COMMISSION" and row["commission_asset"] != "USDT":
                reason = "non_usdt_fill_commission"
            else:
                expected = (Decimal(str(row["realized_pnl"]))
                            if row["income_type"] == "REALIZED_PNL"
                            else -Decimal(str(row["commission"])))
                reason = None if Decimal(str(row["income"])) == expected else "income_fill_mismatch"
        if reason:
            rejected.append(dict(identity, reason=reason))
            continue
        candidates.append(dict(identity, trade_id=str(row["trade_id"]),
                               order_id=order_id, algo_id=algo_id,
                               strategy=next(iter(strategies)),
                               income=str(row["income"]),
                               asset=str(row["asset"]),
                               event_time_utc=row["event_time_utc"].isoformat(),
                               algo_payload_sha256=link["payload_sha256"],
                               algo_status=link.get("algo_status"),
                               algo_create_time_ms=link.get("create_time_ms"),
                               algo_trigger_time_ms=link.get("trigger_time_ms")))
    sort_key = lambda item: (item["symbol"], item["income_type"], item["tran_id"])
    return {"candidates": sorted(candidates, key=sort_key),
            "rejected": sorted(rejected, key=sort_key)}


async def _fetch_links(client, symbols, start_ms, end_ms):
    links = []
    for symbol in symbols:
        left = start_ms
        while left < end_ms:
            right = min(left + MAX_ALGO_WINDOW_MS, end_ms)
            rows = await client._request("GET", "/papi/v1/um/algo/allAlgoOrders", {
                "symbol": symbol, "startTime": left, "endTime": right - 1,
                "limit": 1000,
            }, signed=True)
            if not isinstance(rows, list) or len(rows) >= 1000:
                raise RuntimeError(f"条件单历史未证实完整: {symbol} {left}-{right}")
            for row in rows:
                if str(row.get("symbol")) != symbol:
                    raise RuntimeError(f"条件单币种不符: {symbol}")
                actual = row.get("actualOrderId")
                if actual in (None, "", "0", 0):
                    continue
                algo_id, order_id = int(row["algoId"]), int(actual)
                if algo_id <= 0 or order_id <= 0:
                    raise RuntimeError(f"条件单标识无效: {symbol}")
                digest = hashlib.sha256(json.dumps(row, sort_keys=True,
                    separators=(",", ":"), default=str).encode()).hexdigest()
                links.append({"symbol": symbol, "algo_id": algo_id,
                              "actual_order_id": order_id,
                              "payload_sha256": digest,
                              "algo_status": row.get("algoStatus"),
                              "create_time_ms": row.get("createTime"),
                              "trigger_time_ms": row.get("triggerTime")})
            left = right
    return links


async def _run(month, account_id):
    from shared.database import DatabaseManager

    start_ms, end_ms = month_window_ms(month)
    end_ms = min(end_ms, int(time.time() * 1000) - 1000)
    if start_ms >= end_ms:
        raise ValueError("月份尚未开始")
    password = os.environ.get("DATABASE_PASSWORD")
    if not password:
        raise RuntimeError("缺少 DATABASE_PASSWORD")
    db = DatabaseManager(host=os.environ.get("DATABASE_HOST", "localhost"),
                         port=int(os.environ.get("DATABASE_PORT", "5432")),
                         database=os.environ.get("DATABASE_NAME", "trading_platform"),
                         user=os.environ.get("DATABASE_USER", "trading_user"),
                         password=password, min_pool_size=1, max_pool_size=2)
    client = None
    await db.connect()
    try:
        start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
        end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
        events = [dict(row) for row in await db.fetch_all(_EVENT_SQL, account_id, start, end)]
        symbols = sorted({str(row["symbol"]) for row in events})
        if not symbols:
            return {"account_id": account_id, "month": month, "candidates": [], "rejected": []}
        conditions = [dict(row) for row in await db.fetch_all(_CONDITION_SQL, symbols)]
        existing = [dict(row) for row in await db.fetch_all(_OWNER_SQL, account_id, symbols)]
        client, code = _build_client_from_env()
        if client is None:
            raise RuntimeError(f"币安客户端初始化失败: {code}")
        if not client.use_unified_account:
            raise RuntimeError("仅支持币安 PM 账户")
        links = await _fetch_links(client, symbols, start_ms, end_ms)
        result = assemble_candidates(events, links, conditions, existing)
        return {"account_id": account_id, "month": month,
                "start_ms": start_ms, "end_ms": end_ms,
                "algo_links_observed": len(links), "income_events_examined": len(events),
                **result}
    finally:
        if client is not None:
            await client.close()
        await db.disconnect()


def main():
    parser = argparse.ArgumentParser(description="只读核对条件单触发成交与收入；不写入策略归属")
    parser.add_argument("--month", required=True)
    parser.add_argument("--account-id", default=os.environ.get("TRADE_ACCOUNT_ID", "default"))
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(_run(args.month, args.account_id)),
                         ensure_ascii=False, sort_keys=True, indent=2))
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"条件单归属审计失败: {exc}\n")


if __name__ == "__main__":
    main()
