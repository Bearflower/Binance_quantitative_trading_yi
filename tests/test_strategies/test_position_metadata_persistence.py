from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy, PositionState


@pytest.mark.asyncio
async def test_aggressive_persists_full_position_metadata_under_independent_key():
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = object()
    strategy.instance_id = "btc_eth_aggressive"
    position = PositionState()
    position.direction = "LONG"
    position.entry_price = Decimal("100")
    position.entry_time = datetime(2026, 11, 2, 12, 0)
    position.grade = "A"
    position.atr = Decimal("2.5")
    position.initial_quantity = Decimal("1")
    position.current_quantity = Decimal("0.8")
    position.entry_order_id = 123
    position.episode_entry_order_id = 123
    position.stop_loss_order_id = 456
    position.tp1_order_id = 789
    position.tp2_order_id = 790
    position.tp1_hit = True
    strategy.positions = {"ETHUSDT": position}
    strategy._unbound_stop_algo_ids = {"ETHUSDT": {"987"}}

    with patch("strategies.btc_eth_aggressive.strategy.save_strategy_state",
               new=AsyncMock()) as save:
        await strategy._persist_position_metadata("ETHUSDT")

    assert save.await_args.kwargs["state_key"] == "position:ETHUSDT"
    data = save.await_args.args[2]["ETHUSDT"]
    assert data["grade"] == "A"
    assert data["atr"] == "2.5"
    assert data["entry_time"] == "2026-11-02T12:00:00"
    assert data["tp1_hit"] is True
    assert data["entry_order_id"] == 123
    assert data["episode_entry_order_id"] == 123
    assert data["unbound_stop_algo_ids"] == ["987"]


@pytest.mark.asyncio
async def test_aggressive_does_not_persist_guessed_metadata():
    strategy = object.__new__(BTCEthStrategy)
    strategy.positions = {"ETHUSDT": PositionState()}
    with patch("strategies.btc_eth_aggressive.strategy.save_strategy_state",
               new=AsyncMock()) as save:
        await strategy._persist_position_metadata("ETHUSDT")
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_position_persists_exchange_first_fill_as_restart_anchor():
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = object()
    strategy.instance_id = "btc_eth_aggressive"
    strategy.binance = AsyncMock()
    strategy.binance.get_user_trades.return_value = [
        {"id": 5, "time": 1793409086845, "side": "BUY", "qty": "0.023"}
    ]
    position = PositionState()
    position.direction = "LONG"
    position.entry_price = Decimal("2200")
    position.entry_time = datetime(2026, 10, 31, 1, 11, 20)
    position.grade = "B"
    position.atr = Decimal("18")
    position.initial_quantity = Decimal("0.023")
    position.current_quantity = Decimal("0.023")
    position.entry_order_id = 100
    position.entry_fill_capture_pending = True
    strategy.positions = {"ETHUSDT": position}

    with patch("strategies.btc_eth_aggressive.strategy.save_strategy_state", new=AsyncMock()) as save:
        await strategy._persist_position_metadata("ETHUSDT")

    data = save.await_args.args[2]["ETHUSDT"]
    assert data["entry_fill_time_ms"] == 1793409086845
    assert data["entry_time"] == "2026-10-31T01:11:26.845000"
    assert position.entry_fill_capture_pending is False
    strategy.binance.get_user_trades.assert_awaited_once_with("ETHUSDT", order_id="100")
