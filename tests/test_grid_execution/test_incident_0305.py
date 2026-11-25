"""Replay the production 03:05 signal using a frozen clock and fake exchange."""

import json
import logging
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from strategies.grid.execution.contracts import ExecutionSettings
from strategies.grid.execution.exchange import GridExchange
from strategies.grid.execution.main import ExecutorService
from strategies.grid.execution.planner import ExchangeFilters
from strategies.grid.execution.store import DecisionStore, migrate


@pytest.mark.asyncio
async def test_original_0305_signal_is_skipped_without_lock_orders_or_retry(
        tmp_path, monkeypatch, caplog):
    sample = json.loads((Path(__file__).parents[1] / "fixtures/grid_execution/"
                         "ethusdt_20261114_0305.json").read_text())
    payload = sample["payload"]
    now_ms = payload["calculated_at_ms"] + 3000
    monkeypatch.setattr("strategies.grid.execution.main.time.time", lambda: now_ms / 1000)
    settings = ExecutionSettings(
        mode="live", max_margin_usdt=Decimal(240), max_notional_usdt=Decimal(2400),
        max_daily_loss_usdt=Decimal(15), max_drawdown_usdt=Decimal(30),
        max_open_orders=7, expected_leverage=5)
    exchange = Mock(spec=GridExchange)
    exchange.verify_account = AsyncMock(return_value=ExchangeFilters(
        **{k: Decimal(v) for k, v in sample["replay_metadata"]["filters"].items()}))
    exchange.ticker_price = AsyncMock(
        return_value=Decimal(payload["market_analysis"]["current_price"]))
    exchange.account_info = AsyncMock(return_value={
        "accountEquity": "300", "totalAvailableBalance": "300"})
    exchange.income = AsyncMock(return_value=[])
    exchange.position = AsyncMock(return_value=[{"symbol": "ETHUSDT",
                                                 "positionSide": "BOTH",
                                                 "positionAmt": "0"}])
    writes = ("place_limit", "place_stop", "close_position", "cancel_order", "cancel_stop")
    for name in writes:
        setattr(exchange, name, AsyncMock(side_effect=AssertionError("unexpected trade write")))
    database = tmp_path / "execution.sqlite3"
    heartbeat = tmp_path / "heartbeat.json"
    migrate(database)
    store = DecisionStore(database)
    try:
        assert store.publish(payload, sample["config_hash"]) == sample["decision_id"]
        service = ExecutorService(settings, store, exchange,
                                  {"GRID_EXECUTION_HEARTBEAT": str(heartbeat)})
        service.stream.ready = True
        with caplog.at_level(logging.INFO):
            await service.tick()
            await service.tick()
        assert "grid plan skipped: open order count exceeds limit" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert store.engine_state()["state"] == "FLAT"
        assert store.engine_state()["lock_reason"] is None
        assert store.next_decision() is None
        assert store.active_epoch_id() is None
        assert not store.all_orders()
        state = json.loads(heartbeat.read_text())
        assert (state["state"], state["risk_status"], state["stream_ready"]) == (
            "FLAT", "ALLOW", True)
        exchange.verify_account.assert_awaited_once_with(5)
    finally:
        store.close()

    reopened = DecisionStore(database)
    try:
        assert reopened.publish(payload, sample["config_hash"]) == sample["decision_id"]
        service = ExecutorService(settings, reopened, exchange,
                                  {"GRID_EXECUTION_HEARTBEAT": str(heartbeat)})
        service.stream.ready = True
        await service.tick()
        assert reopened.next_decision() is None
        assert reopened.engine_state()["state"] == "FLAT"
        assert reopened.cursor() == sample["decision_id"]
        exchange.verify_account.assert_awaited_once()
        for name in writes:
            getattr(exchange, name).assert_not_awaited()
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("available", ["300", "80"])
async def test_execution_service_sizes_signal_for_subaccount_balance(tmp_path, available):
    from strategies.grid.execution.contracts import decode_grid_params, decode_feasibility
    from strategies.grid.execution.planner import HardLimits, build_plan, PlanLimitExceeded

    sample = json.loads((Path(__file__).parents[1] / "fixtures/grid_execution/"
                         "ethusdt_20261114_0305.json").read_text())
    settings = ExecutionSettings(
        mode="live", max_margin_usdt=Decimal(240), max_notional_usdt=Decimal(2400),
        max_daily_loss_usdt=Decimal(15), max_drawdown_usdt=Decimal(30),
        max_open_orders=7, expected_leverage=5)
    filters = ExchangeFilters(
        **{k: Decimal(v) for k, v in sample["replay_metadata"]["filters"].items()})
    original = sample["payload"]
    # Isolate the quantity calculation from the independently tested seven-order
    # production limit. No service is started with this diagnostic limit.
    plan = build_plan(
        decode_grid_params(original["grid_params"]),
        decode_feasibility(original["feasibility"]),
        Decimal(original["market_analysis"]["current_price"]), filters,
        HardLimits(Decimal(240), Decimal(2400), 13, 5),
        margin_budget_usdt=min(Decimal(available), Decimal(240)))
    assert plan.max_margin_usdt <= min(Decimal(available), Decimal(240))
    assert plan.orders[0].quantity < Decimal(".258")

    # A separate six-interval signal exercises normal service sizing within
    # the production seven-order limit, using the same price bounds.
    payload = json.loads(json.dumps(original))
    payload["grid_params"]["grid_count"] = payload["feasibility"]["grid_count"] = 6
    spacing = (Decimal(payload["grid_params"]["upper_boundary"])
               - Decimal(payload["grid_params"]["lower_boundary"])) / 6
    payload["grid_params"]["grid_spacing"] = str(spacing)
    payload["grid_params"]["profit_rate"] = str(
        spacing / Decimal(payload["market_analysis"]["current_price"]))
    exchange = Mock(spec=GridExchange)
    exchange.verify_account = AsyncMock(return_value=filters)
    exchange.ticker_price = AsyncMock(
        return_value=Decimal(payload["market_analysis"]["current_price"]))
    exchange.account_info = AsyncMock(return_value={"totalAvailableBalance": available})
    path = tmp_path / "size.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    try:
        service = ExecutorService(settings, store, exchange, {})
        sized = await service._plan(payload)
        budget = min(Decimal(available), Decimal(240))
        assert sized.max_margin_usdt <= budget
        assert sized.max_notional_usdt <= budget * 5
        assert all(o.quantity < Decimal(".258") for o in sized.orders)
        assert payload["feasibility"]["qty_per_grid"] == original["feasibility"]["qty_per_grid"]
        assert len(sized.orders) <= 7
        with pytest.raises(PlanLimitExceeded, match="open order count"):
            await service._plan(original)
    finally:
        store.close()
