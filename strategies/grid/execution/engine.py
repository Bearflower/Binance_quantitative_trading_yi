"""Safety-first reconciliation for the dedicated ETHUSDT account.

All writes are disabled until the journal and exchange agree. More execution
transitions are added after the reconciliation contract is tested.
"""

from decimal import Decimal
import asyncio
import hashlib

from .policy import Action, decide
from .risk import RiskStatus
from .fills import FillCoordinator, ProtectionCoordinator, TradeUpdate


class GridEngine:
    def __init__(self, store, exchange):
        self.store = store
        self.exchange = exchange

    def _lock(self, reason: str) -> bool:
        self.store.set_engine_state("LOCKED", reason)
        return False

    async def on_order_update(self, raw_event: dict, *, epoch_id: int,
                              levels: tuple, filters, stop_loss_low: Decimal,
                              stop_loss_high: Decimal) -> bool:
        """Persist a trade, protect the net position, then place a paired order.

        The caller must serialize this with decision handling and reconnect
        reconciliation under the lifetime execution lock.
        """
        if raw_event.get("e") != "ORDER_TRADE_UPDATE":
            return True
        if self.store.engine_state()["state"] in ("LOCKED", "EXITING"):
            return False
        try:
            update = TradeUpdate.parse(raw_event)
            FillCoordinator(self.store).record(raw_event)
            positions = await self.exchange.position()
            if len(positions) != 1 or positions[0].get("positionSide") != "BOTH":
                return self._lock("position unavailable after fill")
            amount = Decimal(str(positions[0]["positionAmt"]))
            protected = await ProtectionCoordinator(self.store, self.exchange).ensure(
                epoch_id, amount, stop_loss_low, stop_loss_high)
            if not protected:
                await self.exit_all(epoch_id)
                return False
            if not await self.reconcile():
                await self.exit_all(epoch_id)
                return False
            if (self.store.engine_state()["state"] != "ACTIVE"
                    or update.execution_type != "TRADE"):
                return True
            coordinator = FillCoordinator(self.store)
            prepared = coordinator.prepare_reverse(
                update.client_order_id, levels, filters.step_size,
                filters.min_qty, filters.min_notional)
            if prepared.status == "REMAINDER":
                return True
            if prepared.status == "WAITING_LEVEL":
                # A level already occupied by an ordinary order is never
                # duplicated. Cancel it and wait for exact REST trade facts.
                for client_id in prepared.blocked_client_order_ids:
                    response = await self.exchange.cancel_order(client_id)
                    if response.get("status") not in ("CANCELED", "EXPIRED", "FILLED"):
                        return self._lock("reverse level cancellation uncertain")
                    self.store.record_order(client_id, str(response["orderId"]),
                                            response["status"],
                                            Decimal(str(response.get("executedQty", "0"))))
                if not await self.reconcile():
                    return False
                prepared = coordinator.prepare_reverse(
                    update.client_order_id, levels, filters.step_size,
                    filters.min_qty, filters.min_notional)
                if prepared.status != "READY":
                    return True
            result = await self.exchange.place_limit(
                prepared.side, prepared.quantity, prepared.price,
                prepared.client_order_id)
            if result.get("status") != "NEW":
                return self._lock("reverse order was not passive")
            self.store.record_order(prepared.client_order_id,
                                    str(result["orderId"]), "NEW",
                                    Decimal(str(result.get("executedQty", "0"))))
            return True
        except Exception:
            self._lock("fill handling uncertain")
            await self.exit_all(epoch_id)
            return False

    async def pause(self, epoch_id: int, *, stop_loss_low: Decimal,
                    stop_loss_high: Decimal) -> bool:
        """Stop replenishment and cancel every ordinary order that could add risk."""
        if self.store.engine_state()["state"] in ("LOCKED", "EXITING"):
            return False
        self.store.set_engine_state("PAUSED")
        try:
            for intent in self.store.active_orders():
                response = await self.exchange.cancel_order(intent["client_order_id"])
                if response.get("status") not in ("CANCELED", "EXPIRED", "FILLED"):
                    return self._lock("pause cancellation uncertain")
                self.store.record_order(intent["client_order_id"],
                                        str(response["orderId"]), response["status"],
                                        Decimal(str(response.get("executedQty", "0"))))
            positions = await self.exchange.position()
            if len(positions) != 1:
                return self._lock("position unavailable while pausing")
            amount = Decimal(str(positions[0]["positionAmt"]))
            protected = await ProtectionCoordinator(self.store, self.exchange).ensure(
                epoch_id, amount, stop_loss_low, stop_loss_high)
            if not protected:
                await self.exit_all(epoch_id)
                return False
            return await self.reconcile()
        except Exception:
            return self._lock("pause failed")

    async def start_grid(self, decision: dict, plan, risk_assessment, *,
                         leverage: int, now_ms: int) -> bool:
        """Create a fresh epoch only after risk, account mode and exchange checks.

        The caller must obtain `plan` from `build_plan` using current exchange
        filters and hard limits. This method enforces the execution boundary.
        """
        if self.store.engine_state()["state"] != "FLAT":
            return False
        if risk_assessment.status != RiskStatus.ALLOW:
            self._lock("risk: " + ",".join(risk_assessment.reasons))
            return False
        next_item = self.store.next_decision()
        if not next_item or next_item["decision_id"] != decision.get("decision_id"):
            return False
        age_ms = now_ms - decision["calculated_at_ms"]
        limits = {"15m": 30 * 60_000, "1h": 90 * 60_000,
                  "4h": 5 * 60 * 60_000}
        times = decision.get("last_closed_kline_ms") or {}
        if (not decision.get("executable") or age_ms < 0 or age_ms > 90 * 60_000
                or any(not isinstance(times.get(tf), int)
                       or not 0 < now_ms - times[tf] <= max_age
                       for tf, max_age in limits.items())):
            return False
        action = decide(decision, {"clean": True, "position": Decimal(0)},
                        {"locked": False})
        if action != Action.START:
            return False
        if not plan.orders or len({o.price for o in plan.orders}) != len(plan.orders):
            return False
        try:
            await self.exchange.verify_account(leverage)
            account = await self.exchange.account_info()
            if Decimal(str(account["availableBalance"])) < plan.max_margin_usdt:
                return self._lock("insufficient free balance")
            if not await self.reconcile():
                return False
        except Exception:
            return self._lock("account verification failed")

        grid_params = decision.get("grid_params") or {}
        epoch_id = self.store.create_epoch(
            decision["decision_id"], self._plan_digest(plan), levels=plan.levels,
            stop_loss_low=grid_params.get("stop_loss_low"),
            stop_loss_high=grid_params.get("stop_loss_high"))
        for order in plan.orders:
            token = "%s:%s:%s" % (epoch_id, order.level_index, order.side)
            client_id = "eg-" + hashlib.sha256(token.encode()).hexdigest()[:24]
            try:
                self.store.prepare_order(epoch_id, order.level_index, "ENTRY",
                                         order.side, order.price, order.quantity,
                                         client_id)
                result = await self.exchange.place_limit(
                    order.side, order.quantity, order.price, client_id)
                if result.get("clientOrderId", client_id) != client_id:
                    return self._lock("exchange client order ID mismatch")
                if result.get("status") != "NEW":
                    self._lock("opening order was not passive")
                    await self.exit_all(epoch_id)
                    return False
                self.store.record_order(client_id, str(result["orderId"]),
                                        "NEW", Decimal(str(result.get("executedQty", "0"))))
                positions = await self.exchange.position()
                if len(positions) != 1 or Decimal(str(positions[0]["positionAmt"])) != 0:
                    self._lock("position changed during grid start")
                    await self.exit_all(epoch_id)
                    return False
            except Exception:
                return self._lock("opening order result unknown: %s" % client_id)
        self.store.set_engine_state("ACTIVE")
        self.store.acknowledge(decision["decision_id"])
        return True

    @staticmethod
    def _plan_digest(plan) -> str:
        value = repr((tuple(str(level) for level in plan.levels),
                      tuple((o.level_index, o.side, str(o.price), str(o.quantity))
                            for o in plan.orders)))
        return hashlib.sha256(value.encode()).hexdigest()

    async def apply_decision(self, decision: dict, plan, risk_assessment, *,
                             leverage: int, now_ms: int,
                             stop_loss_low: Decimal,
                             stop_loss_high: Decimal) -> bool:
        """Apply one durable signal and acknowledge it only after safe facts."""
        next_item = self.store.next_decision()
        if not next_item or next_item["decision_id"] != decision.get("decision_id"):
            return False
        epoch_id = self.store.active_epoch_id()
        if risk_assessment.status != RiskStatus.ALLOW:
            self._lock("risk: " + ",".join(risk_assessment.reasons))
            if epoch_id is not None and not await self.exit_all(epoch_id):
                return False
            self.store.acknowledge(decision["decision_id"])
            return True

        if self.store.engine_state()["state"] == "LOCKED":
            return False
        age_ms = now_ms - decision["calculated_at_ms"]
        if (not decision.get("executable") or age_ms < 0
                or age_ms > 90 * 60_000):
            if epoch_id is not None and not await self.pause(
                    epoch_id, stop_loss_low=stop_loss_low,
                    stop_loss_high=stop_loss_high):
                return False
            self.store.acknowledge(decision["decision_id"])
            return True

        clean = await self.reconcile()
        if not clean:
            if epoch_id is not None:
                await self.exit_all(epoch_id)
            return False
        state = self.store.engine_state()["state"]
        action = decide(decision, {"clean": True, "position": Decimal(0),
                                   "active_epoch": bool(epoch_id)},
                        {"locked": state == "LOCKED"})
        if action == Action.EXIT:
            if epoch_id is not None and not await self.exit_all(epoch_id):
                return False
            self.store.acknowledge(decision["decision_id"])
            return True
        if action in (Action.PAUSE_EXPOSURE, Action.PAUSE_ALL):
            if epoch_id is not None and not await self.pause(
                    epoch_id, stop_loss_low=stop_loss_low,
                    stop_loss_high=stop_loss_high):
                return False
            self.store.acknowledge(decision["decision_id"])
            return True
        if action == Action.WAIT:
            if epoch_id is not None and not await self.pause(
                    epoch_id, stop_loss_low=stop_loss_low,
                    stop_loss_high=stop_loss_high):
                return False
            self.store.acknowledge(decision["decision_id"])
            return True
        if action in (Action.START, Action.REPLAN):
            if epoch_id is not None:
                epoch = self.store.epoch(epoch_id)
                if (state == "ACTIVE" and epoch["plan_digest"] == self._plan_digest(plan)):
                    self.store.acknowledge(decision["decision_id"])
                    return True
                if not await self.exit_all(epoch_id):
                    return False
            return await self.start_grid(decision, plan, risk_assessment,
                                         leverage=leverage, now_ms=now_ms)
        return False

    async def reconcile(self) -> bool:
        """Compare account-wide ETHUSDT facts with owned, durable intents."""
        if self.store.engine_state()["state"] == "LOCKED":
            return False
        ordinary = await self.exchange.open_orders()
        algo = await self.exchange.open_stops()
        position_rows = await self.exchange.position()
        if len(position_rows) != 1 or position_rows[0].get("positionSide") != "BOTH":
            return self._lock("cannot confirm one-way ETHUSDT position")
        position = Decimal(str(position_rows[0]["positionAmt"]))

        owned = {row["client_order_id"]: row for row in self.store.active_orders()}
        open_by_id = {}
        for row in ordinary:
            if row.get("symbol") != "ETHUSDT":
                return self._lock("unexpected ordinary order symbol")
            client_id = row.get("clientOrderId")
            if not client_id or not client_id.startswith("eg-") or client_id not in owned:
                return self._lock("foreign or unknown ETHUSDT ordinary order")
            if client_id in open_by_id:
                return self._lock("duplicate exchange client order ID")
            open_by_id[client_id] = row

        protection_ids = {row["client_algo_id"] for row in self.store.active_protections()}
        for row in algo:
            if row.get("symbol") != "ETHUSDT":
                return self._lock("unexpected algo order symbol")
            client_id = row.get("clientAlgoId")
            if not client_id or client_id not in protection_ids:
                return self._lock("foreign or unknown ETHUSDT algo order")

        for client_id, intent in owned.items():
            row = open_by_id.get(client_id)
            if row is None:
                row = await self.exchange.query_order(client_id)
            if not row or row.get("code") is not None:
                return self._lock("order result unknown: %s" % client_id)
            if row.get("clientOrderId", client_id) != client_id:
                return self._lock("order client ID mismatch")
            status = row.get("status")
            if status not in ("NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED"):
                return self._lock("unknown exchange order status")
            executed = Decimal(str(row.get("executedQty", "0")))
            if executed < Decimal(intent["executed_qty"]):
                return self._lock("exchange order snapshot regressed")
            self.store.record_order(client_id, str(row["orderId"]), status, executed)

        # REST trade facts fill gaps left by a disconnected user stream. A
        # missing integer trade ID is normal; exact per-order quantity is the
        # completeness check.
        for intent in self.store.all_orders():
            executed = Decimal(intent["executed_qty"])
            known = sum((Decimal(fill["quantity"]) for fill in self.store.fills(
                intent["client_order_id"])), Decimal(0))
            if known > executed:
                return self._lock("journal fills exceed exchange execution")
            if known == executed:
                continue
            if not intent["exchange_order_id"]:
                return self._lock("filled order has no exchange order ID")
            if not hasattr(self.exchange, "order_trades"):
                return self._lock("trade backfill unavailable")
            try:
                trades = await self.exchange.order_trades(int(intent["exchange_order_id"]))
                for trade in trades:
                    if trade.get("symbol") != "ETHUSDT" or trade.get("side") != intent["side"]:
                        return self._lock("trade ownership mismatch")
                    self.store.record_fill(intent["client_order_id"],
                                           intent["exchange_order_id"],
                                           str(trade["id"]), Decimal(str(trade["qty"])),
                                           Decimal(str(trade["price"])))
            except Exception:
                return self._lock("trade backfill failed")
            known = sum((Decimal(fill["quantity"]) for fill in self.store.fills(
                intent["client_order_id"])), Decimal(0))
            if known != executed:
                return self._lock("trade backfill incomplete")

        expected_position = sum((Decimal(row["quantity"]) *
                                 (1 if row["side"] == "BUY" else -1)
                                 for row in self.store.all_fills()), Decimal(0))
        if expected_position != position:
            return self._lock("position does not match journal fills")
        if position != 0 and not algo:
            return self._lock("position has no exchange stop protection")
        return True

    async def exit_all(self, epoch_id: int) -> bool:
        """Cancel owned grid orders, flatten confirmed residual, then clear stops.

        A lost close response never causes an unverified second market order.
        """
        was_locked = self.store.engine_state()["state"] == "LOCKED"
        if not was_locked:
            self.store.set_engine_state("EXITING")
        for intent in self.store.active_orders():
            client_id = intent["client_order_id"]
            try:
                response = await self.exchange.cancel_order(client_id)
                # The cancellation response can race with a fill. It is a fact,
                # never proof of an empty position.
                status = response.get("status")
                if status not in ("CANCELED", "EXPIRED", "FILLED"):
                    response = await self.exchange.query_order(client_id)
                if not response or response.get("status") not in ("CANCELED", "EXPIRED", "FILLED"):
                    return self._lock("cancel result unknown: %s" % client_id)
                self.store.record_order(client_id, str(response["orderId"]),
                                        response["status"],
                                        Decimal(str(response.get("executedQty", "0"))))
            except Exception:
                return self._lock("cancel result unknown: %s" % client_id)

        for attempt in range(3):
            try:
                positions = await self.exchange.position()
                if len(positions) != 1 or positions[0].get("positionSide") != "BOTH":
                    return self._lock("cannot verify position while exiting")
                amount = Decimal(str(positions[0]["positionAmt"]))
            except Exception:
                return self._lock("position unavailable while exiting")
            if amount == 0:
                break
            client_id = "eg-exit-%s-%s" % (epoch_id, attempt)
            side = "SELL" if amount > 0 else "BUY"
            try:
                # Role EXIT makes the sentinel price unmistakable in the journal.
                self.store.prepare_order(epoch_id, -1 - attempt, "EXIT", side,
                                         Decimal(1), abs(amount), client_id)
                response = await self.exchange.close_position(side, abs(amount), client_id)
            except Exception:
                try:
                    response = await self.exchange.query_order(client_id)
                except Exception:
                    response = None
                if not response or response.get("status") != "FILLED":
                    return self._lock("market close result unknown: %s" % client_id)
            if response.get("status") not in ("FILLED", "PARTIALLY_FILLED"):
                return self._lock("market close not complete: %s" % client_id)
            self.store.record_order(client_id, str(response["orderId"]),
                                    response["status"],
                                    Decimal(str(response.get("executedQty", "0"))))
            # A partially filled close may still be open. Do not send another.
            if response["status"] == "PARTIALLY_FILLED":
                return self._lock("partial market close needs reconciliation")
            await asyncio.sleep(0)
        else:
            return self._lock("residual position after close attempts")

        for protection in self.store.active_protections():
            client_id = protection["client_algo_id"]
            try:
                await self.exchange.cancel_stop(client_id)
                self.store.record_protection(client_id, "CANCELED")
            except Exception:
                return self._lock("stop cancellation uncertain")
        try:
            positions = await self.exchange.position()
            ordinary = await self.exchange.open_orders()
            algo = await self.exchange.open_stops()
            if (len(positions) != 1 or Decimal(str(positions[0]["positionAmt"])) != 0
                    or ordinary or algo or self.store.active_orders()
                    or self.store.active_protections()):
                return self._lock("exchange not flat after exit")
        except Exception:
            return self._lock("flat verification failed")
        if not was_locked:
            self.store.set_engine_state("FLAT")
        self.store.close_epoch(epoch_id)
        return True
