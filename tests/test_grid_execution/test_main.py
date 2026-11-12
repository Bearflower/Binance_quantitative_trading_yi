import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from strategies.grid.execution.main import ExecutorService, run_main
from strategies.grid.execution.planner import PlanLimitExceeded
from strategies.grid.execution.risk import RiskStatus
from strategies.grid.execution.store import DecisionStore, migrate


@pytest.mark.asyncio
async def test_off_mode_returns_without_opening_store_or_exchange(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled executor touched exchange")

    monkeypatch.setattr("strategies.grid.execution.main.GridExchange.from_settings",
                        forbidden)
    assert await run_main({}) == 0


@pytest.mark.asyncio
async def test_live_missing_limits_refuses_before_exchange(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("incomplete settings touched exchange")

    monkeypatch.setattr("strategies.grid.execution.main.GridExchange.from_settings",
                        forbidden)
    with pytest.raises(ValueError, match="GRID_SUBACCOUNT_API_KEY"):
        await run_main({"GRID_EXECUTION_MODE": "live"})


@pytest.mark.asyncio
async def test_backlog_exit_preempts_older_tradable_signals(tmp_path):
    path = tmp_path / "backlog.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    ids = []
    for i, state in enumerate(("OSCILLATION", "OSCILLATION",
                               "NORMAL_STRONG_TREND", "OSCILLATION")):
        ids.append(store.publish({"symbol": "ETHUSDT", "state": state,
            "calculated_at_ms": 1000 + i, "executable": True,
            "last_closed_kline_ms": {"15m": 900, "1h": 900, "4h": 900}}, "hash"))
    epoch = store.create_epoch(ids[0], levels=(Decimal("1900"),),
                               stop_loss_low=Decimal("1800"),
                               stop_loss_high=Decimal("2100"))
    service = ExecutorService.__new__(ExecutorService)
    service.store = store
    service.engine = type("FakeEngine", (), {"exit_all": AsyncMock(return_value=True),
                                               "pause": AsyncMock(return_value=True)})()
    assert await service._drain_backlog(store.pending_decisions(), epoch)
    service.engine.exit_all.assert_awaited_once_with(epoch)
    service.engine.pause.assert_not_awaited()
    assert store.next_decision()["decision_id"] == ids[-1]
    store.close()


@pytest.mark.asyncio
async def test_backlog_without_exit_pauses_before_skipping(tmp_path):
    path = tmp_path / "backlog.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    for i, state in enumerate(("OSCILLATION", "EARLY_WARNING_15M", "OSCILLATION")):
        store.publish({"symbol": "ETHUSDT", "state": state,
            "calculated_at_ms": 1000 + i, "executable": True,
            "last_closed_kline_ms": {"15m": 900, "1h": 900, "4h": 900}}, "hash")
    epoch = store.create_epoch(store.next_decision()["decision_id"],
                               levels=(Decimal("1900"),),
                               stop_loss_low=Decimal("1800"),
                               stop_loss_high=Decimal("2100"))
    service = ExecutorService.__new__(ExecutorService)
    service.store = store
    service.engine = type("FakeEngine", (), {"exit_all": AsyncMock(return_value=True),
                                               "pause": AsyncMock(return_value=True)})()
    assert await service._drain_backlog(store.pending_decisions(), epoch)
    service.engine.pause.assert_awaited_once_with(
        epoch, stop_loss_low=Decimal("1800"), stop_loss_high=Decimal("2100"))
    assert store.next_decision()["state"] == "OSCILLATION"
    store.close()


@pytest.mark.asyncio
async def test_locked_state_alerts_once_per_reason_and_ignores_send_failure(tmp_path):
    path = tmp_path / "alerts.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    service = ExecutorService.__new__(ExecutorService)
    service.store = store
    service.alert_sender = AsyncMock(side_effect=[False, RuntimeError("webhook down")])
    service._last_alert_token = None
    store.set_engine_state("LOCKED", "foreign order")
    await service._alert_if_needed()
    await service._alert_if_needed()
    service.alert_sender.assert_awaited_once()
    store.set_engine_state("LOCKED", "risk unknown")
    await service._alert_if_needed()
    assert service.alert_sender.await_count == 2
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_over_budget_plan_is_acknowledged_without_lock_or_order(active):
    decision = {"decision_id": "budget-1", "state": "OSCILLATION",
                "feasibility": {"feasible": True}, "grid_params": {
                    "stop_loss_low": "1800", "stop_loss_high": "2200"}}
    store = SimpleNamespace(
        active_epoch_id=lambda: 7 if active else None,
        pending_decisions=lambda: [decision],
        next_decision=lambda: decision,
        epoch=lambda _epoch: {"stop_loss_low": "1800",
                              "stop_loss_high": "2200"},
        acknowledge=lambda _id: None)
    acknowledged = []
    store.acknowledge = acknowledged.append
    engine = SimpleNamespace(
        pause=AsyncMock(return_value=True),
        apply_decision=AsyncMock(),
        exit_all=AsyncMock(),
        _lock=lambda _reason: pytest.fail("budget rejection locked executor"))
    service = ExecutorService.__new__(ExecutorService)
    service.store = store
    service.engine = engine
    service.guard = asyncio.Lock()
    service.stream = SimpleNamespace(ready=True)
    service._risk = AsyncMock(return_value=SimpleNamespace(status=RiskStatus.ALLOW))
    service._plan = AsyncMock(side_effect=PlanLimitExceeded("whole-plan margin exceeds limit"))
    service._drain_backlog = AsyncMock(return_value=True)
    service._write_heartbeat = lambda _now: None
    await service.tick()
    assert acknowledged == ["budget-1"]
    assert engine.pause.await_count == int(active)
    engine.apply_decision.assert_not_awaited()
    engine.exit_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_over_budget_plan_waits_when_active_grid_cannot_pause():
    decision = {"decision_id": "budget-2", "state": "OSCILLATION",
                "feasibility": {"feasible": True}, "grid_params": {
                    "stop_loss_low": "1800", "stop_loss_high": "2200"}}
    acknowledged = []
    service = ExecutorService.__new__(ExecutorService)
    service.store = SimpleNamespace(
        active_epoch_id=lambda: 7,
        pending_decisions=lambda: [decision],
        next_decision=lambda: decision,
        epoch=lambda _epoch: {"stop_loss_low": "1800",
                              "stop_loss_high": "2200"},
        acknowledge=acknowledged.append)
    service.engine = SimpleNamespace(
        pause=AsyncMock(return_value=False),
        apply_decision=AsyncMock(),
        exit_all=AsyncMock(),
        _lock=lambda _reason: pytest.fail("budget rejection locked executor"))
    service.guard = asyncio.Lock()
    service.stream = SimpleNamespace(ready=True)
    service._risk = AsyncMock(return_value=SimpleNamespace(status=RiskStatus.ALLOW))
    service._plan = AsyncMock(side_effect=PlanLimitExceeded("whole-plan margin exceeds limit"))
    service._drain_backlog = AsyncMock(return_value=True)
    service._write_heartbeat = lambda _now: None
    await service.tick()
    assert acknowledged == []
    service.engine.apply_decision.assert_not_awaited()
