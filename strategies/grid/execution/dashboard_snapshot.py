"""Read-only, account-isolated snapshot of the live ETHUSDT order grid."""

import asyncio
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .contracts import ExecutionSettings
from .exchange import GridExchange

DAY_MS = 86_400_000


def _decimal(value, label):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid " + label) from exc
    if not result.is_finite():
        raise ValueError("invalid " + label)
    return result


def calculate_snapshot(*, epochs, fills, facts, funding, position, engine,
                       open_orders, now_ms):
    """Match every executor fill to PM evidence before showing any PnL."""
    if not isinstance(now_ms, int) or now_ms <= 0 or position.get("positionSide") != "BOTH":
        raise ValueError("invalid PM position")
    if not isinstance(open_orders, int) or open_orders < 0:
        raise ValueError("invalid open orders")
    epoch_ids = {int(row["id"]) for row in epochs}
    if len(epoch_ids) != len(epochs):
        raise ValueError("duplicate grid epoch")
    fact_map = {(str(row["order_id"]), str(row["trade_id"])): row for row in facts}
    fill_keys = {(str(row["exchange_order_id"]), str(row["trade_id"])) for row in fills}
    if len(fact_map) != len(facts) or len(fill_keys) != len(fills) or set(fact_map) != fill_keys:
        raise ValueError("unmatched grid PM fill evidence")
    for row in fills:
        fact = fact_map[(str(row["exchange_order_id"]), str(row["trade_id"]))]
        if (int(row["epoch_id"]) not in epoch_ids or int(row["epoch_id"]) != int(fact["epoch_id"])
                or row["side"] != fact["side"] or row["side"] not in ("BUY", "SELL")
                or _decimal(row["quantity"], "fill quantity") != _decimal(fact["quantity"], "PM quantity")
                or _decimal(row["price"], "fill price") != _decimal(fact["price"], "PM price")):
            raise ValueError("unmatched grid PM fill evidence")
    realized = Decimal(0)
    stage_realized = Decimal(0)
    reconstructed_qty = Decimal(0)
    latest_id = int(epochs[-1]["id"]) if epochs else None
    for fact in facts:
        if fact["commission_asset"] != "USDT":
            raise ValueError("invalid PM commission asset")
        fee = _decimal(fact["commission"], "commission")
        qty = _decimal(fact["quantity"], "quantity")
        if fee < 0 or qty <= 0:
            raise ValueError("invalid PM commission or quantity")
        net = _decimal(fact["realized_pnl"], "realized pnl") - fee
        realized += net
        if int(fact["epoch_id"]) == latest_id:
            stage_realized += net
        reconstructed_qty += qty if fact["side"] == "BUY" else -qty
    net_qty = _decimal(position.get("positionAmt"), "position amount")
    if reconstructed_qty != net_qty:
        raise ValueError("PM position differs from executor fills")
    unrealized = _decimal(position.get("unRealizedProfit", "0"), "unrealized pnl")
    entry = _decimal(position.get("entryPrice", "0"), "entry price")
    mark = _decimal(position.get("markPrice", "0"), "mark price")
    total_funding = Decimal(0)
    stage_funding = Decimal(0)
    seen_funding = set()
    for row in funding:
        if (row.get("incomeType") != "FUNDING_FEE" or row.get("symbol") != "ETHUSDT"
                or row.get("asset") != "USDT" or not isinstance(row.get("time"), int)
                or not epochs or not int(epochs[0]["created_at_ms"]) <= row["time"] <= now_ms):
            raise ValueError("invalid grid funding evidence")
        key = str(row.get("tranId"))
        if key == "None" or key in seen_funding:
            raise ValueError("duplicate or missing funding ID")
        seen_funding.add(key)
        amount = _decimal(row.get("income"), "funding")
        total_funding += amount
        if row["time"] >= int(epochs[-1]["created_at_ms"]):
            stage_funding += amount
    if not epochs and (facts or funding or net_qty != 0 or open_orders):
        raise ValueError("grid position or orders without epoch")
    recent = max(facts, key=lambda row: (int(row.get("created_at_ms", 0)),
                                          str(row["trade_id"]))) if facts else None
    last_trade = ({"trade_id": str(recent["trade_id"]), "side": recent["side"],
                   "quantity": str(_decimal(recent["quantity"], "last quantity")),
                   "price": str(_decimal(recent["price"], "last price")),
                   "time_ms": int(recent.get("created_at_ms", 0))} if recent else None)
    return {
        "version": 1, "account_scope": "grid_subaccount_pm", "symbol": "ETHUSDT",
        "as_of_ms": now_ms, "engine_state": str(engine.get("state", "UNKNOWN")),
        "risk_status": str(engine.get("risk_status", "UNKNOWN")),
        "stream_ready": engine.get("stream_ready") is True,
        "active_epoch_id": latest_id if epochs and epochs[-1].get("status") == "ACTIVE" else None,
        "phase_epoch_id": latest_id, "fill_count": len(facts),
        "last_trade": last_trade,
        "order_count": len({str(row["order_id"]) for row in facts}),
        "open_order_count": open_orders, "net_qty": str(net_qty),
        "entry_price": str(entry), "mark_price": str(mark),
        "unrealized_pnl_usdt": str(unrealized),
        "realized_net_usdt": str(realized), "funding_usdt": str(total_funding),
        "stage_pnl_usdt": str(stage_realized + stage_funding + unrealized),
        "cumulative_pnl_usdt": str(realized + total_funding + unrealized),
    }


def _read_sqlite(path, query):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(query)]
    finally:
        conn.close()


async def _funding(exchange, start_ms, end_ms):
    rows = []
    cursor = start_ms
    while cursor <= end_ms:
        stop = min(end_ms, cursor + DAY_MS - 1)
        page = await exchange.income(cursor, stop)
        if any(not isinstance(row, dict) or not isinstance(row.get("time"), int)
               or not cursor <= row["time"] <= stop for row in page):
            raise ValueError("incomplete PM income evidence")
        for row in page:
            if row.get("incomeType") != "FUNDING_FEE":
                continue
            if (row.get("symbol") != "ETHUSDT" or row.get("asset") != "USDT"
                    or row.get("tranId") is None):
                raise ValueError("invalid PM funding event")
            _decimal(row.get("income"), "funding")
            rows.append(row)
        cursor = stop + 1
    return rows


async def cached_funding(exchange, cache_path, epoch_start_ms, now_ms):
    """Keep verified PM funding events, refreshing the most recent hour."""
    cache_path = Path(cache_path)
    if cache_path.exists():
        state = json.loads(cache_path.read_text())
        if (state.get("version") != 1 or state.get("epoch_start_ms") != epoch_start_ms
                or not isinstance(state.get("covered_until_ms"), int)
                or state["covered_until_ms"] < epoch_start_ms - 1
                or not isinstance(state.get("events"), list)):
            raise ValueError("invalid grid funding cache")
    else:
        state = {"version": 1, "epoch_start_ms": epoch_start_ms,
                 "covered_until_ms": epoch_start_ms - 1, "events": []}
    cursor = state["covered_until_ms"]
    if cursor > now_ms:
        raise ValueError("grid funding cursor moved backwards")
    refresh_from = max(epoch_start_ms, cursor - 3_600_000 + 1)
    fresh = await _funding(exchange, refresh_from, now_ms)
    events = {str(row["tranId"]): row for row in state["events"]}
    if len(events) != len(state["events"]):
        raise ValueError("duplicate cached funding ID")
    for row in fresh:
        key = str(row.get("tranId"))
        if key == "None" or (key in events and events[key] != row):
            raise ValueError("grid funding conflict")
        events[key] = row
    result = sorted(events.values(), key=lambda row: (row["time"], str(row["tranId"])))
    write_snapshot(cache_path, {"version": 1, "epoch_start_ms": epoch_start_ms,
                                "covered_until_ms": now_ms, "events": result})
    return result


async def collect(execution_db, notification_db, heartbeat_path, exchange, now_ms,
                  funding_cache_path=None):
    epochs = _read_sqlite(execution_db,
                          "SELECT id,created_at_ms,status FROM grid_epoch ORDER BY id")
    fills = _read_sqlite(execution_db,
                         "SELECT f.exchange_order_id,f.trade_id,f.quantity,f.price,"
                         "o.epoch_id,o.side FROM fill f JOIN order_intent o "
                         "USING(client_order_id) ORDER BY f.created_at_ms,f.rowid")
    facts = _read_sqlite(notification_db, "SELECT * FROM fact ORDER BY created_at_ms,order_id,trade_id")
    engine = json.loads(Path(heartbeat_path).read_text())
    if now_ms - int(engine["updated_at_ms"]) > 120_000:
        raise ValueError("executor heartbeat stale")
    positions = await exchange.position()
    if len(positions) != 1:
        raise ValueError("PM position unavailable")
    orders = await exchange.open_orders()
    funding = (await cached_funding(exchange, funding_cache_path,
                                    int(epochs[0]["created_at_ms"]), now_ms)
               if epochs and funding_cache_path else
               await _funding(exchange, int(epochs[0]["created_at_ms"]), now_ms)
               if epochs else [])
    return calculate_snapshot(epochs=epochs, fills=fills, facts=facts,
                              funding=funding, position=positions[0], engine=engine,
                              open_orders=len(orders), now_ms=now_ms)


def write_snapshot(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


async def main():
    settings = ExecutionSettings.from_env(dict(os.environ, GRID_EXECUTION_MODE="live"))
    exchange = GridExchange.from_settings(settings)
    try:
        while True:
            try:
                now_ms = int(time.time() * 1000)
                snapshot = await collect(
                    os.getenv("GRID_EXECUTION_DB", "/app/data/grid_execution.sqlite3"),
                    os.getenv("GRID_NOTIFICATION_DB", "/app/data/grid_notifications.sqlite3"),
                    os.getenv("GRID_EXECUTION_HEARTBEAT", "/app/data/grid_execution_heartbeat.json"),
                    exchange, now_ms,
                    os.getenv("GRID_DASHBOARD_FUNDING_CACHE", "/app/output/funding.json"))
                write_snapshot(os.getenv("GRID_DASHBOARD_SNAPSHOT", "/app/output/snapshot.json"), snapshot)
            except Exception as exc:
                print("grid dashboard snapshot unavailable: " + type(exc).__name__, file=sys.stderr,
                      flush=True)
            await asyncio.sleep(15)
    finally:
        await exchange.close()


if __name__ == "__main__":
    if "--healthcheck" in sys.argv:
        try:
            data = json.loads(Path(os.getenv("GRID_DASHBOARD_SNAPSHOT", "/app/output/snapshot.json")).read_text())
            age = int(time.time() * 1000) - int(data["as_of_ms"])
            raise SystemExit(0 if data.get("account_scope") == "grid_subaccount_pm" and 0 <= age <= 120_000 else 1)
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit(1)
    asyncio.run(main())
