"""Read the grid journal without altering execution, then project it to PG.

The SQLite execution ledger remains the order authority. PostgreSQL writes are
idempotent and can be replayed after a database outage or process restart.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time

import asyncpg

from .observer_journal import notice_identity, notice_path, verify_execution_identity
from .exchange import GridExchange


LOG = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
STRATEGY = "grid"
SYMBOL = "ETHUSDT"


def _read(path, queries):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        return tuple([dict(row) for row in conn.execute(query)] for query in queries)
    finally:
        conn.close()


def read_snapshot(path):
    """An atomic read snapshot of owned ordinary orders, stops and fills."""
    return _read(path, (
        "SELECT * FROM order_intent ORDER BY created_at_ms,client_order_id",
        "SELECT * FROM protection ORDER BY epoch_id,client_algo_id",
        "SELECT * FROM fill ORDER BY created_at_ms,exchange_order_id,trade_id",
    ))


def _beijing(ms):
    return datetime.fromtimestamp(int(ms) / 1000, BEIJING).replace(tzinfo=None)


def _utc(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc)


def _positive_id(value, name):
    try:
        result = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid " + name) from exc
    if result <= 0:
        raise ValueError("invalid " + name)
    return result


def _decimal(value, name):
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError("invalid " + name) from exc
    if not result.is_finite():
        raise ValueError("invalid " + name)
    return result


ORDER_UPSERT = """INSERT INTO trading.trade_records
    (account_id,strategy,symbol,client_order_id,grid_epoch_id,order_id,algo_id,
     side,order_type,quantity,price,commission,status,executed_at,realized_pnl,
     pnl_source,close_reason)
    VALUES ($1,'grid','ETHUSDT',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
    ON CONFLICT (account_id,strategy,client_order_id) WHERE client_order_id IS NOT NULL
    DO UPDATE SET order_id=EXCLUDED.order_id,algo_id=EXCLUDED.algo_id,
      quantity=EXCLUDED.quantity,price=EXCLUDED.price,
      commission=EXCLUDED.commission,status=EXCLUDED.status,
      executed_at=EXCLUDED.executed_at,realized_pnl=EXCLUDED.realized_pnl,
      pnl_source=EXCLUDED.pnl_source,close_reason=EXCLUDED.close_reason
    WHERE trading.trade_records.grid_epoch_id=EXCLUDED.grid_epoch_id
      AND trading.trade_records.side=EXCLUDED.side
      AND trading.trade_records.order_type=EXCLUDED.order_type"""

FILL_UPSERT = """INSERT INTO trading.execution_fact_fill
    (account_id,symbol,trade_id,order_id,side,qty,price,quote_qty,
     commission,commission_asset,realized_pnl,trade_time_utc)
    VALUES ($1,'ETHUSDT',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
    ON CONFLICT (account_id,symbol,trade_id) DO UPDATE SET
      order_id=EXCLUDED.order_id,side=EXCLUDED.side,qty=EXCLUDED.qty,
      price=EXCLUDED.price,quote_qty=EXCLUDED.quote_qty,
      commission=EXCLUDED.commission,commission_asset=EXCLUDED.commission_asset,
      realized_pnl=EXCLUDED.realized_pnl,trade_time_utc=EXCLUDED.trade_time_utc"""


class GridPgProjector:
    def __init__(self, journal_path, notice_path, account_id, exchange, connection):
        if not account_id or account_id in ("default", "main"):
            raise ValueError("dedicated GRID_ACCOUNT_ID required")
        self.journal_path = journal_path
        self.notice_path = notice_path
        classic = getattr(exchange, "pm", True) is False
        self.account_id = account_id + ":classic" if classic else account_id
        self.trade_source = "FAPI_USER_TRADES" if classic else "PM_USER_TRADES"
        self.exchange = exchange
        self.connection = connection
        self._projected = {}
        self._full_replay_at = 0

    async def _triggered_stops(self, stops, completed):
        """A triggered PM algo order has a separate ordinary orderId."""
        result = {}
        for stop in stops:
            if stop["status"] == "CANCELED" or stop["client_algo_id"] in completed:
                continue
            response = await self.exchange.query_stop(stop["client_algo_id"])
            if not isinstance(response, dict) or response.get("clientAlgoId") != stop["client_algo_id"]:
                raise ValueError("PM stop ownership cannot be confirmed")
            if response.get("algoStatus") in ("TRIGGERED", "FINISHED"):
                order_id = _positive_id(response.get("actualOrderId"), "triggered orderId")
                order = await self.exchange.query_order_by_id(order_id)
                if (not isinstance(order, dict) or order.get("symbol") != SYMBOL
                        or int(order.get("orderId", 0)) != order_id
                        or order.get("side") != stop["side"]):
                    raise ValueError("triggered PM order ownership mismatch")
                trades = await self.exchange.order_trades(order_id,
                    self._stop_epoch_time(stop["epoch_id"]), int(time.time() * 1000))
                if not isinstance(trades, list):
                    raise ValueError("triggered PM trades unavailable")
                for trade in trades:
                    if (trade.get("symbol") != SYMBOL or trade.get("side") != stop["side"]
                            or int(trade.get("orderId", 0)) != order_id
                            or trade.get("commissionAsset") != "USDT"):
                        raise ValueError("triggered PM trade ownership mismatch")
                if sum((_decimal(t["qty"], "quantity") for t in trades), Decimal(0)) != _decimal(
                        order.get("executedQty", "0"), "triggered quantity"):
                    raise ValueError("triggered PM trade set incomplete")
                result[stop["client_algo_id"]] = (order, trades)
        return result

    async def run_once(self):
        if time.monotonic() - self._full_replay_at >= 3600:
            self._projected.clear()
            self._full_replay_at = time.monotonic()
        orders, stops, journal_fills = read_snapshot(self.journal_path)
        complete_rows = await self.connection.fetch("""SELECT client_order_id
            FROM trading.trade_records WHERE account_id=$1 AND strategy='grid'
            AND order_type='CONDITIONAL_ORDER' AND order_id IS NOT NULL
            AND status IN ('FILLED','CANCELED','EXPIRED')""", self.account_id)
        completed = {row["client_order_id"] for row in complete_rows}
        notice_rows = _read(self.notice_path, (
            "SELECT * FROM fact ORDER BY created_at_ms,order_id,trade_id",))[0]
        facts = {(row["order_id"], row["trade_id"]): row for row in notice_rows}
        own_ids = {row["client_order_id"]: row for row in orders}
        if any(not row["client_order_id"].startswith("eg-") for row in orders):
            raise ValueError("non-grid order in execution journal")
        if any(not row["client_algo_id"].startswith("eg-s-") for row in stops):
            raise ValueError("non-grid stop in execution journal")
        verified = {}
        for fill in journal_fills:
            order = own_ids.get(fill["client_order_id"])
            if order is None or fill["exchange_order_id"] != order["exchange_order_id"]:
                raise ValueError("journal fill ownership mismatch")
            fact = facts.get((fill["exchange_order_id"], fill["trade_id"]))
            if fact is None:
                continue  # notifier's PM verification has not completed yet
            if (_decimal(fact["quantity"], "fact quantity") != _decimal(fill["quantity"], "fill quantity")
                    or _decimal(fact["price"], "fact price") != _decimal(fill["price"], "fill price")
                    or fact["side"] != order["side"] or fact["commission_asset"] != "USDT"):
                raise ValueError("verified PM fact conflicts with journal")
            verified[(fill["exchange_order_id"], fill["trade_id"])] = (fill, fact)
        triggered = await self._triggered_stops(stops, completed)
        by_order = {}
        for (order_id, _), (_, fact) in verified.items():
            by_order.setdefault(order_id, []).append(fact)
        projected = dict(self._projected)
        async def write(sql, *args):
            key = (sql, args[0], args[1])
            if projected.get(key) == args:
                return "UPDATE 1"
            result = await self.connection.execute(sql, *args)
            projected[key] = args
            return result
        async with self.connection.transaction():
            count = 0
            for order in orders:
                if order["role"] == "STOP":
                    continue  # Actual child belongs to the protection order row.
                order_id = order["exchange_order_id"] or None
                own_facts = by_order.get(order_id, [])
                qty = sum((_decimal(f["quantity"], "quantity") for f in own_facts), Decimal(0))
                expected_qty = _decimal(order["executed_qty"], "executed quantity")
                if qty > expected_qty:
                    raise ValueError("PM facts exceed journal execution")
                price = (sum((_decimal(f["quantity"], "quantity") * _decimal(f["price"], "price")
                              for f in own_facts), Decimal(0)) / qty) if qty else Decimal(0)
                complete = qty == expected_qty
                status = order["status"] if complete else "PENDING_FACTS"
                command = await write(ORDER_UPSERT,
                    self.account_id, order["client_order_id"], order["epoch_id"],
                    order_id, None, order["side"],
                    "MARKET" if order["role"] == "EXIT" else "LIMIT",
                    qty, price, sum((_decimal(f["commission"], "commission") for f in own_facts), Decimal(0)),
                    status, _beijing(max((f.get("trade_time_ms") or f["created_at_ms"]
                                           for f in own_facts), default=order["created_at_ms"])),
                    sum((_decimal(f["realized_pnl"], "realized pnl") for f in own_facts), Decimal(0))
                    if own_facts else None, self.trade_source if own_facts else None,
                    None)
                if command not in ("INSERT 0 1", "UPDATE 1"):
                    raise ValueError("grid order attribution collision")
                count += 1
            for stop in stops:
                if stop["client_algo_id"] in completed:
                    continue
                child_data = triggered.get(stop["client_algo_id"])
                child, trades = child_data if child_data else (None, ())
                status = child["status"] if child else stop["status"]
                child_id = str(child["orderId"]) if child else None
                qty = sum((_decimal(t["qty"], "quantity") for t in trades), Decimal(0))
                price = (sum((_decimal(t["qty"], "quantity") * _decimal(t["price"], "price")
                              for t in trades), Decimal(0)) / qty) if qty else Decimal(0)
                command = await write(ORDER_UPSERT,
                    self.account_id, stop["client_algo_id"], stop["epoch_id"],
                    child_id, stop["exchange_algo_id"], stop["side"],
                    "CONDITIONAL_ORDER", qty, price,
                    sum((_decimal(t["commission"], "commission") for t in trades), Decimal(0)),
                    status, _beijing(child.get("time", int(time.time() * 1000))) if child else _beijing(
                        self._stop_epoch_time(stop["epoch_id"])),
                    sum((_decimal(t["realizedPnl"], "realized pnl") for t in trades), Decimal(0))
                    if trades else None, self.trade_source if trades else None,
                    "STOP_LOSS" if trades else None)
                if command not in ("INSERT 0 1", "UPDATE 1"):
                    raise ValueError("grid stop attribution collision")
                count += 1
                for trade in trades:
                    trade_qty = _decimal(trade["qty"], "quantity")
                    trade_price = _decimal(trade["price"], "price")
                    await write(FILL_UPSERT,
                        self.account_id, _positive_id(trade["id"], "tradeId"),
                        _positive_id(trade["orderId"], "orderId"), stop["side"],
                        trade_qty, trade_price, trade_qty * trade_price,
                        _decimal(trade["commission"], "commission"), "USDT",
                        _decimal(trade["realizedPnl"], "realized pnl"),
                        _utc(trade["time"]))
            for fill, fact in verified.values():
                trade_time = fact.get("trade_time_ms") or fill["created_at_ms"]
                await write(FILL_UPSERT,
                    self.account_id, _positive_id(fill["trade_id"], "tradeId"),
                    _positive_id(fill["exchange_order_id"], "orderId"),
                    own_ids[fill["client_order_id"]]["side"],
                    _decimal(fact["quantity"], "quantity"), _decimal(fact["price"], "price"),
                    _decimal(fact["quantity"], "quantity") * _decimal(fact["price"], "price"),
                    _decimal(fact["commission"], "commission"), "USDT",
                    _decimal(fact["realized_pnl"], "realized pnl"), _utc(trade_time))
        actual = await self.connection.fetchval("""SELECT count(*)
            FROM trading.trade_records WHERE account_id=$1 AND strategy='grid'
            AND client_order_id IS NOT NULL""", self.account_id)
        if actual != sum(o["role"] != "STOP" for o in orders) + len(stops):
            self._projected.clear()
            raise ValueError("PostgreSQL grid order count differs from journal")
        self._projected = projected
        return count, len(verified) + sum(len(trades) for _, trades in triggered.values())

    def _stop_epoch_time(self, epoch_id):
        return _read(self.journal_path, (
            "SELECT created_at_ms FROM grid_epoch WHERE id=" + str(int(epoch_id)),))[0][0]["created_at_ms"]


async def main():
    env = dict(os.environ, GRID_EXECUTION_MODE="live")
    required = ("GRID_ACCOUNT_ID", "DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
    if any(not env.get(key) for key in required):
        raise ValueError("grid PG projection configuration incomplete")
    exchange = GridExchange.readonly_from_env(env)
    execution_path = env.get("GRID_EXECUTION_DB", "/app/data/grid_execution.sqlite3")
    notices = notice_path(env, exchange.pm)
    identity = await exchange.journal_identity()
    verify_execution_identity(execution_path, identity)
    conn = sqlite3.connect(Path(notices).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if (notice_identity(conn) or "pm") != identity:
            raise ValueError("grid projector and notification journal account mismatch")
    finally:
        conn.close()
    pool = await asyncpg.create_pool(host=env["DATABASE_HOST"],
        port=int(env.get("DATABASE_PORT", "5432")), database=env["DATABASE_NAME"],
        user=env["DATABASE_USER"], password=env["DATABASE_PASSWORD"],
        min_size=1, max_size=1, command_timeout=30)
    projector = GridPgProjector(execution_path, notices,
        env["GRID_ACCOUNT_ID"], exchange, None)
    heartbeat = Path(env.get("GRID_PG_HEARTBEAT", "/app/data/grid_pg_heartbeat.json"))
    try:
        while True:
            try:
                async with pool.acquire() as connection:
                    projector.connection = connection
                    orders, fills = await projector.run_once()
                _heartbeat(heartbeat, "ok", orders, fills)
            except Exception:
                _heartbeat(heartbeat, "error", 0, 0)
                LOG.exception("grid PostgreSQL projection failed")
            await asyncio.sleep(15)
    finally:
        await pool.close()
        await exchange.close()


def _heartbeat(path, status, orders, fills):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"status": status, "updated_at_ms": int(time.time() * 1000),
                               "orders_seen": orders, "fills_seen": fills}))
    os.replace(tmp, path)


def healthcheck(path):
    try:
        state = json.loads(Path(path).read_text())
        return 0 if (state["status"] == "ok" and
                     0 <= int(time.time() * 1000) - state["updated_at_ms"] <= 120_000) else 1
    except (OSError, KeyError, TypeError, ValueError):
        return 1


if __name__ == "__main__":
    if "--healthcheck" in sys.argv:
        raise SystemExit(healthcheck(os.getenv(
            "GRID_PG_HEARTBEAT", "/app/data/grid_pg_heartbeat.json")))
    asyncio.run(main())
