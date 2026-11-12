"""Read-only PM grid fill reporting with an independent durable delivery ledger."""

import asyncio
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time

from .contracts import ExecutionSettings
from .exchange import GridExchange
from .notification_report import (
    PositionSnapshot, TradeFact, render_report, summarize,
)
from strategies.grid.realtime.delivery import WebhookSingleSender


logger = logging.getLogger(__name__)
DAY_MS = 24 * 60 * 60 * 1000


def healthcheck(path, now_ms=None) -> int:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        data = json.loads(Path(path).read_text())
        age = now_ms - int(data["updated_at_ms"])
        return 0 if data.get("status") == "ok" and 0 <= age <= 120_000 else 1
    except (OSError, ValueError, KeyError, TypeError):
        return 1


def write_heartbeat(path, status):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"updated_at_ms": int(time.time() * 1000),
                                     "status": status}))
    os.replace(temporary, path)


def number(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid " + label) from exc
    if not result.is_finite():
        raise ValueError("invalid " + label)
    return result


class NotificationWorker:
    def __init__(self, execution_db, notification_db, exchange, sender):
        execution_db = Path(execution_db).resolve()
        notification_db = Path(notification_db)
        notification_db.parent.mkdir(parents=True, exist_ok=True)
        self.execution = sqlite3.connect(execution_db.as_uri() + "?mode=ro", uri=True)
        self.execution.row_factory = sqlite3.Row
        self.notices = sqlite3.connect(notification_db)
        self.notices.row_factory = sqlite3.Row
        self.notices.execute("PRAGMA journal_mode=WAL")
        self.notices.execute("PRAGMA synchronous=FULL")
        self.notices.execute("""CREATE TABLE IF NOT EXISTS fact (
            order_id TEXT NOT NULL, trade_id TEXT NOT NULL,
            epoch_id INTEGER NOT NULL, side TEXT NOT NULL,
            quantity TEXT NOT NULL, price TEXT NOT NULL,
            realized_pnl TEXT NOT NULL, commission TEXT NOT NULL,
            commission_asset TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
            sent_at_ms INTEGER, next_attempt_ms INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(order_id,trade_id))""")
        self.exchange = exchange
        self.sender = sender

    def close(self):
        self.execution.close()
        self.notices.close()

    def _owned_fills(self):
        return self.execution.execute("""SELECT f.exchange_order_id, f.trade_id,
            f.quantity, f.price, f.created_at_ms, o.epoch_id, o.side,
            o.created_at_ms AS order_created_at_ms,
            o.exchange_order_id AS intent_order_id
            FROM fill f JOIN order_intent o USING(client_order_id)
            ORDER BY f.created_at_ms,f.rowid""").fetchall()

    def _epochs(self):
        return self.execution.execute(
            "SELECT id,created_at_ms FROM grid_epoch ORDER BY id").fetchall()

    def _facts(self):
        rows = self.notices.execute("SELECT * FROM fact ORDER BY created_at_ms,order_id,trade_id")
        return tuple(TradeFact(int(row["epoch_id"]), row["order_id"], row["trade_id"],
                               row["side"], number(row["quantity"], "quantity"),
                               number(row["price"], "price"),
                               number(row["realized_pnl"], "realized pnl"),
                               number(row["commission"], "commission"),
                               row["commission_asset"]) for row in rows)

    async def _sync_facts(self, now_ms):
        complete = True
        for row in self._owned_fills():
            order_id, trade_id = row["exchange_order_id"], row["trade_id"]
            if order_id != row["intent_order_id"]:
                raise ValueError("journal fill order ID mismatch")
            if self.notices.execute("SELECT 1 FROM fact WHERE order_id=? AND trade_id=?",
                                    (order_id, trade_id)).fetchone():
                continue
            trades = await self.exchange.order_trades(
                int(order_id), int(row["order_created_at_ms"]), now_ms)
            matches = [trade for trade in trades if
                       str(trade.get("orderId")) == order_id and
                       str(trade.get("id")) == trade_id and
                       trade.get("symbol") == "ETHUSDT" and
                       trade.get("side") == row["side"]]
            if len(matches) != 1:
                complete = False
                continue
            trade = matches[0]
            if (number(trade.get("qty"), "PM quantity") != number(row["quantity"], "journal quantity")
                    or number(trade.get("price"), "PM price") != number(row["price"], "journal price")
                    or trade.get("commissionAsset") != "USDT"):
                raise ValueError("PM fill or commission asset mismatches journal")
            realized = number(trade.get("realizedPnl"), "realized pnl")
            commission = number(trade.get("commission"), "commission")
            if commission < 0:
                raise ValueError("negative PM commission")
            with self.notices:
                self.notices.execute("""INSERT OR IGNORE INTO fact
                    (order_id,trade_id,epoch_id,side,quantity,price,realized_pnl,
                     commission,commission_asset,created_at_ms)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (order_id, trade_id, row["epoch_id"], row["side"],
                     row["quantity"], row["price"], str(realized),
                     str(commission), "USDT", row["created_at_ms"]))
        return complete

    async def _funding(self, start_ms, end_ms):
        total = Decimal(0)
        cursor = int(start_ms)
        while cursor <= end_ms:
            stop = min(end_ms, cursor + DAY_MS - 1)
            rows = await self.exchange.income(cursor, stop)
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("time"), int)
                        or not cursor <= row["time"] <= stop):
                    raise ValueError("incomplete PM income evidence")
                if row.get("incomeType") == "FUNDING_FEE":
                    if row.get("symbol") != "ETHUSDT" or row.get("asset") != "USDT":
                        raise ValueError("foreign funding in dedicated grid account")
                    total += number(row.get("income"), "funding")
            cursor = stop + 1
        return total

    async def _position(self):
        rows = await self.exchange.position()
        if len(rows) != 1 or rows[0].get("positionSide") != "BOTH":
            raise ValueError("PM net position unavailable")
        row = rows[0]
        amount = number(row.get("positionAmt"), "position amount")
        return PositionSnapshot(
            amount, number(row.get("entryPrice", "0"), "entry price"),
            number(row.get("markPrice", "0"), "mark price"),
            number(row.get("unRealizedProfit", "0"), "unrealized pnl"))

    async def run_once(self, now_ms=None) -> int:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if not await self._sync_facts(now_ms):
            return 0
        epochs = self._epochs()
        if not epochs:
            return 0
        pending = self.notices.execute("""SELECT order_id,trade_id,next_attempt_ms FROM fact
            WHERE sent_at_ms IS NULL
            ORDER BY created_at_ms,order_id,trade_id""").fetchall()
        delivered = 0
        for row in pending:
            if row["next_attempt_ms"] > now_ms:
                break
            facts = self._facts()
            fact = next(f for f in facts if f.order_id == row["order_id"]
                        and f.trade_id == row["trade_id"])
            current = epochs[-1]
            position = await self._position()
            cumulative_funding = await self._funding(epochs[0]["created_at_ms"], now_ms)
            phase_funding = await self._funding(current["created_at_ms"], now_ms)
            totals = summarize(facts, current["id"], position,
                               phase_funding, cumulative_funding)
            outcome = await self.sender(render_report(fact, position, totals))
            with self.notices:
                if outcome is True:
                    self.notices.execute("""UPDATE fact SET sent_at_ms=?
                        WHERE order_id=? AND trade_id=? AND sent_at_ms IS NULL""",
                        (now_ms, fact.order_id, fact.trade_id))
                    delivered += 1
                else:
                    self.notices.execute("""UPDATE fact SET next_attempt_ms=?
                        WHERE order_id=? AND trade_id=? AND sent_at_ms IS NULL""",
                        (now_ms + 30_000, fact.order_id, fact.trade_id))
                    break
        return delivered


async def main():
    env = dict(os.environ, GRID_EXECUTION_MODE="live")
    settings = ExecutionSettings.from_env(env)
    if not env.get("FEISHU_WEBHOOK_GRID"):
        raise ValueError("FEISHU_WEBHOOK_GRID required")
    exchange = GridExchange.from_settings(settings)
    worker = NotificationWorker(
        env.get("GRID_EXECUTION_DB", "/app/data/grid_execution.sqlite3"),
        env.get("GRID_NOTIFICATION_DB", "/app/data/grid_notifications.sqlite3"),
        exchange, WebhookSingleSender())
    heartbeat = env.get("GRID_NOTIFICATION_HEARTBEAT",
                        "/app/data/grid_notification_heartbeat.json")
    try:
        while True:
            try:
                await worker.run_once()
                write_heartbeat(heartbeat, "ok")
            except Exception:
                write_heartbeat(heartbeat, "error")
                logger.exception("grid notification cycle failed")
            await asyncio.sleep(5)
    finally:
        worker.close()
        await exchange.close()


if __name__ == "__main__":
    if "--healthcheck" in sys.argv:
        raise SystemExit(healthcheck(os.getenv(
            "GRID_NOTIFICATION_HEARTBEAT", "/app/data/grid_notification_heartbeat.json")))
    asyncio.run(main())
