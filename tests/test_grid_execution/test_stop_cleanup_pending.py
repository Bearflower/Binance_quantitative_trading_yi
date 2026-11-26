import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest

from shared.binance_api import BinanceAPIError
from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.exchange import GridExchange
from strategies.grid.execution.main import ExecutorService
from strategies.grid.execution.risk import RiskAssessment, RiskStatus
from strategies.grid.execution.store import DecisionStore, migrate


@pytest.mark.asyncio
async def test_stop_delete_uses_single_attempt_transport():
    client = AsyncMock()
    client.use_unified_account = True
    client._request.return_value = {"clientAlgoId": "eg-stop", "algoStatus": "NEW"}
    client._request_once.return_value = {"complete": True}
    assert (await GridExchange(client).cancel_stop("eg-stop"))["complete"]
    client._request_once.assert_awaited_once_with(
        "DELETE", "/papi/v1/um/algo/order", {"clientAlgoId": "eg-stop"}, signed=True)
    assert all(call.args[0] == "GET" for call in client._request.call_args_list)


@pytest.mark.asyncio
async def test_flat_stop_cleanup_survives_restart_without_repeating_delete(tmp_path, monkeypatch):
    path = tmp_path / "journal"
    migrate(path)
    store = DecisionStore(path)
    clock = [1800000000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    epoch = store.create_epoch("old")
    store.conn.execute("INSERT INTO protection VALUES('eg-stop',?,'SELL','1600','NEW','9')", (epoch,))
    store.set_engine_state("ACTIVE")
    exchange = AsyncMock()
    exchange.query_stop.return_value = {"clientAlgoId": "eg-stop", "algoStatus": "NEW", "actualOrderId": ""}
    exchange.position.return_value = [{"positionSide": "BOTH", "positionAmt": "0"}]
    exchange.open_orders.return_value = exchange.open_stops.return_value = []
    exchange.cancel_stop.side_effect = BinanceAPIError(-2011, "Unknown order sent.")
    try:
        assert not await GridEngine(store, exchange).exit_all(epoch)
        assert store.engine_state()["state"] == "EXITING"
        assert store.active_protections()
        store.close()
        store = DecisionStore(path)
        clock[0] += 61
        assert not await GridEngine(store, exchange).exit_all(epoch)
        exchange.cancel_stop.assert_awaited_once_with("eg-stop")
        assert store.engine_state()["state"] == "EXITING"
        clock[0] += 61
        exchange.query_stop.return_value = {"clientAlgoId": "eg-stop", "algoStatus": "EXPIRED", "actualOrderId": ""}
        assert await GridEngine(store, exchange).exit_all(epoch)
        assert store.engine_state()["state"] == "FLAT"
        assert store.active_epoch_id() is None
        assert not store.active_protections()
        exchange.close_position.assert_not_awaited()
        exchange.cancel_stop.assert_awaited_once()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unconfirmed_stop_cleanup_eventually_locks_with_evidence(tmp_path, monkeypatch):
    path = tmp_path / "journal"
    migrate(path)
    store = DecisionStore(path)
    clock = [1800000000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    epoch = store.create_epoch("old")
    store.conn.execute("INSERT INTO protection VALUES('eg-stop',?,'SELL','1600','NEW','9')", (epoch,))
    store.set_engine_state("ACTIVE")
    exchange = AsyncMock()
    exchange.query_stop.return_value = {"clientAlgoId": "eg-stop", "algoStatus": "NEW"}
    exchange.position.return_value = [{"positionSide": "BOTH", "positionAmt": "0"}]
    exchange.cancel_stop.side_effect = TimeoutError("reply lost")
    try:
        assert not await GridEngine(store, exchange).exit_all(epoch)
        assert store.engine_state()["state"] == "EXITING"
        clock[0] += 301
        assert not await GridEngine(store, exchange).exit_all(epoch)
        assert store.engine_state()["state"] == "LOCKED"
        exchange.cancel_stop.assert_awaited_once()
        row = store.conn.execute("SELECT * FROM protection_cleanup WHERE client_algo_id='eg-stop'").fetchone()
        assert "reply lost" in row["last_error"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_exit_retry_retains_rebuild_signal_and_obeys_cooldown(tmp_path, monkeypatch):
    import runpy
    path = tmp_path / "journal"
    migrate(path)
    store = DecisionStore(path)
    now = int(time.time() * 1000)
    decision = runpy.run_path("tests/test_grid_execution/test_start.py")["_decision"](store, now)
    epoch = store.create_epoch("old")
    store.set_engine_state("EXITING", "awaiting stop terminal confirmation")
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    service = ExecutorService.__new__(ExecutorService)
    service.store = store
    service.guard = asyncio.Lock()
    service.engine = Mock(exit_all=AsyncMock(return_value=False),
                          enforce_limits=AsyncMock(return_value=True))
    service._risk = AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    service._write_heartbeat = Mock()
    try:
        await service.tick()
        await service.tick()
        service.engine.exit_all.assert_awaited_once_with(epoch)
        clock[0] += 61
        async def finish(_):
            store.close_epoch(epoch)
            store.set_engine_state("FLAT")
            return True
        service.engine.exit_all.side_effect = finish
        await service.tick()
        assert store.next_decision()["decision_id"] == decision["decision_id"]
        assert store.engine_state()["state"] == "FLAT"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_locked_cleanup_retries_even_when_account_risk_is_allow(tmp_path):
    path = tmp_path / "journal"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("old")
    store.set_engine_state("LOCKED", "stop cancellation uncertain")
    service = ExecutorService.__new__(ExecutorService)
    service.store = store
    service.guard = asyncio.Lock()
    service.engine = Mock(exit_all=AsyncMock(return_value=False),
                          enforce_limits=AsyncMock(return_value=True))
    service._risk = AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    service._write_heartbeat = Mock()
    try:
        await service.tick()
        service.engine.exit_all.assert_awaited_once_with(epoch)
        assert store.engine_state()["state"] == "LOCKED"
    finally:
        store.close()
