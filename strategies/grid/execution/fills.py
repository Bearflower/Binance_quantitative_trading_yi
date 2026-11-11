"""Durable fill obligations and exchange-side protection for the order grid.

This module never infers a fill from a missing open order. The caller must
reconcile user-stream events against REST after gaps, disconnects and restarts.
"""

import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping, Optional, Tuple


TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "EXPIRED"}
KNOWN_ORDER_STATUSES = TERMINAL_ORDER_STATUSES | {"NEW", "PARTIALLY_FILLED"}


@dataclass(frozen=True)
class TradeUpdate:
    client_order_id: str
    exchange_order_id: str
    side: str
    execution_type: str
    order_status: str
    quantity: Decimal
    cumulative_quantity: Decimal
    price: Decimal
    trade_id: str

    @classmethod
    def parse(cls, event: Mapping) -> "TradeUpdate":
        if event.get("e") != "ORDER_TRADE_UPDATE":
            raise ValueError("not an order trade update")
        order = event.get("o")
        if not isinstance(order, Mapping) or order.get("s") != "ETHUSDT":
            raise ValueError("wrong order symbol")
        try:
            client_id = str(order["c"])
            order_id = str(order["i"])
            side = order["S"]
            execution_type = order["x"]
            status = order["X"]
            quantity = Decimal(str(order["l"]))
            cumulative = Decimal(str(order["z"]))
            price = Decimal(str(order["L"]))
            trade_id = str(order["t"])
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError("malformed order update") from exc
        if (not client_id.startswith("eg-") or side not in ("BUY", "SELL")
                or status not in KNOWN_ORDER_STATUSES or not order_id.isdigit()
                or not quantity.is_finite() or not cumulative.is_finite()
                or not price.is_finite() or quantity < 0 or cumulative < 0):
            raise ValueError("invalid order update")
        if execution_type == "TRADE":
            if quantity <= 0 or price <= 0 or not trade_id.isdigit() or int(trade_id) <= 0:
                raise ValueError("invalid trade fact")
        elif quantity != 0:
            raise ValueError("non-trade update carries a fill")
        return cls(client_id, order_id, side, execution_type, status,
                   quantity, cumulative, price, trade_id)


@dataclass(frozen=True)
class PairPreparation:
    status: str
    side: Optional[str] = None
    price: Optional[Decimal] = None
    quantity: Decimal = Decimal(0)
    client_order_id: Optional[str] = None
    blocked_client_order_ids: Tuple[str, ...] = ()


class FillCoordinator:
    def __init__(self, store):
        self.store = store

    def record(self, raw_event: Mapping) -> bool:
        """Record an exact trade before advancing an order's cumulative state."""
        event = TradeUpdate.parse(raw_event)
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            intent = self.store.get_order(event.client_order_id)
            if intent is None or intent["side"] != event.side:
                raise ValueError("trade update has no owned order intent")
            if intent["exchange_order_id"] not in (None, event.exchange_order_id):
                raise ValueError("exchange order ID changed")
            previous = Decimal(intent["executed_qty"])
            if event.cumulative_quantity < previous:
                raise ValueError("cumulative fill regressed")
            if event.cumulative_quantity > Decimal(intent["quantity"]):
                raise ValueError("cumulative fill exceeds intended quantity")
            previous_status = intent["status"]
            if (previous_status in TERMINAL_ORDER_STATUSES
                    and event.order_status in TERMINAL_ORDER_STATUSES
                    and previous_status != event.order_status):
                raise ValueError("conflicting terminal order status")

            inserted = False
            if event.execution_type == "TRADE":
                prior = conn.execute("""SELECT client_order_id,quantity,price
                    FROM fill WHERE exchange_order_id=? AND trade_id=?""",
                    (event.exchange_order_id, event.trade_id)).fetchone()
                if prior:
                    if (prior["client_order_id"] != event.client_order_id
                            or Decimal(prior["quantity"]) != event.quantity
                            or Decimal(prior["price"]) != event.price):
                        raise ValueError("conflicting duplicate trade ID")
                else:
                    inserted = self.store.record_fill(
                        event.client_order_id, event.exchange_order_id,
                        event.trade_id, event.quantity, event.price)

            filled = sum((Decimal(row["quantity"]) for row in self.store.fills(
                event.client_order_id)), Decimal(0))
            if filled > event.cumulative_quantity:
                raise ValueError("trade facts exceed cumulative quantity")
            status = event.order_status
            if (previous_status in TERMINAL_ORDER_STATUSES
                    or previous_status == "PARTIALLY_FILLED" and status == "NEW"):
                status = previous_status
            self.store.record_order(event.client_order_id,
                                    event.exchange_order_id, status,
                                    event.cumulative_quantity)
            conn.execute("COMMIT")
            return inserted
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def prepare_reverse(self, source_client_order_id: str,
                        levels: Tuple[Decimal, ...], step_size: Decimal,
                        min_qty: Decimal, min_notional: Decimal) -> PairPreparation:
        """Allocate filled ETH to one adjacent reverse level, once per free slot.

        A blocked or sub-minimum obligation remains as unallocated fills in the
        journal. The caller may cancel a conflicting order and retry after REST
        confirms its terminal status.
        """
        if step_size <= 0 or min_qty <= 0 or min_notional <= 0:
            raise ValueError("invalid reverse order filters")
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            source = self.store.get_order(source_client_order_id)
            if source is None:
                raise ValueError("unknown source order")
            if source["side"] == "BUY":
                reverse_level, reverse_side = source["level_index"] + 1, "SELL"
            elif source["side"] == "SELL":
                reverse_level, reverse_side = source["level_index"] - 1, "BUY"
            else:
                raise ValueError("invalid source side")
            if not 0 <= reverse_level < len(levels):
                raise ValueError("source order lacks adjacent reverse level")
            reverse_price = Decimal(levels[reverse_level])
            if reverse_price <= 0:
                raise ValueError("invalid reverse price")
            facts = sum((Decimal(row["quantity"]) for row in self.store.fills(
                source_client_order_id)), Decimal(0))
            if facts != Decimal(source["executed_qty"]):
                raise ValueError("missing trade facts; reconcile before pairing")
            allocated = conn.execute("""SELECT quantity FROM pair
                WHERE source_client_order_id=?""", (source_client_order_id,)).fetchall()
            residual = facts - sum((Decimal(row[0]) for row in allocated), Decimal(0))
            if residual < 0:
                raise ValueError("pair allocations exceed fills")
            quantity = (residual / step_size).to_integral_value(rounding=ROUND_FLOOR) * step_size
            if quantity < min_qty or quantity * reverse_price < min_notional:
                conn.execute("COMMIT")
                return PairPreparation("REMAINDER")

            blockers = conn.execute("""SELECT client_order_id FROM order_intent
                WHERE epoch_id=? AND level_index=? AND status IN
                ('PREPARED','SENT','NEW','PARTIALLY_FILLED','UNKNOWN')
                ORDER BY client_order_id""", (source["epoch_id"], reverse_level)).fetchall()
            if blockers:
                conn.execute("COMMIT")
                return PairPreparation("WAITING_LEVEL", reverse_side, reverse_price,
                                       quantity, blocked_client_order_ids=tuple(
                                           row[0] for row in blockers))

            pair_index = conn.execute("""SELECT count(*) FROM pair
                WHERE source_client_order_id=?""", (source_client_order_id,)).fetchone()[0]
            digest = hashlib.sha256((str(source["epoch_id"]) + ":" +
                source_client_order_id + ":" + str(pair_index)).encode()).hexdigest()[:24]
            reverse_id = "eg-r-" + digest
            conn.execute("""INSERT INTO pair
                (epoch_id,source_client_order_id,reverse_client_order_id,quantity,status)
                VALUES(?,?,?,?,'PREPARED')""",
                (source["epoch_id"], source_client_order_id, reverse_id, str(quantity)))
            self.store.prepare_order(source["epoch_id"], reverse_level, "REVERSE",
                                     reverse_side, reverse_price, quantity, reverse_id)
            conn.execute("COMMIT")
            return PairPreparation("READY", reverse_side, reverse_price, quantity,
                                   reverse_id)
        except Exception:
            conn.execute("ROLLBACK")
            raise


class ProtectionCoordinator:
    def __init__(self, store, exchange):
        self.store = store
        self.exchange = exchange

    def _exit(self, reason: str) -> bool:
        if self.store.engine_state()["state"] != "LOCKED":
            self.store.set_engine_state("EXITING", reason)
        return False

    async def ensure(self, epoch_id: int, position: Decimal,
                     stop_loss_low: Decimal, stop_loss_high: Decimal) -> bool:
        """Confirm a close-all stop for the current exchange net position."""
        if position == 0:
            known = {row["client_algo_id"]: row for row in self.store.active_protections()}
            try:
                open_rows = await self.exchange.open_stops()
                if any(row.get("symbol") != "ETHUSDT" or
                       row.get("clientAlgoId") not in known for row in open_rows):
                    return self._exit("foreign stop while flat")
                for row in open_rows:
                    client_id = row["clientAlgoId"]
                    await self.exchange.cancel_stop(client_id)
                    self.store.conn.execute("""UPDATE protection SET status='CANCELED'
                        WHERE client_algo_id=?""", (client_id,))
                if any(row["client_algo_id"] not in
                       {item["clientAlgoId"] for item in open_rows}
                       for row in known.values()):
                    return self._exit("unresolved stop intent while flat")
            except Exception:
                return self._exit("flat stop cleanup uncertain")
            return True
        if stop_loss_low <= 0 or stop_loss_high <= stop_loss_low:
            return self._exit("invalid stop boundaries")
        side = "SELL" if position > 0 else "BUY"
        trigger = stop_loss_low if position > 0 else stop_loss_high
        known = {row["client_algo_id"]: row for row in self.store.active_protections()}
        same_active = [row for row in known.values()
                       if row["epoch_id"] == epoch_id and row["side"] == side
                       and Decimal(row["trigger_price"]) == trigger]
        if len(same_active) > 1:
            return self._exit("duplicate active stop intents")
        if same_active:
            client_id = same_active[0]["client_algo_id"]
        else:
            generation = self.store.conn.execute("""SELECT count(*) FROM protection
                WHERE epoch_id=? AND side=? AND trigger_price=?""",
                (epoch_id, side, str(trigger))).fetchone()[0]
            digest = hashlib.sha256((str(epoch_id) + ":" + side + ":" +
                                     str(trigger) + ":" + str(generation)).encode()).hexdigest()[:24]
            client_id = "eg-s-" + digest
        try:
            open_rows = await self.exchange.open_stops()
        except Exception:
            return self._exit("cannot confirm exchange stop protection")
        if any(row.get("symbol") != "ETHUSDT" or row.get("clientAlgoId") not in known
               for row in open_rows):
            return self._exit("foreign or untracked exchange stop")
        matching = [row for row in open_rows if row.get("clientAlgoId") == client_id
                    and row.get("side") == side
                    and Decimal(str(row.get("triggerPrice", "0"))) == trigger
                    and row.get("algoStatus") == "NEW"]
        if matching:
            self.store.conn.execute("""UPDATE protection SET status='NEW',
                exchange_algo_id=? WHERE client_algo_id=?""",
                (str(matching[0].get("algoId", "")), client_id))
            return True

        existing = self.store.conn.execute("SELECT * FROM protection WHERE client_algo_id=?",
                                           (client_id,)).fetchone()
        if existing:
            # A prepared/unknown POST is not safe to submit twice.
            try:
                found = await self.exchange.query_stop(client_id)
            except Exception:
                return self._exit("stop result unknown")
            if not found or found.get("algoStatus") != "NEW":
                return self._exit("stop result unknown")
            self.store.conn.execute("""UPDATE protection SET status='NEW',
                exchange_algo_id=? WHERE client_algo_id=?""",
                (str(found.get("algoId", "")), client_id))
        else:
            self.store.conn.execute("""INSERT INTO protection
                (client_algo_id,epoch_id,side,trigger_price,status)
                VALUES(?,?,?,?,'PREPARED')""",
                (client_id, epoch_id, side, str(trigger)))
            try:
                result = await self.exchange.place_stop(side, trigger, client_id)
                confirmed = await self.exchange.query_stop(client_id)
            except Exception:
                self.store.conn.execute("UPDATE protection SET status='UNKNOWN' WHERE client_algo_id=?",
                                        (client_id,))
                return self._exit("stop placement failed or result unknown")
            if (not confirmed or confirmed.get("algoStatus") != "NEW"
                    or confirmed.get("clientAlgoId") != client_id
                    or confirmed.get("side") != side
                    or Decimal(str(confirmed.get("triggerPrice", "0"))) != trigger):
                self.store.conn.execute("UPDATE protection SET status='UNKNOWN' WHERE client_algo_id=?",
                                        (client_id,))
                return self._exit("stop placement unconfirmed")
            self.store.conn.execute("""UPDATE protection SET status='NEW',
                exchange_algo_id=? WHERE client_algo_id=?""",
                (str(confirmed.get("algoId", result.get("algoId", ""))), client_id))

        # The replacement is confirmed before retiring stale/old-side stops.
        for old in open_rows:
            old_id = old["clientAlgoId"]
            if old_id == client_id:
                continue
            try:
                await self.exchange.cancel_stop(old_id)
                self.store.conn.execute("UPDATE protection SET status='CANCELED' WHERE client_algo_id=?",
                                        (old_id,))
            except Exception:
                return self._exit("old stop cancellation uncertain")
        return True
