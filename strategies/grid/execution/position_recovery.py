"""Recover supported settled execution locks from independent PM evidence."""
import json
import logging
import time
from collections import defaultdict
from decimal import Decimal

from .fills import TradeUpdate
from .risk import RiskStatus
from .store import FILL_HANDLING_LOCK, POSITION_MISMATCH_LOCK


logger = logging.getLogger(__name__)
RECHECK_MS = 5000
RETRY_MS = 30000


class RecoveryEvidenceError(ValueError):
    pass


class UnownedRecoveryActivity(RecoveryEvidenceError):
    pass


class PositionLockRecovery:
    def __init__(self, store, exchange):
        self.store = store
        self.exchange = exchange
        self._first = None
        self._next_check_ms = 0

    def reset(self):
        self._first = None

    def due(self, now_ms):
        return now_ms >= self._next_check_ms

    def can_recover(self, state):
        if state["state"] != "LOCKED":
            return False
        if state["lock_reason"] in (POSITION_MISMATCH_LOCK, "position unavailable while exiting"):
            return True
        if state["lock_reason"] != FILL_HANDLING_LOCK:
            return False
        return self._has_settled_late_exit_fill(state)

    def _has_settled_late_exit_fill(self, state):
        # The old lock reason was shared by all fill failures. Require the
        # saved event that identifies this specific failure before probing PM.
        epoch = self.store.latest_epoch()
        conn = self.store.conn
        if (not epoch or epoch["status"] != "CLOSED"
                or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='order_event_failure'").fetchone()):
            return False
        failures = conn.execute("""SELECT observed_ms, payload_json, error FROM order_event_failure
            WHERE observed_ms >= ? LIMIT 2""", (epoch["created_at_ms"],)).fetchall()
        if len(failures) != 1:
            return False
        failure = failures[0]
        if (failure["observed_ms"] > state["updated_at_ms"]
                or failure["error"] != "ValueError: cumulative fill regressed"):
            return False
        try:
            event = TradeUpdate.parse(json.loads(failure["payload_json"]))
        except ValueError:
            return False
        order = self.store.get_order(event.client_order_id)
        if (not order or order["epoch_id"] != epoch["id"] or order["role"] != "EXIT"
                or order["status"] != "FILLED" or order["side"] != event.side
                or event.execution_type != "TRADE" or event.order_status != "PARTIALLY_FILLED"
                or not event.quantity <= event.cumulative_quantity < Decimal(order["executed_qty"])):
            return False
        return any(fill["trade_id"] == event.trade_id
                   and fill["exchange_order_id"] == event.exchange_order_id == order["exchange_order_id"]
                   and Decimal(fill["quantity"]) == event.quantity and Decimal(fill["price"]) == event.price
                   for fill in self.store.fills(event.client_order_id))

    async def check(self, state, risk, stream, now_ms):
        if not self.can_recover(state) or not stream.ready or risk.status != RiskStatus.ALLOW:
            self._first = None
            return
        if not self.due(now_ms):
            return
        self._next_check_ms = now_ms + RECHECK_MS
        try:
            epoch = self.store.latest_epoch()
            if not epoch or epoch["status"] != "CLOSED" or epoch["created_at_ms"] > state["updated_at_ms"]:
                raise RecoveryEvidenceError("closed epoch unavailable")
            evidence = await self._verify_flat(epoch, now_ms)
            if not stream.ready:
                self._first = None
                return
            fingerprint = (state["updated_at_ms"], state["lock_reason"], epoch["id"], evidence)
            if self._first != fingerprint:
                self._first = fingerprint
                logger.info("grid lock recovery first check passed: epoch=%s trades=%s", epoch["id"], len(evidence))
                return
            recovered_at_ms = int(time.time() * 1000)
            if self.store.finish_position_recovery(
                    state["updated_at_ms"], epoch["id"], len(evidence), recovered_at_ms,
                    reason=state["lock_reason"]):
                logger.info("grid position lock recovered: epoch=%s trades=%s signal_cutoff_ms=%s reason=%s",
                            epoch["id"], len(evidence), recovered_at_ms, state["lock_reason"])
            self._first = None
        except UnownedRecoveryActivity as exc:
            self.reset()
            self.store.set_engine_state("LOCKED", str(exc))
            logger.warning("grid position lock recovery requires operator: epoch=%s reason=%s", epoch["id"], exc)
        except RecoveryEvidenceError as exc:
            self._defer(now_ms, str(exc))
        except Exception as exc:
            self._defer(now_ms, type(exc).__name__)

    def _defer(self, now_ms, reason):
        self._first = None
        self._next_check_ms = now_ms + RETRY_MS
        logger.warning("grid position lock recovery waiting: reason=%s", reason)

    async def _verify_flat(self, epoch, now_ms):
        if self.store.active_epoch_id() is not None or self.store.active_orders() or self.store.active_protections():
            raise RecoveryEvidenceError("local orders or epoch remain open")
        positions = await self.exchange.position()
        if (len(positions) != 1 or positions[0]["symbol"] != "ETHUSDT" or positions[0]["positionSide"] != "BOTH"
                or Decimal(positions[0]["positionAmt"]) != 0):
            raise RecoveryEvidenceError("exchange net position is not flat")
        if await self.exchange.open_orders() or await self.exchange.open_stops():
            raise RecoveryEvidenceError("exchange orders or protections remain open")

        orders = {row["client_order_id"]: row for row in self.store.all_orders()}
        by_venue_id = {}
        required_orders = set()
        for order in orders.values():
            venue_id = order["exchange_order_id"]
            if not venue_id:
                continue
            if venue_id in by_venue_id:
                raise RecoveryEvidenceError("journal has duplicate exchange order IDs")
            by_venue_id[venue_id] = order
            if order["epoch_id"] == epoch["id"]:
                required_orders.add(venue_id)
        expected = {}
        required = set()
        totals = defaultdict(Decimal)
        for fill in self.store.all_fills():
            order = orders[fill["client_order_id"]]
            if order["exchange_order_id"] != fill["exchange_order_id"]:
                raise RecoveryEvidenceError("journal fill has a different order ID")
            identity = (fill["exchange_order_id"], fill["trade_id"])
            qty = Decimal(fill["quantity"])
            expected[identity] = (order["side"], qty, Decimal(fill["price"]))
            totals[fill["client_order_id"]] += qty
            if order["epoch_id"] == epoch["id"]:
                required.add(identity)
        if any(totals[client_id] != Decimal(order["executed_qty"]) for client_id, order in orders.items()):
            raise RecoveryEvidenceError("journal execution quantities are incomplete")
        if sum(qty if side == "BUY" else -qty for side, qty, _ in expected.values()) != 0:
            raise RecoveryEvidenceError("journal net position is not flat")

        # Include the latest closed round in full, with overlap for exchange
        # clock skew and any fills near the boundary with its predecessor.
        start_ms = max(1, epoch["created_at_ms"] - 5000)
        verified = set()
        for trade in await self.exchange.trade_history(start_ms, now_ms):
            identity = (str(trade["orderId"]), str(trade["id"]))
            if identity[0] not in by_venue_id:
                raise UnownedRecoveryActivity("unowned exchange trade during grid recovery")
            fact = (trade["side"], Decimal(trade["qty"]), Decimal(trade["price"]))
            if (trade["symbol"] != "ETHUSDT" or not start_ms <= trade["time"] <= now_ms
                    or identity in verified or expected.get(identity) != fact):
                raise RecoveryEvidenceError("exchange trade has no exact journal match")
            verified.add(identity)
        if not required <= verified:
            raise RecoveryEvidenceError("latest epoch trades are missing from exchange history")

        confirmed = set()
        for actual in await self.exchange.order_history(start_ms, now_ms):
            venue_id = str(actual["orderId"])
            order = by_venue_id.get(venue_id)
            if order is None:
                raise UnownedRecoveryActivity("unowned exchange order during grid recovery")
            if (actual["symbol"] != "ETHUSDT" or venue_id in confirmed
                    or (order["role"] != "STOP" and actual["clientOrderId"] != order["client_order_id"])
                    or actual["side"] != order["side"] or actual["status"] != order["status"]
                    or actual["status"] not in ("CANCELED", "EXPIRED", "FILLED")
                    or Decimal(actual["origQty"]) != Decimal(order["quantity"])
                    or Decimal(actual["executedQty"]) != Decimal(order["executed_qty"])):
                raise RecoveryEvidenceError("exchange order has no exact terminal journal match")
            confirmed.add(venue_id)
        if not required_orders or not required_orders <= confirmed:
            raise RecoveryEvidenceError("latest epoch orders are missing from exchange history")
        return frozenset(verified)
