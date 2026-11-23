"""Production container stdin probe. Uses only signed GETs and read-only SELECTs."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import os
import re

import asyncpg
import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
from shared.binance_api import BinanceClient

UTC = timezone.utc
START_BY_STRATEGY = {
    "btc_eth": datetime(2026, 11, 23, 6, 42, 31, 700917, tzinfo=UTC),
    "btc_eth_aggressive": datetime(2026, 11, 23, 6, 42, 53, 35033, tzinfo=UTC),
}
START = min(START_BY_STRATEGY.values())
ACCOUNT = "default"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
STRATEGIES = ("btc_eth", "btc_eth_aggressive")


async def inspect_first(db, api):
    reservations = {}
    recorded_owners = {}
    async with db.transaction(readonly=True):
        for row in await db.fetch(
                "SELECT client_order_id,symbol,geometry FROM trading.entry_risk_reservation "
                "WHERE account_id=$1 AND strategy_id=$2 AND created_at >= $3",
                ACCOUNT, STRATEGIES[1], START):
            geometry = row["geometry"]
            if isinstance(geometry, str):
                geometry = json.loads(geometry)
            reservations[(row["symbol"], row["client_order_id"])] = geometry.get("signal", {})

        cutoff_beijing = (START + timedelta(hours=8)).replace(tzinfo=None)
        owners = await db.fetch(
            "SELECT symbol,order_id::text AS order_id,strategy FROM trading.order_strategy_evidence "
            "WHERE account_id=$1 AND observed_at >= $2 AND strategy=ANY($3::text[]) UNION "
            "SELECT symbol,order_id::text AS order_id,strategy FROM trading.trade_records "
            "WHERE account_id=$1 AND executed_at >= $4 AND strategy=ANY($3::text[]) AND order_id IS NOT NULL",
            ACCOUNT, START, list(STRATEGIES), cutoff_beijing,
        )
        for row in owners:
            recorded_owners.setdefault((row["symbol"], row["order_id"]), set()).add(row["strategy"])

    result = {strategy: None for strategy in STRATEGIES}
    now = datetime.now(UTC)
    oldest = START
    found = []
    while oldest < now and {p[0] for p in found} != set(STRATEGIES):
        newest = min(now, oldest + timedelta(days=1))
        for symbol in SYMBOLS:
            orders = await api.get_order_history(
                symbol, int(oldest.timestamp() * 1000), int(newest.timestamp() * 1000), limit=1000,
            )
            if len(orders) >= 1000:
                raise RuntimeError(f"history may be truncated for {symbol} in {oldest}")
            for order in orders:
                cid = order["clientOrderId"]
                order_id = str(order["orderId"])
                if order.get("reduceOnly") in (True, "true") or Decimal(order["executedQty"]) <= 0:
                    continue
                if re.fullmatch(r"mt1[tr][a-f0-9]{32}", cid):
                    strategy = STRATEGIES[0]
                    intent = "trend" if cid[3] == "t" else "mean_reversion"
                elif (symbol, cid) in reservations:
                    strategy = STRATEGIES[1]
                    intent = reservations[symbol, cid].get("entry_intent")
                elif len(owners := recorded_owners.get((symbol, order_id), set())) == 1:
                    strategy = next(iter(owners))
                    intent = None
                else:
                    continue
                if int(order["time"]) < int(START_BY_STRATEGY[strategy].timestamp() * 1000):
                    continue
                trades = await api.get_user_trades(symbol, order_id=order_id, limit=1000)
                matching = [r for r in trades if str(r["orderId"]) == order_id and r["symbol"] == symbol]
                if not matching or len(matching) >= 1000:
                    raise RuntimeError(f"missing or truncated signed fills for {symbol} order {order_id}")
                found.append((strategy, symbol, order_id, cid, intent, order, matching))
        oldest = newest + timedelta(milliseconds=1)

    for strategy, symbol, order_id, cid, intent, order, trades in sorted(
            found, key=lambda row: min(int(t["time"]) for t in row[-1])):
        if result[strategy] is not None:
            continue
        keys = [str(trade["id"]) for trade in trades]
        async with db.transaction(readonly=True):
            signals = await db.fetch(
                f"SELECT id,status,direction,metadata->>'entry_intent' AS entry_intent,"
                f"metadata->>'entry_order_id' AS order_id,metadata->>'entry_client_order_id' AS client_id "
                f"FROM {strategy}.trade_signals WHERE symbol=$1 AND "
                "(metadata->>'entry_order_id'=$2 OR metadata->>'entry_client_order_id'=$3) ORDER BY id",
                symbol, order_id, cid,
            )
            owners = await db.fetch(
                "SELECT DISTINCT strategy,source_kind FROM trading.order_strategy_evidence "
                "WHERE account_id=$1 AND symbol=$2 AND order_id::text=$3",
                ACCOUNT, symbol, order_id,
            )
            fill_ids = await db.fetch(
                "SELECT trade_id::text FROM trading.execution_fact_fill "
                "WHERE account_id=$1 AND symbol=$2 AND order_id::text=$3", ACCOUNT, symbol, order_id,
            )
        observed = {row["trade_id"] for row in fill_ids}
        owner_values = {row["strategy"] for row in owners}
        side = "LONG" if order["side"] == "BUY" else "SHORT"
        signal_matches = len(signals) == 1 and all(
            signals[0][key] == exp for key, exp in
            (("entry_intent", intent), ("direction", side), ("order_id", order_id), ("client_id", cid))
        )
        signed_qty = sum(Decimal(trade["qty"]) for trade in trades)
        quantity_matches = signed_qty == Decimal(order["executedQty"])
        checks = {
            "intent": intent in {"trend", "mean_reversion"},
            "signal_matches_order": signal_matches,
            "signed_fill_quantity_matches": quantity_matches,
            "fact_ledger_has_all_signed_fills": set(keys) == observed,
            "exclusive_owner": owner_values == {strategy},
        }
        result[strategy] = {
            "state": "verified" if all(checks.values()) else "needs_evidence",
            "filled_at_utc": datetime.fromtimestamp(min(int(t["time"]) for t in trades) / 1000, UTC),
            "symbol": symbol, "direction": side, "intent": intent, "order_id": order_id,
            "client_order_id": cid, "signed_fill_count": len(keys), "filled_qty": str(signed_qty),
            "order_status": order["status"], "signals": [dict(row) for row in signals],
            "owners": [dict(row) for row in owners], "checks": checks,
        }
    return {"checked_at_utc": now, "start": START, "strategies": result}


async def main():
    if os.getenv("TRADE_ACCOUNT_ID", ACCOUNT) != ACCOUNT:
        raise RuntimeError("account mismatch")
    db = await asyncpg.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
                               database=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                               password=os.environ["DB_PASSWORD"])
    api = BinanceClient(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"],
                        use_unified_account=True)
    try:
        print(json.dumps(await inspect_first(db, api), default=str, ensure_ascii=False))
    finally:
        await api.close()
        await db.close()


asyncio.run(main())
