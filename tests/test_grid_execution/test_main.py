from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution.main import ExecutorService, run_main
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
