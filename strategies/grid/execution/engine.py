"""Safety-first reconciliation for the dedicated ETHUSDT account.

All writes are disabled until the journal and exchange agree. More execution
transitions are added after the reconciliation contract is tested.
"""

from decimal import Decimal, ROUND_FLOOR
import asyncio
import hashlib
import time
import logging
import traceback

from shared.binance_api import BinanceAPIError

from .exposure import projected_exposure, validate_exposure
from .exchange import MakerOrderRejected
from .planner import PlanLimitExceeded

from .policy import Action, decide, grid_candles_current
from .risk import RiskStatus
from .store import FILL_HANDLING_LOCK, POSITION_MISMATCH_LOCK
from .fills import FillCoordinator, ProtectionCoordinator, TradeUpdate


logger = logging.getLogger(__name__)


def log_cancel_result(intent, response, *, trigger, resolved_by="cancel_response"):
    logger.info(
        "grid order cancel resolved: epoch=%s trigger=%s role=%s side=%s price=%s quantity=%s "
        "client_order_id=%s exchange_order_id=%s status=%s executed_qty=%s resolved_by=%s",
        intent["epoch_id"], trigger, intent["role"], intent["side"], intent["price"],
        intent["quantity"], intent["client_order_id"], response["orderId"], response["status"],
        response.get("executedQty", "0"), resolved_by,
    )


class _PositionSnapshotMismatch(Exception):
    def __init__(self, venue: Decimal, journal: Decimal):
        self.venue = venue
        self.journal = journal


class GridEngine:
    def __init__(self, store, exchange, limits=None):
        self.store = store
        self.exchange = exchange
        self.limits = limits
        self.trailing = None

    def _lock(self, reason: str) -> bool:
        # Cleanup reconciliation must neither downgrade an existing manual
        # lock to a recoverable reason nor reset this lock's original time.
        if reason != POSITION_MISMATCH_LOCK or self.store.engine_state()["state"] != "LOCKED":
            self.store.set_engine_state("LOCKED", reason)
        return False

    def _pending_stop_cleanup(self, cleanup, client_id, error=None):
        if error is not None:
            self.store.record_stop_cleanup_failure(client_id, error)
        logging.getLogger(__name__).warning(
            "grid flat stop cleanup pending: client_algo_id=%s started_at_ms=%s error=%r",
            client_id, cleanup["started_at_ms"], error or cleanup["last_error"])
        if int(time.time() * 1000) - cleanup["started_at_ms"] >= 300_000:
            return self._lock("stop cancellation uncertain")
        # EXITING forbids replenishment and new epochs. Existing operator locks
        # remain locks; this path never unlocks an account automatically.
        if self.store.engine_state()["state"] != "LOCKED":
            self.store.set_engine_state("EXITING", "awaiting stop terminal confirmation")
        return False

    async def check_limits(self, extra_orders=()):
        if self.limits is None:
            return
        positions = await self.exchange.position()
        mark = await self.exchange.mark_price()
        journal_position = sum((Decimal(f["quantity"]) *
                                (1 if f["side"] == "BUY" else -1)
                                for f in self.store.all_fills()), Decimal(0))
        orders = {o["client_order_id"]: dict(o) for o in self.store.active_orders()}
        known = {o["client_order_id"]: o for o in self.store.all_orders()}
        for actual in await self.exchange.open_orders():
            client_id = actual.get("clientOrderId")
            intent = known.get(client_id)
            if intent is None or intent["role"] not in ("ENTRY", "REVERSE"):
                raise ValueError("unowned ordinary order in exposure")
            if (actual.get("symbol") != "ETHUSDT" or actual.get("side") != intent["side"]
                    or Decimal(str(actual["origQty"])) != Decimal(intent["quantity"])
                    or Decimal(str(actual["price"])) != Decimal(intent["price"])):
                raise ValueError("exchange order differs from exposure intent")
            reserved = dict(intent)
            reserved["executed_qty"] = str(min(Decimal(intent["executed_qty"]),
                                                 Decimal(str(actual["executedQty"]))))
            orders[client_id] = reserved
        exposure = projected_exposure(
            positions, list(orders.values()) + list(extra_orders),
            mark, journal_position)
        validate_exposure(exposure, self.limits)

    async def enforce_limits(self, epoch_id):
        if self.limits is None or self.store.engine_state()["state"] not in ("ACTIVE", "PAUSED"):
            return True
        try:
            await self.check_limits()
            return True
        except PlanLimitExceeded as exc:
            logging.getLogger(__name__).warning("grid risk paused: %s", exc)
            context = self.store.epoch(epoch_id)
            if not await self.pause(
                    epoch_id, stop_loss_low=Decimal(context["stop_loss_low"]),
                    stop_loss_high=Decimal(context["stop_loss_high"]), trigger="exposure_limit"):
                await self.exit_all(epoch_id)
                return False
            # Canceling unfilled orders can restore the budget. Check the
            # remaining protected position before allowing further operation.
            try:
                await self.check_limits()
            except PlanLimitExceeded:
                self._lock("position exposure exceeds budget")
                await self.exit_all(epoch_id)
            except Exception:
                self._lock("exposure evidence unavailable after pause")
                await self.exit_all(epoch_id)
            return False
        except Exception:
            self._lock("exposure evidence unavailable")
            await self.exit_all(epoch_id)
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
        try:
            raw_event = await self.normalize_order_event(raw_event)
            update = TradeUpdate.parse(raw_event)
            inserted_fill = FillCoordinator(self.store).record(raw_event)
            source = self.store.get_order(update.client_order_id)
            if source["role"] == "STOP" and source["epoch_id"] == epoch_id:
                self.store.settle_stop(epoch_id)
                return await self.exit_all(epoch_id)
            if (source["epoch_id"] != epoch_id or source["role"] not in ("ENTRY", "REVERSE")
                    or self.store.epoch(source["epoch_id"])["status"] != "ACTIVE"):
                return True
            if self.store.engine_state()["state"] in ("LOCKED", "EXITING"):
                # Queued order acknowledgements can arrive after the exit
                # attempt. Only new evidence of execution needs prompt cleanup.
                if inserted_fill or (update.execution_type != "TRADE" and update.cumulative_quantity > 0):
                    await self.exit_all(epoch_id)
                return False
            # Ownership/status validation above still runs for every ACK. An
            # accepted NEW carries no fill and creates no protection obligation.
            if (update.execution_type == "NEW" and update.order_status == "NEW"
                    and update.cumulative_quantity == 0
                    and source["status"] == "NEW" and Decimal(source["executed_qty"]) == 0):
                return True
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
            if self.store.engine_state()["state"] == "EXITING":
                await self.exit_all(epoch_id)
                return False
            if self.store.engine_state()["state"] != "ACTIVE":
                return True
            if self.trailing is not None:
                if not await self.trailing.guard(epoch_id):
                    return False
                if self.trailing.pending(epoch_id):
                    return True
            for client_id in self.pending_obligations(epoch_id, levels, filters):
                if not await self._replenish(client_id, levels, filters):
                    return False
            return True
        except Exception as exc:
            self.store.record_event_failure(raw_event, exc)
            logging.getLogger(__name__).exception("grid order event processing failed")
            self._lock(FILL_HANDLING_LOCK)
            await self.exit_all(epoch_id)
            return False

    def pending_obligations(self, epoch_id, levels, filters):
        pending = []
        for source in self.store.all_orders():
            if source["epoch_id"] != epoch_id or source["role"] not in ("ENTRY", "REVERSE"):
                continue
            adjacent = self.store.adjacent_level(source, levels)
            if adjacent is None:
                continue
            target, target_price = adjacent
            rows = self.store.conn.execute("""SELECT p.quantity,o.executed_qty,o.status
                FROM pair p JOIN order_intent o ON o.client_order_id=p.reverse_client_order_id
                WHERE p.source_client_order_id=?""", (source["client_order_id"],)).fetchall()
            allocated = sum((Decimal(row[1]) if row[2] in ("FILLED", "CANCELED", "EXPIRED")
                             else Decimal(row[0]) for row in rows), Decimal(0))
            residual = Decimal(source["executed_qty"]) - allocated
            quantity = (residual / filters.step_size).to_integral_value(rounding=ROUND_FLOOR) * filters.step_size
            if quantity >= filters.min_qty and quantity * target_price >= filters.min_notional:
                pending.append(source["client_order_id"])
        return pending

    async def recover_obligations(self, epoch_id, filters, *, allow_moving=False):
        if self.trailing is not None and not allow_moving and self.trailing.pending(epoch_id):
            return
        context = self.store.epoch(epoch_id)
        if not context["levels_json"] or filters is None:
            return
        import json
        levels = tuple(Decimal(value) for value in json.loads(context["levels_json"]))
        if not self.pending_obligations(epoch_id, levels, filters):
            return
        try:
            if not await self.reconcile() or self.store.engine_state()["state"] != "ACTIVE":
                await self.exit_all(epoch_id)
                return
            for client_id in self.pending_obligations(epoch_id, levels, filters):
                if not await self._replenish(client_id, levels, filters):
                    await self.exit_all(epoch_id)
                    return
        except Exception:
            self._lock("pending fill obligation recovery failed")
            await self.exit_all(epoch_id)

    async def _replenish(self, source_client_id, levels, filters):
        if self.limits is not None and self.store.engine_state()["state"] != "ACTIVE":
            return True
        if self.trailing is not None:
            if not self.trailing.can_submit(self.store.active_epoch_id()):
                return True
            source = self.store.get_order(source_client_id)
            adjacent = self.store.adjacent_level(source, levels)
            if adjacent is None:
                return True
            _, target_price = adjacent
            mark = await self.exchange.mark_price()
            if ((source['side'] == 'BUY' and target_price <= mark)
                    or (source['side'] == 'SELL' and target_price >= mark)):
                return True  # Keep the exact obligation; do not force a taker fill.
        coordinator = FillCoordinator(self.store)
        prepared = coordinator.prepare_reverse(
            source_client_id, levels, filters.step_size,
            filters.min_qty, filters.min_notional)
        if prepared.status in ("REMAINDER", "RETIRED"):
            return True
        if prepared.status == "WAITING_LEVEL":
            # A level already occupied by an ordinary order is never
            # duplicated. Cancel it and wait for exact REST trade facts.
            for client_id in prepared.blocked_client_order_ids:
                blocker = self.store.get_order(client_id)
                allocation = self.store.conn.execute(
                    "SELECT source_client_order_id FROM pair WHERE reverse_client_order_id=?", (client_id,)).fetchone()
                if blocker["role"] == "REVERSE" and (allocation is None or allocation[0] != source_client_id):
                    return True
            for client_id in prepared.blocked_client_order_ids:
                intent = self.store.get_order(client_id)
                response = await self.exchange.cancel_order(client_id)
                if response.get("status") not in ("CANCELED", "EXPIRED", "FILLED"):
                    return self._lock("reverse level cancellation uncertain")
                self.store.record_order(client_id, str(response["orderId"]),
                                        response["status"],
                                        Decimal(str(response.get("executedQty", "0"))))
                log_cancel_result(intent, response, trigger="pair_level_replacement")
            if not await self.reconcile() or self.store.engine_state()["state"] != "ACTIVE":
                return False
            prepared = coordinator.prepare_reverse(
                source_client_id, levels, filters.step_size,
                filters.min_qty, filters.min_notional)
            if prepared.status != "READY":
                return True
        try:
            await self.check_limits()
            if self.limits is not None:
                account = await self.exchange.account_info()
                free = Decimal(str(account["totalAvailableBalance"]))
                # A closing order may require no new margin. The exchange still
                # validates actual PM availability when accepting this order.
                position = Decimal(str((await self.exchange.position())[0]["positionAmt"]))
                adding = max(abs(position + (prepared.quantity if prepared.side == "BUY"
                                             else -prepared.quantity)) - abs(position), Decimal(0))
                if not free.is_finite() or free < adding * prepared.price / self.limits.leverage:
                    raise PlanLimitExceeded("insufficient free margin for reverse order")
        except PlanLimitExceeded as exc:
            self.store.discard_prepared_reverse(prepared.client_order_id)
            logging.getLogger(__name__).warning("grid reverse deferred: %s", exc)
            context = self.store.epoch(self.store.active_epoch_id())
            return await self.pause(context["id"], stop_loss_low=Decimal(context["stop_loss_low"]),
                                    stop_loss_high=Decimal(context["stop_loss_high"]), trigger="reverse_order_budget")
        except Exception:
            self.store.discard_prepared_reverse(prepared.client_order_id)
            self._lock("reverse exposure evidence unavailable")
            await self.exit_all(self.store.active_epoch_id())
            return False
        result = await self.exchange.place_limit(
            prepared.side, prepared.quantity, prepared.price,
            prepared.client_order_id)
        if result.get("status") != "NEW":
            return self._lock("reverse order was not passive")
        self.store.record_order(prepared.client_order_id,
                                str(result["orderId"]), "NEW",
                                Decimal(str(result.get("executedQty", "0"))))
        logger.info(
            "grid pair placed: epoch=%s source_client_order_id=%s client_order_id=%s exchange_order_id=%s side=%s price=%s quantity=%s",
            self.store.active_epoch_id(), source_client_id, prepared.client_order_id,
            result["orderId"], prepared.side, prepared.price, prepared.quantity,
        )
        return True

    async def normalize_order_event(self, event):
        order = event.get("o") or {}
        if self.store.get_order(str(order.get("c"))):
            return event
        await self.sync_triggered_stops()
        owned = self.store.conn.execute(
            "SELECT client_order_id FROM order_intent WHERE role='STOP' AND exchange_order_id=?",
            (str(order.get("i")),)).fetchone()
        if owned:
            return dict(event, o=dict(order, c=owned["client_order_id"]))
        return event

    async def sync_triggered_stops(self):
        self.store.recover_legacy_stop()
        for stop in self.store.active_protections():
            if not hasattr(self.exchange, "query_stop"):
                continue
            response = await self.exchange.query_stop(stop["client_algo_id"])
            if (response and response.get("algoStatus") in ("CANCELED", "EXPIRED", "REJECTED")
                    and not response.get("actualOrderId")):
                if response.get("clientAlgoId") != stop["client_algo_id"]:
                    raise ValueError("stop ownership mismatch")
                if response.get("algoId") is not None:
                    self.store.conn.execute(
                        "UPDATE protection SET exchange_algo_id=? WHERE client_algo_id=?",
                        (str(response["algoId"]), stop["client_algo_id"]))
                positions = await self.exchange.position()
                flat = (len(positions) == 1 and positions[0].get("positionSide") == "BOTH"
                        and Decimal(str(positions[0]["positionAmt"])) == 0)
                if flat:
                    self.store.record_protection(stop["client_algo_id"], "CANCELED",
                                                 str(response.get("algoId") or stop["exchange_algo_id"]))
                else:
                    self.store.settle_stop(stop["epoch_id"], stop["client_algo_id"], status="CANCELED")
                continue
            if not response or response.get("algoStatus") not in ("TRIGGERED", "FINISHED"):
                continue
            if response.get("clientAlgoId") != stop["client_algo_id"]:
                raise ValueError("stop ownership mismatch")
            order_id = int(response["actualOrderId"])
            child = await self.exchange.query_order_by_id(order_id)
            if (child.get("symbol") != "ETHUSDT" or child.get("side") != stop["side"]
                    or int(child["orderId"]) != order_id):
                raise ValueError("stop child ownership mismatch")
            quantity = Decimal(str(child["origQty"]))
            client_id = "eg-stop-" + str(order_id)
            if self.store.get_order(client_id) is None:
                self.store.prepare_order(stop["epoch_id"], -1000000-order_id, "STOP",
                                         stop["side"], Decimal(stop["trigger_price"]),
                                         quantity, client_id)
            self.store.record_order(client_id, str(order_id), child["status"],
                                    Decimal(str(child["executedQty"])))
            trades = await self.exchange.order_trades(order_id,
                self.store.epoch(stop["epoch_id"])["created_at_ms"], int(time.time()*1000))
            for trade in trades:
                if (int(trade["orderId"]) != order_id or trade.get("symbol") != "ETHUSDT"
                        or trade.get("side") != stop["side"]):
                    raise ValueError("stop child trade mismatch")
                self.store.record_fill(client_id, str(order_id), str(trade["id"]),
                                       Decimal(str(trade["qty"])), Decimal(str(trade["price"])))
            if sum((Decimal(f["quantity"]) for f in self.store.fills(client_id)), Decimal(0)) != Decimal(str(child["executedQty"])):
                raise ValueError("stop child fills incomplete")
            terminal = child["status"] in ("FILLED", "CANCELED", "EXPIRED")
            self.store.settle_stop(stop["epoch_id"], stop["client_algo_id"] if terminal else None)

    async def pause(self, epoch_id: int, *, stop_loss_low: Decimal,
                    stop_loss_high: Decimal, reason=None, trigger="unspecified") -> bool:
        """Stop replenishment and cancel every ordinary order that could add risk."""
        if self.store.engine_state()["state"] in ("LOCKED", "EXITING"):
            return False
        self.store.set_engine_state("PAUSED", reason)
        context = self.store.epoch(epoch_id)
        if context["stop_loss_low"] and context["stop_loss_high"]:
            stop_loss_low = Decimal(context["stop_loss_low"])
            stop_loss_high = Decimal(context["stop_loss_high"])
        trigger = reason or trigger
        stage = "cancel_orders"
        client_id = None
        try:
            orders = self.store.active_orders()
            logger.info("grid pause started: epoch=%s trigger=%s orders=%s", epoch_id, trigger, len(orders))
            for intent in orders:
                client_id = intent["client_order_id"]
                if (intent['status'] == 'PREPARED' and not intent['exchange_order_id']
                        and self.store.unsubmitted_move_draft(intent['client_order_id'])):
                    self.store.record_order(intent['client_order_id'], '', 'CANCELED', Decimal(0))
                    continue
                resolved_by = "cancel_response"
                try:
                    response = await self.exchange.cancel_order(client_id)
                except BinanceAPIError as exc:
                    if exc.code != -2011:
                        raise
                    # It may have filled during disconnect. Require its exact
                    # terminal exchange record before using the remaining qty.
                    resolved_by = "status_query"
                    response = await self.exchange.query_order(client_id)
                executed = Decimal(str(response["executedQty"]))
                if (response.get("clientOrderId") != client_id
                        or (intent["exchange_order_id"] and str(response["orderId"]) != intent["exchange_order_id"])
                        or not Decimal(intent["executed_qty"]) <= executed <= Decimal(intent["quantity"])
                        or response.get("status") not in ("CANCELED", "EXPIRED", "FILLED")):
                    logger.warning("grid pause cancel unconfirmed: epoch=%s trigger=%s client_order_id=%s",
                                   epoch_id, trigger, client_id)
                    return self._lock("pause cancellation uncertain")
                self.store.record_order(client_id, str(response["orderId"]), response["status"], executed)
                log_cancel_result(intent, response, trigger=trigger, resolved_by=resolved_by)
            client_id = None
            stage = "position"
            positions = await self.exchange.position()
            if len(positions) != 1:
                return self._lock("position unavailable while pausing")
            amount = Decimal(str(positions[0]["positionAmt"]))
            stage = "protection"
            protected = await ProtectionCoordinator(self.store, self.exchange).ensure(
                epoch_id, amount, stop_loss_low, stop_loss_high)
            if not protected:
                await self.exit_all(epoch_id)
                return False
            stage = "reconcile"
            reconciled = await self.reconcile()
            logger.info("grid pause checked: epoch=%s trigger=%s position_qty=%s reconciled=%s state=%s",
                        epoch_id, trigger, amount, reconciled, self.store.engine_state()["state"])
            return reconciled
        except Exception as exc:
            logger.warning("grid pause failed: epoch=%s trigger=%s stage=%s client_order_id=%s type=%s api_code=%s",
                           epoch_id, trigger, stage, client_id, type(exc).__name__,
                           exc.code if isinstance(exc, BinanceAPIError) else None)
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
        if (not decision.get("executable") or age_ms < 0 or age_ms > 90 * 60_000
                or not grid_candles_current(decision, now_ms)):
            return False
        action = decide(decision, {"clean": True, "position": Decimal(0)},
                        {"locked": False})
        if action != Action.START:
            return False
        if not plan.orders or len({o.price for o in plan.orders}) != len(plan.orders):
            return False
        from .exchange import GridExchange
        if (isinstance(self.exchange, GridExchange)
                and not self.exchange.can_start_batch(len(plan.orders), planning=False)):
            return False
        try:
            verified_filters = await self.exchange.verify_account(leverage)
            account = await self.exchange.account_info()
            if Decimal(str(account["totalAvailableBalance"])) < plan.max_margin_usdt:
                return self._lock("insufficient free balance")
            if not await self.reconcile():
                return False
            await self.check_limits([
                {"role": "ENTRY", "side": o.side, "price": str(o.price),
                 "quantity": str(o.quantity), "executed_qty": "0"} for o in plan.orders])
        except PlanLimitExceeded:
            self.store.acknowledge(decision["decision_id"])
            return True
        except Exception:
            return self._lock("account verification failed")

        grid_params = decision.get("grid_params") or {}
        stop_low = plan.stop_loss_low if plan.stop_loss_low is not None else grid_params.get("stop_loss_low")
        stop_high = plan.stop_loss_high if plan.stop_loss_high is not None else grid_params.get("stop_loss_high")
        epoch_id = self.store.create_epoch(
            decision["decision_id"],
            self._plan_digest(plan, stop_low, stop_high),
            levels=plan.levels,
            stop_loss_low=stop_low, stop_loss_high=stop_high)
        if plan.sizing is not None:
            # Persist the executed geometry before the first exchange write.
            import json
            self.store.conn.execute('INSERT INTO schema_meta(key,value) VALUES(?,?)',
                ('grid_epoch_sizing:%s' % epoch_id, json.dumps(plan.sizing, sort_keys=True)))
        for order in plan.orders:
            token = "%s:%s:%s" % (epoch_id, order.level_index, order.side)
            client_id = "eg-" + hashlib.sha256(token.encode()).hexdigest()[:24]
            try:
                await self.check_limits([{
                    "role": "ENTRY", "side": order.side, "price": str(order.price),
                    "quantity": str(order.quantity), "executed_qty": "0"}])
                self.store.prepare_order(epoch_id, order.level_index, "ENTRY",
                                         order.side, order.price, order.quantity,
                                         client_id)
                result = await self.exchange.place_limit(
                    order.side, order.quantity, order.price, client_id)
                if result.get("clientOrderId", client_id) != client_id:
                    raise ValueError("exchange client order ID mismatch")
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
            except MakerOrderRejected:
                self.store.record_maker_rejection(client_id)
                logger.info('grid startup maker order rejected: epoch=%s client_order_id=%s', epoch_id, client_id)
                # Market moved across a planned level while the batch was
                # opening. Retire the partial grid through normal cancellation
                # and position reconciliation; never change it to a taker.
                if await self.exit_all(epoch_id, trigger='startup maker rejection'):
                    self.store.acknowledge_after_action(decision['decision_id'], epoch_id)
                return False
            except PlanLimitExceeded as exc:
                logging.getLogger(__name__).warning("grid start paused: %s", exc)
                if await self.exit_all(epoch_id):
                    self.store.acknowledge_after_action(decision["decision_id"], epoch_id)
                return False
            except Exception:
                self._lock("opening order result unknown: %s" % client_id)
                await self.exit_all(epoch_id)
                return False
        self.store.set_engine_state("ACTIVE")
        if self.trailing is not None:
            self.trailing.ensure_epoch(epoch_id, verified_filters)
        self.store.acknowledge(decision["decision_id"])
        return True

    @staticmethod
    def _plan_digest(plan, stop_loss_low=None, stop_loss_high=None) -> str:
        stop_loss_low = plan.stop_loss_low if plan.stop_loss_low is not None else stop_loss_low
        stop_loss_high = plan.stop_loss_high if plan.stop_loss_high is not None else stop_loss_high
        value = repr((tuple(str(level) for level in plan.levels),
                      tuple((o.level_index, o.side, str(o.price), str(o.quantity))
                            for o in plan.orders),
                      str(stop_loss_low), str(stop_loss_high)))
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
            self.store.acknowledge_after_action(decision["decision_id"], epoch_id)
            return True

        if self.store.engine_state()["state"] == "LOCKED":
            return False
        market_state = decision["state"]
        age_ms = now_ms - decision["calculated_at_ms"]
        if (not decision.get("executable") or age_ms < 0
                or age_ms > 90 * 60_000):
            if epoch_id is not None and not await self.pause(
                    epoch_id, stop_loss_low=stop_loss_low,
                    stop_loss_high=stop_loss_high, trigger="signal_unusable"):
                return False
            self.store.acknowledge_after_action(decision["decision_id"], epoch_id)
            return True

        clean = await self.reconcile()
        if not clean:
            if epoch_id is not None:
                await self.exit_all(epoch_id)
            return False
        state = self.store.engine_state()["state"]
        if state == "EXITING":
            if epoch_id is not None and not await self.exit_all(epoch_id):
                return False
            self.store.acknowledge_after_action(decision["decision_id"], epoch_id)
            return True
        action = decide(decision, {"clean": True, "position": Decimal(0),
                                   "active_epoch": bool(epoch_id)},
                        {"locked": state == "LOCKED"})
        if action == Action.EXIT:
            if epoch_id is not None and not await self.exit_all(epoch_id, trigger=f"signal_exit:{market_state}"):
                return False
            self.store.acknowledge_after_action(decision["decision_id"], epoch_id)
            return True
        if action in (Action.PAUSE_EXPOSURE, Action.PAUSE_ALL):
            if epoch_id is not None and not await self.pause(
                    epoch_id, stop_loss_low=stop_loss_low,
                    stop_loss_high=stop_loss_high, trigger=f"signal_pause:{market_state}"):
                return False
            self.store.acknowledge_after_action(decision["decision_id"], epoch_id)
            return True
        if action == Action.WAIT:
            if epoch_id is not None and not await self.pause(
                    epoch_id, stop_loss_low=stop_loss_low,
                    stop_loss_high=stop_loss_high, trigger=f"signal_wait:{market_state}"):
                return False
            self.store.acknowledge_after_action(decision["decision_id"], epoch_id)
            return True
        if action in (Action.START, Action.REPLAN):
            if epoch_id is not None:
                if self.trailing is not None and self.trailing.keep_active(decision, epoch_id):
                    self.store.acknowledge(decision['decision_id'])
                    return True
                epoch = self.store.epoch(epoch_id)
                if (state == "ACTIVE" and epoch["plan_digest"] ==
                        self._plan_digest(plan, stop_loss_low, stop_loss_high)):
                    self.store.acknowledge(decision["decision_id"])
                    return True
                if not await self.exit_all(epoch_id, trigger=f"signal_replan:{market_state}"):
                    return False
            return await self.start_grid(decision, plan, risk_assessment,
                                         leverage=leverage, now_ms=now_ms)
        return False

    async def reconcile(self, *, allow_locked: bool = False) -> bool:
        """Compare account-wide ETHUSDT facts with owned, durable intents."""
        for attempt in range(1, 4):
            try:
                matched = await self._reconcile_facts(allow_locked=allow_locked)
                if matched and attempt > 1:
                    logger.info("grid position snapshot reconciled: epoch=%s attempt=%s",
                                self.store.active_epoch_id(), attempt)
                return matched
            except _PositionSnapshotMismatch as mismatch:
                logger.warning(
                    "grid position snapshot mismatch: epoch=%s attempt=%s venue_qty=%s journal_qty=%s",
                    self.store.active_epoch_id(), attempt, mismatch.venue, mismatch.journal,
                )
                if attempt == 3:
                    return self._lock(POSITION_MISMATCH_LOCK)
                # Order, trade and position endpoints are separate snapshots.
                # Refresh the complete view under the existing execution guard
                # before considering the funds unmatched.
                await asyncio.sleep(0.15 * attempt)
            except Exception as exc:
                # Signed transport errors may include credentials in their message.
                path = " > ".join(frame.name for frame in traceback.extract_tb(exc.__traceback__))
                logger.error("grid reconciliation failed: epoch=%s path=%s error_type=%s api_code=%s",
                             self.store.active_epoch_id(), path, type(exc).__name__,
                             getattr(exc, "code", None))
                return self._lock("exchange reconciliation failed")

    async def _query_missing_order(self, intent):
        client_id = intent["client_order_id"]
        venue_id = intent["exchange_order_id"]
        if intent["role"] == "STOP":
            return await self.exchange.query_order_by_id(int(venue_id))
        try:
            return await self.exchange.query_order(client_id)
        except BinanceAPIError as exc:
            logger.warning("grid ordinary order lookup failed: epoch=%s role=%s client_order_id=%s "
                           "exchange_order_id=%s api_code=%s", intent["epoch_id"], intent["role"],
                           client_id, venue_id, exc.code)
            if exc.code != -2013 or not venue_id:
                raise

        # The accepted order has a durable exchange ID. A negative lookup
        # proves neither cancellation nor a fill. Only retry exact GETs.
        for attempt in range(1, 4):
            try:
                row = await self.exchange.query_order_by_id(int(venue_id))
            except BinanceAPIError as exc:
                if exc.code != -2013 or attempt == 3:
                    raise
                await asyncio.sleep(0.2 * attempt)
                continue
            if (not isinstance(row, dict) or str(row.get("orderId")) != venue_id
                    or row.get("clientOrderId") != client_id or row.get("symbol") != "ETHUSDT"
                    or row.get("side") != intent["side"]
                    or Decimal(str(row["origQty"])) != Decimal(intent["quantity"])
                    or not Decimal(0) <= Decimal(str(row["executedQty"])) <= Decimal(intent["quantity"])):
                raise ValueError("exchange order identity or quantity mismatch")
            logger.info("grid order verified by exchange ID: epoch=%s client_order_id=%s "
                        "exchange_order_id=%s attempt=%s status=%s", intent["epoch_id"], client_id,
                        venue_id, attempt, row.get("status"))
            return row

    async def _reconcile_facts(self, *, allow_locked: bool = False) -> bool:
        if self.store.engine_state()["state"] == "LOCKED" and not allow_locked:
            return False
        await self.sync_triggered_stops()
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
            stop_intent = next((intent for intent in owned.values()
                                if intent["role"] == "STOP" and
                                intent["exchange_order_id"] == str(row.get("orderId"))), None)
            if stop_intent:
                client_id = stop_intent["client_order_id"]
                row = dict(row, clientOrderId=client_id)
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
            if (intent['status'] == 'PREPARED' and not intent['exchange_order_id']
                    and self.store.unsubmitted_move_draft(client_id)):
                continue
            row = open_by_id.get(client_id)
            if row is None:
                row = await self._query_missing_order(intent)
            if not row or row.get("code") is not None:
                return self._lock("order result unknown: %s" % client_id)
            if intent["role"] != "STOP" and row.get("clientOrderId", client_id) != client_id:
                return self._lock("order client ID mismatch")
            if intent["exchange_order_id"] and str(row.get("orderId")) != intent["exchange_order_id"]:
                return self._lock("exchange order ID mismatch")
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
                trades = await self.exchange.order_trades(
                    int(intent["exchange_order_id"]), int(intent["created_at_ms"]),
                    int(time.time() * 1000))
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
            raise _PositionSnapshotMismatch(position, expected_position)
        if position != 0:
            side = "SELL" if position > 0 else "BUY"
            epoch_id = self.store.active_epoch_id()
            context = self.store.epoch(epoch_id) if epoch_id is not None else None
            bounds_valid = False
            context_has_bounds = context and (context["stop_loss_low"] is not None
                                              or context["stop_loss_high"] is not None)
            low = high = None
            if context:
                try:
                    low = Decimal(context["stop_loss_low"])
                    high = Decimal(context["stop_loss_high"])
                    bounds_valid = low.is_finite() and high.is_finite() and 0 < low < high
                except (TypeError, ValueError, ArithmeticError):
                    pass
            expected_trigger = low if position > 0 else high
            identity_conflict = False

            def valid_stop(row, intent):
                nonlocal identity_conflict
                if not row:
                    return False
                try:
                    known_id = intent["exchange_algo_id"]
                    actual_id = str(row.get("algoId", ""))
                    if known_id and row.get("clientAlgoId") == intent["client_algo_id"]:
                        if (not actual_id.isdecimal() or not str(known_id).isdecimal()
                                or int(actual_id) != int(known_id)):
                            identity_conflict = True
                            return False
                    trigger = Decimal(str(row.get("triggerPrice", "0")))
                    return (row.get("symbol") == "ETHUSDT"
                            and row.get("clientAlgoId") == intent["client_algo_id"]
                            and row.get("side") == side == intent["side"]
                            and row.get("algoStatus") == "NEW"
                            and row.get("closePosition") is True
                            and row.get("workingType") == "MARK_PRICE"
                            and row.get("orderType", row.get("type")) == "STOP_MARKET"
                            and row.get("positionSide") == "BOTH"
                            and intent["epoch_id"] == epoch_id
                            and trigger.is_finite() and trigger > 0
                            and trigger == Decimal(intent["trigger_price"])
                            and (not context_has_bounds
                                 or (bounds_valid and trigger == expected_trigger)))
                except (ValueError, ArithmeticError):
                    return False

            async def confirm_stop(rows, attempts):
                intents = [p for p in self.store.active_protections()
                           if p["epoch_id"] == epoch_id and p["side"] == side]
                if any(valid_stop(row, intent) for row in rows for intent in intents):
                    return True
                # The algo list may lag a successful placement. Query only
                # durable owned IDs, and apply exactly the same full validation.
                for _ in range(attempts):
                    for intent in intents:
                        row = None
                        try:
                            row = await self.exchange.query_stop(intent["client_algo_id"])
                        except Exception as exc:
                            logging.getLogger(__name__).warning(
                                "grid stop query unavailable: epoch=%s client_algo_id=%s error_type=%s",
                                epoch_id, intent["client_algo_id"], type(exc).__name__)
                        if valid_stop(row, intent):
                            return True
                        fields = ("symbol", "clientAlgoId", "algoId", "algoStatus", "side",
                                  "triggerPrice", "closePosition", "workingType",
                                  "orderType", "type", "positionSide", "actualOrderId")
                        logging.getLogger(__name__).warning(
                            "grid protection snapshot invalid: epoch=%s client_algo_id=%s expected_side=%s "
                            "expected_trigger=%s snapshot=%s", epoch_id, intent["client_algo_id"],
                            side, intent["trigger_price"],
                            {key: row.get(key) for key in fields} if row else None)
                return False

            if not await confirm_stop(algo, 1):
                if identity_conflict:
                    return self._lock("exchange stop ID differs from protection intent")
                state = self.store.engine_state()["state"]
                # REST may win the race with the user stream. Once owned fills
                # account for the entire venue position, protect that position
                # before reconciliation permits any new/replenished order.
                if state in ("ACTIVE", "PAUSED") and context:
                    if bounds_valid:
                        try:
                            ensured = await ProtectionCoordinator(self.store, self.exchange).ensure(
                                epoch_id, position, low, high)
                        except Exception as exc:
                            ensured = False
                            logging.getLogger(__name__).warning(
                                "grid protection ensure failed: epoch=%s error_type=%s",
                                epoch_id, type(exc).__name__)
                        # ensure persists the intent before POST and never
                        # resubmits an existing uncertain intent. Its success
                        # alone is insufficient evidence of every stop field.
                        confirmed = await confirm_stop([], 2)
                        if ensured and confirmed:
                            return True
                return self._lock("position has no valid exchange stop protection")
        return True

    async def exit_all(self, epoch_id: int, *, trigger=None) -> bool:
        """Cancel owned grid orders, flatten confirmed residual, then clear stops.

        A lost close response never causes an unverified second market order.
        """
        try:
            await self.sync_triggered_stops()
        except Exception:
            # Keep reducing known exposure even while stop ownership is unresolved.
            self._lock("stop child reconciliation uncertain")
        state = self.store.engine_state()
        was_locked = state["state"] == "LOCKED"
        trigger = trigger or state["lock_reason"] or "exit_cleanup"
        if state["state"] != "EXITING":
            logger.info("grid exit started: epoch=%s trigger=%s state=%s", epoch_id, trigger, state["state"])
        if not was_locked:
            self.store.set_engine_state("EXITING")
        # A prior MARKET close can survive a process restart. Query it first;
        # canceling it as though it were a resting grid order is unsafe.
        for intent in self.store.active_orders():
            if intent["role"] != "EXIT":
                continue
            client_id = intent["client_order_id"]
            response = await self._resolve_close(client_id)
            if response is None:
                return self._lock("previous market close unresolved: %s" % client_id)
            self.store.record_order(client_id, str(response["orderId"]),
                                    response["status"],
                                    Decimal(str(response.get("executedQty", "0"))))
        unresolved = False
        for intent in self.store.active_orders():
            if intent["role"] in ("EXIT", "STOP"):
                unresolved = True
                continue
            client_id = intent["client_order_id"]
            if (intent['status'] == 'PREPARED' and not intent['exchange_order_id']
                    and self.store.unsubmitted_move_draft(client_id)):
                self.store.record_order(client_id, '', 'CANCELED', Decimal(0))
                continue
            try:
                resolved_by = "cancel_response"
                try:
                    response = await self.exchange.cancel_order(client_id)
                except BinanceAPIError as exc:
                    if exc.code != -2011:
                        raise
                    # A filled order is no longer cancellable. Resolve it by
                    # client ID while the later reconciliation checks fills.
                    response = None
                terminal = ("CANCELED", "EXPIRED", "FILLED")
                if not response or response.get("status") not in terminal:
                    resolved_by = "status_query"
                    response = await self.exchange.query_order(client_id)
                if not response or response.get("status") not in terminal:
                    raise ValueError("exit cancellation is not terminal")
                venue_id = str(response["orderId"])
                executed = Decimal(str(response["executedQty"]))
                if (response.get("clientOrderId") != client_id
                        or response.get("symbol", "ETHUSDT") != "ETHUSDT"
                        or response.get("side", intent["side"]) != intent["side"]
                        or not venue_id.isdigit() or int(venue_id) <= 0
                        or (intent["exchange_order_id"] and venue_id != intent["exchange_order_id"])
                        or not Decimal(intent["executed_qty"]) <= executed <= Decimal(intent["quantity"])
                        or (response["status"] == "FILLED" and executed != Decimal(intent["quantity"]))):
                    raise ValueError("exit cancellation differs from order intent")
                self.store.record_order(client_id, venue_id, response["status"], executed)
                log_cancel_result(intent, response, trigger=trigger, resolved_by=resolved_by)
            except Exception as exc:
                logger.warning("grid exit cancel unconfirmed: epoch=%s trigger=%s client_order_id=%s "
                               "exchange_order_id=%s resolved_by=%s type=%s api_code=%s",
                               epoch_id, trigger, client_id, intent["exchange_order_id"], resolved_by,
                               type(exc).__name__, exc.code if isinstance(exc, BinanceAPIError) else None)
                unresolved = True
        previous_closes = sum(1 for row in self.store.all_orders()
                              if row["epoch_id"] == epoch_id and row["role"] == "EXIT")
        if previous_closes >= 3:
            rows = await self.exchange.position()
            if (len(rows) != 1 or
                    Decimal(str(rows[0]["positionAmt"])) != 0):
                return self._lock("too many market close attempts")
        for attempt in range(previous_closes, 3):
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
                response = None
            response = await self._resolve_close(client_id, response)
            if response is None:
                return self._lock("market close result unknown: %s" % client_id)
            self.store.record_order(client_id, str(response["orderId"]),
                                    response["status"],
                                    Decimal(str(response.get("executedQty", "0"))))
            logger.info(
                "grid close resolved: epoch=%s trigger=%s client_order_id=%s exchange_order_id=%s status=%s executed_qty=%s",
                epoch_id, trigger, client_id, response["orderId"], response["status"], response.get("executedQty", "0"),
            )
            if response["status"] != "FILLED":
                return self._lock("market close did not fill")
            # Permit exchange position propagation before considering any
            # confirmed residual. Repeating against a stale position is unsafe.
            for _ in range(5):
                rows = await self.exchange.position()
                if len(rows) == 1 and Decimal(str(rows[0]["positionAmt"])) == 0:
                    break
                await asyncio.sleep(0.2)
        else:
            rows = await self.exchange.position()
            if len(rows) != 1 or Decimal(str(rows[0]["positionAmt"])) != 0:
                return self._lock("residual position after close attempts")

        if unresolved:
            return self._lock("cancel result unknown during exit")
        # A successful market close may expire its close-all protection while
        # the next algo snapshot still lags. Reconcile before attempting DELETE.
        try:
            await self.sync_triggered_stops()
        except Exception:
            return self._lock("stop state unavailable after flatten")
        for protection in self.store.active_protections():
            client_id = protection["client_algo_id"]
            cleanup, first_attempt = self.store.prepare_stop_cleanup(client_id)
            if not first_attempt:
                # sync_triggered_stops above retires verified terminal stops.
                # A prior cancellation may have reached the venue; query on
                # subsequent passes rather than blindly repeating DELETE.
                return self._pending_stop_cleanup(cleanup, client_id)
            try:
                await self.exchange.cancel_stop(client_id)
                self.store.record_protection(client_id, "CANCELED")
            except Exception as exc:
                return self._pending_stop_cleanup(cleanup, client_id, exc)
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
        if not await self.reconcile(allow_locked=True):
            return False
        self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            if not was_locked:
                self.store.set_engine_state("FLAT")
            self.store.close_epoch(epoch_id)
            self.store.conn.execute("COMMIT")
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise
        return True

    async def _resolve_close(self, client_id: str, response=None):
        for _ in range(5):
            if response and response.get("status") in ("FILLED", "CANCELED", "EXPIRED"):
                return response
            try:
                response = await self.exchange.query_order(client_id)
            except Exception:
                response = None
            await asyncio.sleep(0.2)
        return None
