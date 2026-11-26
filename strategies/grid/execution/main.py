"""Separate, opt-in ETHUSDT order-grid process.

The Compose profile and GRID_EXECUTION_MODE both default to disabled. A
configured process still requires a migrated journal and complete risk facts.
"""

import asyncio
import json
import logging
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional

from shared.notification_time import beijing_time

from .contracts import ExecutionSettings, decode_feasibility, decode_grid_params
from .engine import GridEngine
from .exchange import GridExchange
from .fills import FillCoordinator
from .planner import HardLimits, PlanLimitExceeded, build_plan
from .policy import EXIT_STATES, TRADABLE_STATES
from .position_recovery import PositionLockRecovery
from .risk import RiskLimits, RiskStatus
from .risk_tracker import RiskTracker
from .store import DecisionStore, ExecutionLock
from .signal_receipt import SignalReceiptWriter
from .stream import UserStream
from .stream_recovery import HANDOFF_PAUSE, RECOVERABLE_PAUSES, STREAM_PAUSE, StreamRecovery
from strategies.grid.realtime.delivery import WebhookSingleSender


logger = logging.getLogger(__name__)
LOCK_ALERT_REPEAT_SECONDS = 24 * 60 * 60


class ExecutorService:
    def __init__(self, settings: ExecutionSettings, store: DecisionStore,
                 exchange: GridExchange, env: Mapping[str, str]):
        self.settings = settings
        self.store = store
        self.exchange = exchange
        self.env = env
        self.engine = GridEngine(store, exchange, HardLimits(
            settings.max_margin_usdt, settings.max_notional_usdt,
            settings.max_open_orders, settings.expected_leverage))
        enabled = env.get('GRID_TRAILING_ENABLED', 'false').strip().lower()
        if enabled not in ('true', 'false'):
            raise ValueError('GRID_TRAILING_ENABLED must be true or false')
        if enabled == 'true':
            from .trailing import TrailingController
            self.engine.trailing = TrailingController(self.engine)
        self.trailing = self.engine.trailing
        self.position_recovery = PositionLockRecovery(store, exchange)
        self.stream_recovery = StreamRecovery(self.trailing) if self.trailing else None
        self.risk_tracker = RiskTracker(store, exchange, RiskLimits(
            settings.max_daily_loss_usdt, settings.max_drawdown_usdt))
        self.guard = asyncio.Lock()
        self.filters = None
        self.heartbeat = Path(env.get("GRID_EXECUTION_HEARTBEAT",
                                      "/app/data/grid_execution_heartbeat.json"))
        self.stream = UserStream(
            exchange, settings.mode, self._on_event, self._on_reconnect,
            self._on_disconnect, self._on_idle,
            testnet_base=env.get("GRID_TESTNET_WS_BASE"), pm=settings.use_unified_account)
        if self.trailing is not None:
            self.trailing.submission_allowed = lambda: self.stream.ready
        self._risk_result = None
        self._last_risk_ms = 0
        self.alert_sender = WebhookSingleSender()
        self._last_alert_token = None
        self._last_alert_at = 0.0
        self._next_alert_attempt = 0.0
        self.signal_receipts = SignalReceiptWriter()

    async def _on_reconnect(self):
        async with self.guard:
            self.filters = await self.exchange.verify_account(
                self.settings.expected_leverage)
            self.account_identity = ("pm" if self.settings.use_unified_account else
                                     await self.exchange.journal_identity())
            self.store.bind_account(self.account_identity)
            epoch = self.store.active_epoch_id()
            self._risk_result = None  # Refresh account risk after reconnect.
            if epoch is None and self.store.engine_state()["state"] == "LOCKED":
                # The stream may collect facts, while the recovery checks still
                # fence any strategy action behind the persisted lock.
                self.position_recovery.reset()
                return
            if self.engine.trailing is not None and epoch is not None:
                self.engine.trailing.ensure_epoch(epoch, self.filters)
            if not await self.engine.reconcile():
                epoch = self.store.active_epoch_id()
                if epoch is not None:
                    await self.engine.exit_all(epoch)
                raise RuntimeError("exchange reconciliation failed on reconnect")

    async def _on_disconnect(self):
        async with self.guard:
            epoch = self.store.active_epoch_id()
            if epoch is not None and self.store.engine_state()["state"] == "ACTIVE":
                context = self.store.epoch(epoch)
                if context["stop_loss_low"] and context["stop_loss_high"]:
                    logger.info("grid stream pause requested: epoch=%s stage=%s",
                                epoch, self.stream.disconnect_stage)
                    if self.stream_recovery:
                        self.stream_recovery.capture(epoch)
                    await self.engine.pause(
                        epoch, stop_loss_low=Decimal(context["stop_loss_low"]),
                        stop_loss_high=Decimal(context["stop_loss_high"]),
                        reason=STREAM_PAUSE if self.stream_recovery else None, trigger=STREAM_PAUSE)
                else:
                    self.engine._lock("user stream disconnected without stop context")
                    await self.engine.exit_all(epoch)

    async def _on_idle(self):
        async with self.guard:
            if self.store.engine_state()["state"] == "LOCKED" and self.store.active_epoch_id() is None:
                return
            if not await self.engine.reconcile():
                epoch = self.store.active_epoch_id()
                if epoch is not None:
                    await self.engine.exit_all(epoch)
                raise RuntimeError("user stream idle reconciliation failed")

    async def _on_event(self, event: dict):
        if event.get("e") != "ORDER_TRADE_UPDATE":
            return
        async with self.guard:
            epoch = self.store.active_epoch_id()
            if epoch is None:
                # Exit trades may arrive after FLAT. Retain the exact fact in
                # the journal; any unknown client ID is a safety violation.
                try:
                    event = await self.engine.normalize_order_event(event)
                    FillCoordinator(self.store).record(event)
                except Exception as exc:
                    self.store.record_event_failure(event, exc)
                    logger.exception("grid late order event reconciliation failed")
                    owned = self.store.get_order(str((event.get("o") or {}).get("c")))
                    self.engine._lock("known order event reconciliation failed"
                                      if owned else "unowned order event")
                return
            context = self.store.epoch(epoch)
            if (context and context["stop_loss_low"] and context["stop_loss_high"]
                    and self._signal_stale(int(time.time() * 1000))
                    and self.store.engine_state()["state"] == "ACTIVE"):
                await self.engine.pause(epoch, stop_loss_low=Decimal(context["stop_loss_low"]),
                                        stop_loss_high=Decimal(context["stop_loss_high"]), trigger="latest_signal_unusable")
            if (not context or not context["levels_json"] or
                    not context["stop_loss_low"] or not context["stop_loss_high"] or
                    self.filters is None):
                self.engine._lock("missing active grid context")
                await self.engine.exit_all(epoch)
                return
            levels = tuple(Decimal(value) for value in json.loads(context["levels_json"]))
            await self.engine.on_order_update(
                event, epoch_id=epoch, levels=levels, filters=self.filters,
                stop_loss_low=Decimal(context["stop_loss_low"]),
                stop_loss_high=Decimal(context["stop_loss_high"]))

    def _signal_stale(self, now_ms):
        latest = self.store.conn.execute(
            "SELECT payload_json FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()
        if latest is None:
            return True
        decision = json.loads(latest["payload_json"])
        return (not decision.get("executable") or
                not 0 <= now_ms - decision["calculated_at_ms"] <= 90 * 60_000)

    async def _risk(self, now_ms: int):
        if self._risk_result is None or now_ms - self._last_risk_ms >= 30_000:
            self._risk_result = await self.risk_tracker.assess(now_ms)
            self._last_risk_ms = now_ms
        return self._risk_result

    async def _plan(self, decision: dict):
        self._plan_receipt = None
        params = decode_grid_params(decision["grid_params"])
        feasibility = decode_feasibility(decision["feasibility"])
        self.filters = await self.exchange.verify_account(
            self.settings.expected_leverage)
        price = await self.exchange.ticker_price()
        limits = HardLimits(self.settings.max_margin_usdt,
                            self.settings.max_notional_usdt,
                            self.settings.max_open_orders,
                            self.settings.expected_leverage)
        signal_margin = Decimal(str(decision["margin_used"]))
        account = await self.exchange.account_info()
        available = Decimal(str(account["totalAvailableBalance"]))
        if not signal_margin.is_finite() or signal_margin <= 0 or not available.is_finite():
            raise ValueError("invalid execution margin evidence")
        budget = min(self.settings.max_margin_usdt, available, signal_margin)
        self._plan_receipt = dict(decision_id=decision.get('decision_id'),
            checked_at_ms=int(time.time() * 1000),
            budget=str(budget), leverage=self.settings.expected_leverage,
            max_margin=str(self.settings.max_margin_usdt),
            min_notional=str(self.filters.min_notional), status='CHECKING')
        try:
            if decision.get('sizing_version') == 2:
                if not feasibility.feasible:
                    raise PlanLimitExceeded('structural or profit constraints failed')
                from .sizing import size_grid
                plan = size_grid(params, price, self.filters, limits,
                    advisory_margin=signal_margin, available_margin=available,
                    min_grid_count=decision['execution_constraints']['min_grid_count'])
                self._plan_receipt.update(plan.sizing)
            else:
                plan = build_plan(params, feasibility, price, self.filters, limits,
                                  margin_budget_usdt=budget)
        except ValueError as exc:
            self._plan_receipt.update(status='REJECTED', reason=str(exc))
            self._plan_receipt.update(getattr(exc, 'evidence', {}))
            raise
        self._plan_receipt.update(status='PASSED', quantity=str(plan.orders[0].quantity),
                                  order_count=len(plan.orders))
        logger.info("grid execution sized: margin_budget=%s leverage=%s quantity=%s",
                    self._plan_receipt['budget'], self.settings.expected_leverage, plan.orders[0].quantity)
        return plan

    def _write_heartbeat(self, now_ms: int):
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at_ms": now_ms, "account_identity": getattr(self, "account_identity", None),
                   "state": self.store.engine_state()["state"],
                   "stream_ready": self.stream.ready,
                   "risk_status": (self._risk_result.status.value
                                   if self._risk_result else "UNKNOWN")}
        temporary = self.heartbeat.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(str(temporary), str(self.heartbeat))

    async def _drain_backlog(self, pending: list[dict], epoch: Optional[int]) -> bool:
        """Honor an exit before newer signals; never open from superseded ones."""
        if len(pending) < 2:
            return True
        exit_items = [row for row in pending if row["state"] in EXIT_STATES]
        if exit_items:
            target = exit_items[-1]
            if epoch is not None and not await self.engine.exit_all(epoch, trigger=f"backlog_exit:{target['state']}"):
                return False
        else:
            trailing = getattr(self, 'trailing', None)
            now_ms = int(time.time() * 1000)
            if (epoch is not None and trailing is not None
                    and all(row.get('executable') and trailing.keep_active(row, epoch)
                            and (row.get('feasibility') or {}).get('feasible')
                            and 0 <= now_ms - row['calculated_at_ms'] <= 90 * 60_000
                            for row in pending)):
                self.store.acknowledge_through(pending[-2]['decision_id'])
                return True
            if epoch is not None:
                context = self.store.epoch(epoch)
                if not context or not context["stop_loss_low"] or not context["stop_loss_high"]:
                    self.engine._lock("backlog has no stop context")
                    return False
                if not await self.engine.pause(
                        epoch, stop_loss_low=Decimal(context["stop_loss_low"]),
                        stop_loss_high=Decimal(context["stop_loss_high"]), trigger="signal_backlog"):
                    return False
            target = pending[-2]
        if self.store.stopped_epoch(epoch):
            return False  # Reload the epoch and post-stop decisions on the next tick.
        self.store.acknowledge_through(target["decision_id"])
        return True

    async def _alert_if_needed(self):
        state = self.store.engine_state()
        if state["state"] != "LOCKED":
            self._last_alert_token = None
            return
        token = state.get("lock_reason") or "unknown reason"
        now = time.monotonic()
        if (token == self._last_alert_token
                and now - self._last_alert_at < LOCK_ALERT_REPEAT_SECONDS):
            return
        if token == getattr(self, "_attempt_alert_token", None) and now < getattr(self, "_next_alert_attempt", 0):
            return
        self._attempt_alert_token = token
        self._next_alert_attempt = now + 30
        logger.error("grid executor locked: %s", token)
        try:
            delivered = await self.alert_sender(f"⚠️ 【ETHUSDT 实盘网格】已锁定，禁止新开仓\n"
                                                f"告警时间（北京时间） {beijing_time()}\n"
                                                f"锁定原因 {token}\n请核查子账号净仓、未成交委托和止损保护单。")
            if delivered is True:
                self._last_alert_token = token
                self._last_alert_at = now
        except Exception:
            logger.exception("grid executor alert delivery failed")

    async def _manage_trailing(self, epoch, *, move):
        try:
            self.trailing.ensure_epoch(epoch, self.filters)
            if not move:
                return await self.trailing.guard(epoch)
            moved = await self.trailing.tick(epoch, self.filters)
            state = self.store.engine_state()
            if moved and state['state'] == 'ACTIVE' and state['lock_reason'] in RECOVERABLE_PAUSES:
                await self.stream_recovery.restore(epoch, self.filters)
            return moved
        except Exception:
            logger.exception('grid movement handling failed')
            self.engine._lock('grid movement handling failed')
            if self.store.active_epoch_id() is not None:
                await self.engine.exit_all(epoch)
            return False

    async def _maintain_active_grid(self, epoch):
        if getattr(self, "trailing", None) is not None:
            await self._manage_trailing(epoch, move=True)
        if self.store.active_epoch_id() == epoch:
            await self.engine.recover_obligations(epoch, self.filters)

    async def _grid_is_flat(self):
        positions = await self.exchange.position()
        return (len(positions) == 1 and positions[0].get("positionSide") == "BOTH"
                and Decimal(positions[0]["positionAmt"]) == 0)

    async def _advance_grid_handoff(self, epoch, decision, context):
        """Keep servicing the original epoch until a confirmed refresh can retire it flat."""
        if self.trailing.pending(epoch) or not await self._grid_is_flat():
            await self._maintain_active_grid(epoch)
            return

        fills_before = len(self.store.all_fills())
        if not await self.engine.reconcile():
            return
        if (self.store.engine_state()["state"] != "ACTIVE" or self.store.stopped_epoch(epoch)
                or not self.stream.ready or not self.trailing.refresh_confirmed(decision, epoch)):
            return
        if len(self.store.all_fills()) != fills_before or not await self._grid_is_flat():
            await self._maintain_active_grid(epoch)
            return

        proposed = decode_grid_params(decision["grid_params"])
        price = await self.exchange.ticker_price()
        if not proposed.lower_boundary < price < proposed.upper_boundary:
            await self._maintain_active_grid(epoch)
            return
        if not self.stream.ready or not self.trailing.refresh_confirmed(decision, epoch):
            return

        # Reuse the durable pause journal. If a cancel races with a fill or
        # the process stops here, recovery can restore the original prices.
        self.stream_recovery.capture(epoch, reason=HANDOFF_PAUSE)
        if not await self.engine.pause(
                epoch, stop_loss_low=Decimal(context["stop_loss_low"]),
                stop_loss_high=Decimal(context["stop_loss_high"]), reason=HANDOFF_PAUSE):
            return
        if self.store.stopped_epoch(epoch) or self.store.engine_state()["state"] != "PAUSED":
            return
        try:
            ready = (
                len(self.store.all_fills()) == fills_before and await self._grid_is_flat()
                and not await self.exchange.open_orders() and not await self.exchange.open_stops()
                and not self.store.active_orders() and not self.store.active_protections()
            )
            if ready and self.stream.ready and self.trailing.refresh_confirmed(decision, epoch):
                # Check sizing against the balance released by confirmed
                # cancellations. The normal opening path will size it again.
                await self._plan(decision)
                if self.stream.ready and self.trailing.refresh_confirmed(decision, epoch):
                    self.store.retire_flat_handoff(epoch, HANDOFF_PAUSE)
                    logger.info("grid handoff retired flat epoch: epoch=%s decision=%s", epoch, decision["decision_id"])
                    return  # New grid starts only through the normal signal path.
        except ValueError as exc:
            logger.info("grid handoff deferred: epoch=%s reason=%s", epoch, exc)
        logger.info("grid handoff interrupted; retaining original epoch: epoch=%s", epoch)
        self.store.acknowledge_after_action(decision["decision_id"], epoch)

    async def tick(self):
        try:
            await self._tick()
        finally:
            writer = getattr(self, 'signal_receipts', None)
            if writer is not None:
                try:
                    await asyncio.wait_for(writer.capture(self), timeout=8)
                except Exception as exc:
                    logger.warning('grid signal receipt unavailable: %s', type(exc).__name__)

    async def _tick(self):
        now_ms = int(time.time() * 1000)
        trailing = getattr(self, 'trailing', None)
        async with self.guard:
            epoch = self.store.active_epoch_id()
            state = self.store.engine_state()
            if epoch is None and state["state"] == "LOCKED":
                recoverable = self.position_recovery.can_recover(state)
                if recoverable and self.stream.ready and self.position_recovery.due(now_ms):
                    self._risk_result = None
                risk = await self._risk(now_ms)
                if recoverable:
                    if risk.status == RiskStatus.EXIT_LOCKED or "account_transfer" in risk.reasons:
                        self.engine._lock("risk: " + ",".join(risk.reasons))
                    else:
                        await self.position_recovery.check(state, risk, self.stream, int(time.time() * 1000))
                self._write_heartbeat(int(time.time() * 1000))
                return
            risk = await self._risk(now_ms)
            cleanup_wait = (state["state"] == "LOCKED" or
                            (state["state"] == "EXITING" and state["lock_reason"] ==
                             "awaiting stop terminal confirmation"))
            if (epoch is not None and cleanup_wait
                    and time.monotonic() < getattr(self, "_cleanup_after", 0)):
                self._write_heartbeat(now_ms)
                return
            if epoch is not None and cleanup_wait:
                self._cleanup_after = time.monotonic() + 60
            if risk.status != RiskStatus.ALLOW:
                self.engine._lock("risk: " + ",".join(risk.reasons))
                if epoch is not None:
                    await self.engine.exit_all(epoch)
                self._write_heartbeat(now_ms)
                return
            if epoch is not None and self.store.engine_state()["state"] in ("LOCKED", "EXITING"):
                await self.engine.exit_all(epoch)
                # A delayed REPLAN exit must retain its pending decision.
                # Actual stop settlement already sets its own signal cutoff.
                self._write_heartbeat(now_ms)
                return
            if epoch is not None and not await self.engine.enforce_limits(epoch):
                self._write_heartbeat(now_ms)
                return
            if (epoch is not None and trailing is not None
                    and self.store.engine_state()['state'] in ('ACTIVE', 'PAUSED')):
                if not await self._manage_trailing(epoch, move=False):
                    self._write_heartbeat(now_ms)
                    return
            if epoch is not None and self._signal_stale(int(time.time() * 1000)):
                context = self.store.epoch(epoch)
                if self.store.engine_state()["state"] == "ACTIVE":
                    await self.engine.pause(epoch, stop_loss_low=Decimal(context["stop_loss_low"]),
                                            stop_loss_high=Decimal(context["stop_loss_high"]), trigger="latest_signal_unusable")
            pending = self.store.pending_decisions()
            state = self.store.engine_state()
            if (epoch is not None and trailing and state['state'] == 'PAUSED'
                    and state['lock_reason'] in RECOVERABLE_PAUSES
                    and self.stream_recovery.signals_allow_resume(epoch, pending)):
                if not self.stream.ready or not await self.stream_recovery.resume(epoch, self.filters):
                    self._write_heartbeat(now_ms)
                    return
            if not await self._drain_backlog(pending, epoch):
                self._write_heartbeat(now_ms)
                return
            decision = self.store.next_decision()
            if decision is None:
                if (epoch is not None and self.store.engine_state()["state"] == "ACTIVE"
                        and not self._signal_stale(now_ms) and self.stream.ready):
                    await self._maintain_active_grid(epoch)
                self._write_heartbeat(now_ms)
                return
            tradable = (decision["state"] in TRADABLE_STATES and
                        (decision.get("feasibility") or {}).get("feasible"))
            if tradable and not self.stream.ready:
                self._write_heartbeat(now_ms)
                return
            context = self.store.epoch(epoch) if epoch is not None else None
            try:
                holding = (tradable and epoch is not None and trailing is not None
                           and trailing.keep_active(decision, epoch))
                params = context if holding else (decision.get("grid_params") or {})
                stop_low = Decimal(str(params.get("stop_loss_low") or
                                       (context or {}).get("stop_loss_low") or "1"))
                stop_high = Decimal(str(params.get("stop_loss_high") or
                                        (context or {}).get("stop_loss_high") or "2"))
                if (holding and self.store.engine_state()["lock_reason"] is None
                        and trailing.refresh_confirmed(decision, epoch)):
                    await self._advance_grid_handoff(epoch, decision, context)
                    self._write_heartbeat(int(time.time() * 1000))
                    return
                if holding:
                    self.filters = await self.exchange.verify_account(self.settings.expected_leverage)
                    trailing.ensure_epoch(epoch, self.filters)
                if (tradable and not holding and epoch is None
                        and isinstance(getattr(self, "exchange", None), GridExchange)
                        and not self.exchange.can_start_batch(min(
                            int(params['grid_count']) + 1, self.settings.max_open_orders))):
                    # Do not consume the decision or create a partial grid while
                    # waiting for request headroom. The next tick refreshes risk.
                    self._write_heartbeat(int(time.time() * 1000))
                    return
                plan = await self._plan(decision) if tradable and not holding else None
                await self.engine.apply_decision(
                    decision, plan, risk,
                    leverage=self.settings.expected_leverage, now_ms=int(time.time() * 1000),
                    stop_loss_low=stop_low, stop_loss_high=stop_high)
                current = self.store.active_epoch_id()
                if (trailing is not None and current is not None
                        and self.store.engine_state()['state'] == 'ACTIVE'
                        and self.stream.ready and not self._signal_stale(now_ms)):
                    await self._manage_trailing(current, move=True)
            except PlanLimitExceeded as exc:
                logger.info("grid plan skipped: %s", exc)
                if epoch is None or await self.engine.pause(
                        epoch, stop_loss_low=stop_low,
                        stop_loss_high=stop_high, trigger="plan_budget"):
                    self.store.acknowledge_after_action(decision["decision_id"], epoch)
            except Exception as exc:
                logger.exception("grid decision failed: %s", type(exc).__name__)
                self.engine._lock("decision handling failed")
                epoch = self.store.active_epoch_id()
                if epoch is not None:
                    await self.engine.exit_all(epoch)
            self._write_heartbeat(now_ms)

    def _waiting_for_operator(self):
        state = self.store.engine_state()
        return (state["state"] == "LOCKED" and self.store.active_epoch_id() is None
                and not self.position_recovery.can_recover(state))

    async def run_forever(self):
        while True:
            while self._waiting_for_operator():
                await self.tick()
                await self._alert_if_needed()
                await asyncio.sleep(5)
            if self.store.engine_state()["state"] != "LOCKED":
                await self._on_reconnect()
            stream_task = asyncio.create_task(self.stream.run_forever())
            try:
                while not self._waiting_for_operator():
                    if stream_task.done():
                        await stream_task
                        raise RuntimeError("user stream task stopped")
                    await self.tick()
                    await self._alert_if_needed()
                    await asyncio.sleep(5)
            finally:
                stream_task.cancel()
                try:
                    await stream_task
                except asyncio.CancelledError:
                    pass


async def run_main(env: Mapping[str, str]) -> int:
    settings = ExecutionSettings.from_env(env)
    if settings.mode == "off":
        logger.info("grid executor is off")
        return 0
    if settings.mode == "testnet" and not env.get("GRID_TESTNET_WS_BASE"):
        raise ValueError("GRID_TESTNET_WS_BASE required for testnet")
    db_path = Path(env.get("GRID_EXECUTION_DB", "/app/data/grid_execution.sqlite3"))
    lock_path = Path(env.get("GRID_EXECUTION_LOCK", "/app/data/grid_execution.lock"))
    with ExecutionLock(lock_path):
        store = DecisionStore(db_path)
        exchange = GridExchange.from_settings(settings)
        try:
            await ExecutorService(settings, store, exchange, env).run_forever()
        finally:
            await exchange.close()
            store.close()
    return 0


def healthcheck(env: Mapping[str, str]) -> int:
    path = Path(env.get("GRID_EXECUTION_HEARTBEAT",
                        "/app/data/grid_execution_heartbeat.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age_ms = int(time.time() * 1000) - int(data["updated_at_ms"])
        return 0 if (0 <= age_ms <= 120_000 and data["state"] != "LOCKED"
                     and data["risk_status"] == "ALLOW" and data["stream_ready"]) else 1
    except (OSError, ValueError, KeyError, TypeError):
        return 1


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if "--healthcheck" in sys.argv:
        raise SystemExit(healthcheck(os.environ))
    raise SystemExit(asyncio.run(run_main(os.environ)))
