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

from .contracts import ExecutionSettings, decode_feasibility, decode_grid_params
from .engine import GridEngine
from .exchange import GridExchange
from .fills import FillCoordinator
from .planner import HardLimits, build_plan
from .policy import EXIT_STATES, TRADABLE_STATES
from .risk import RiskLimits, RiskStatus
from .risk_tracker import RiskTracker
from .store import DecisionStore, ExecutionLock
from .stream import UserStream


logger = logging.getLogger(__name__)


class ExecutorService:
    def __init__(self, settings: ExecutionSettings, store: DecisionStore,
                 exchange: GridExchange, env: Mapping[str, str]):
        self.settings = settings
        self.store = store
        self.exchange = exchange
        self.env = env
        self.engine = GridEngine(store, exchange)
        self.risk_tracker = RiskTracker(store, exchange, RiskLimits(
            settings.max_daily_loss_usdt, settings.max_drawdown_usdt))
        self.guard = asyncio.Lock()
        self.filters = None
        self.heartbeat = Path(env.get("GRID_EXECUTION_HEARTBEAT",
                                      "/app/data/grid_execution_heartbeat.json"))
        self.stream = UserStream(
            exchange, settings.mode, self._on_event, self._on_reconnect,
            self._on_disconnect, self._on_idle,
            testnet_base=env.get("GRID_TESTNET_WS_BASE"))
        self._risk_result = None
        self._last_risk_ms = 0

    async def _on_reconnect(self):
        async with self.guard:
            self.filters = await self.exchange.verify_account(
                self.settings.expected_leverage)
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
                    await self.engine.pause(
                        epoch, stop_loss_low=Decimal(context["stop_loss_low"]),
                        stop_loss_high=Decimal(context["stop_loss_high"]))
                else:
                    self.engine._lock("user stream disconnected without stop context")
                    await self.engine.exit_all(epoch)

    async def _on_idle(self):
        async with self.guard:
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
                    FillCoordinator(self.store).record(event)
                except Exception:
                    self.engine._lock("unowned order event")
                return
            context = self.store.epoch(epoch)
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

    async def _risk(self, now_ms: int):
        if self._risk_result is None or now_ms - self._last_risk_ms >= 30_000:
            self._risk_result = await self.risk_tracker.assess(now_ms)
            self._last_risk_ms = now_ms
        return self._risk_result

    async def _plan(self, decision: dict):
        params = decode_grid_params(decision["grid_params"])
        feasibility = decode_feasibility(decision["feasibility"])
        self.filters = await self.exchange.verify_account(
            self.settings.expected_leverage)
        price = await self.exchange.ticker_price()
        limits = HardLimits(self.settings.max_margin_usdt,
                            self.settings.max_notional_usdt,
                            self.settings.max_open_orders,
                            self.settings.expected_leverage)
        plan = build_plan(params, feasibility, price, self.filters, limits)
        if decision.get("margin_used") is None or plan.max_margin_usdt > Decimal(
                str(decision["margin_used"])):
            raise ValueError("plan exceeds signal margin")
        return plan

    def _write_heartbeat(self, now_ms: int):
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at_ms": now_ms,
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
            if epoch is not None and not await self.engine.exit_all(epoch):
                return False
            target = exit_items[-1]
        else:
            if epoch is not None:
                context = self.store.epoch(epoch)
                if not context or not context["stop_loss_low"] or not context["stop_loss_high"]:
                    self.engine._lock("backlog has no stop context")
                    return False
                if not await self.engine.pause(
                        epoch, stop_loss_low=Decimal(context["stop_loss_low"]),
                        stop_loss_high=Decimal(context["stop_loss_high"])):
                    return False
            target = pending[-2]
        self.store.acknowledge_through(target["decision_id"])
        return True

    async def tick(self):
        now_ms = int(time.time() * 1000)
        async with self.guard:
            risk = await self._risk(now_ms)
            epoch = self.store.active_epoch_id()
            if risk.status != RiskStatus.ALLOW:
                self.engine._lock("risk: " + ",".join(risk.reasons))
                if epoch is not None:
                    await self.engine.exit_all(epoch)
                self._write_heartbeat(now_ms)
                return
            pending = self.store.pending_decisions()
            if not await self._drain_backlog(pending, epoch):
                self._write_heartbeat(now_ms)
                return
            decision = self.store.next_decision()
            if decision is None:
                self._write_heartbeat(now_ms)
                return
            tradable = (decision["state"] in TRADABLE_STATES and
                        (decision.get("feasibility") or {}).get("feasible"))
            if tradable and not self.stream.ready:
                self._write_heartbeat(now_ms)
                return
            context = self.store.epoch(epoch) if epoch is not None else None
            try:
                plan = await self._plan(decision) if tradable else None
                params = decision.get("grid_params") or {}
                stop_low = Decimal(str(params.get("stop_loss_low") or
                                       (context or {}).get("stop_loss_low") or "1"))
                stop_high = Decimal(str(params.get("stop_loss_high") or
                                        (context or {}).get("stop_loss_high") or "2"))
                await self.engine.apply_decision(
                    decision, plan, risk,
                    leverage=self.settings.expected_leverage, now_ms=now_ms,
                    stop_loss_low=stop_low, stop_loss_high=stop_high)
            except Exception as exc:
                logger.exception("grid decision failed: %s", type(exc).__name__)
                self.engine._lock("decision handling failed")
                epoch = self.store.active_epoch_id()
                if epoch is not None:
                    await self.engine.exit_all(epoch)
            self._write_heartbeat(now_ms)

    async def run_forever(self):
        await self._on_reconnect()
        stream_task = asyncio.create_task(self.stream.run_forever())
        try:
            while True:
                if stream_task.done():
                    await stream_task
                    raise RuntimeError("user stream task stopped")
                await self.tick()
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
