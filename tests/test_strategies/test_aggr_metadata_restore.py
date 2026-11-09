import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy


ANCHOR = 1793409086845


def make_strategy(order_id=101):
    strategy = object.__new__(BTCEthStrategy)
    strategy.instance_id = "btc_eth_aggressive"
    strategy.my_record_name = "btc_eth_aggressive"
    strategy.db_manager = MagicMock()
    strategy.binance = MagicMock()
    state = {"positions": {"ETHUSDT": {
        "direction": "LONG", "entry_price": "2200", "grade": "B", "atr": "18",
        "entry_time": "2026-10-31T01:11:20", "entry_fill_time_ms": ANCHOR,
        "initial_quantity": "0.03", "current_quantity": "0.023",
        "entry_order_id": 100, "episode_entry_order_id": 100,
        "stop_loss_order_id": 101,
        "tp1_order_id": 102, "tp2_order_id": 103,
        "tp1_hit": False, "tp2_hit": False,
        "trailing_activated": False,
    }}}
    strategy.db_manager.fetch_one = AsyncMock(return_value={"state_data": json.dumps(state)})
    strategy.binance.get_user_trades = AsyncMock(side_effect=[[
        {"id": 1, "time": ANCHOR, "side": "BUY", "qty": "0.03"},
        {"id": 2, "time": ANCHOR + 1000, "side": "SELL", "qty": "0.007"},
    ], []])
    strategy.binance.get_open_algo_orders = AsyncMock(return_value=[
        {"algoId": order_id, "symbol": "ETHUSDT", "side": "SELL", "orderType": "STOP", "reduceOnly": True, "quantity": "0.023"},
        {"algoId": 102, "symbol": "ETHUSDT", "side": "SELL", "orderType": "TAKE_PROFIT", "reduceOnly": True, "quantity": "0.006"},
        {"algoId": 103, "symbol": "ETHUSDT", "side": "SELL", "orderType": "TAKE_PROFIT", "reduceOnly": True, "quantity": "0.009"},
    ])
    return strategy


@pytest.mark.asyncio
async def test_restores_original_grade_atr_and_fill_time_when_exchange_matches():
    s = make_strategy()
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner", new=AsyncMock(return_value="btc_eth_aggressive")):
        pos = await s._load_verified_position_metadata(
            "ETHUSDT", {"symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200"}, Decimal("2300")
        )
    assert pos.grade == "B"
    assert pos.atr == Decimal("18")
    assert pos.current_quantity == Decimal("0.023")
    assert pos.entry_fill_time_ms == ANCHOR
    assert pos.episode_entry_order_id == 100
    assert pos.stop_loss_order_id == 101
    assert pos.entry_time.isoformat() == "2026-10-31T01:11:26.845000"
    first_call = s.binance.get_user_trades.await_args_list[0]
    assert first_call.args == ("ETHUSDT",)
    assert first_call.kwargs["start_time"] == ANCHOR
    assert all(call.kwargs["limit"] == 1000 for call in s.binance.get_user_trades.await_args_list)


@pytest.mark.asyncio
async def test_rejects_stale_order_or_reopened_lot():
    s = make_strategy(order_id=999)
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner", new=AsyncMock(return_value="btc_eth_aggressive")):
        assert await s._load_verified_position_metadata(
            "ETHUSDT", {"symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200"}, Decimal("2300")
        ) is None
    s = make_strategy()
    s.binance.get_user_trades.side_effect = [[
        {"id": 1, "time": ANCHOR, "side": "BUY", "qty": "0.03"},
        {"id": 2, "time": ANCHOR + 1000, "side": "SELL", "qty": "0.03"},
        {"id": 3, "time": ANCHOR + 2000, "side": "BUY", "qty": "0.023"},
    ], []]
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner", new=AsyncMock(return_value="btc_eth_aggressive")):
        assert await s._load_verified_position_metadata(
            "ETHUSDT", {"symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200"}, Decimal("2300")
        ) is None


@pytest.mark.asyncio
async def test_adoption_prefers_verified_metadata_even_when_db_order_rows_are_stale():
    s = make_strategy()
    s._get_current_price = AsyncMock(return_value=Decimal("2210"))
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner", new=AsyncMock(return_value="btc_eth_aggressive")):
        pos = await s._build_adopted_position_state(
            "ETHUSDT", {"symbol": "ETHUSDT", "positionAmt": "0.023", "entryPrice": "2200"}, {}
        )
    assert pos is not None
    assert pos.grade == "B"
    assert pos.atr == Decimal("18")
