"""An add order must not replace the position episode's first entry order."""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy
from shared.protection_retry import merge_position


def test_first_entry_order_is_stable_across_add():
    signal = {
        "entry_price": 100, "timestamp": datetime(2026, 11, 9),
        "direction": "LONG", "atr": 2, "grade": "A",
    }
    first = SimpleNamespace(order_id=1001, avg_price=Decimal("100"))
    pos = BTCEthStrategy._build_position_state(signal, first, Decimal("1"))
    assert pos.episode_entry_order_id == 1001

    second = SimpleNamespace(order_id=1002, avg_price=Decimal("110"))
    merge_position(pos, signal, second, Decimal("2"))

    assert pos.entry_order_id == 1002
    assert pos.episode_entry_order_id == 1001


@pytest.mark.asyncio
async def test_stop_binding_requires_known_episode_and_does_not_block_protection():
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = object()
    strategy.strategy_name = "btc_eth_aggressive"
    strategy.positions = {"ETHUSDT": SimpleNamespace(episode_entry_order_id=None)}
    with patch("strategies.btc_eth_aggressive.strategy.record_stop_binding", new=AsyncMock()) as bind:
        await strategy._record_stop_binding("ETHUSDT", "SELL", Decimal("0.3"), 2001)
        bind.assert_not_awaited()

        strategy.positions["ETHUSDT"].episode_entry_order_id = 1001
        bind.side_effect = RuntimeError("database unavailable")
        await strategy._record_stop_binding("ETHUSDT", "SELL", Decimal("0.3"), 2001)
        bind.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_stop_placement_records_candidate_binding():
    strategy = object.__new__(BTCEthStrategy)
    strategy.db_manager = None
    strategy.binance = AsyncMock()
    strategy.binance.place_conditional_order.return_value = {"algoId": 2001}
    strategy._record_stop_binding = AsyncMock()
    order_id = await strategy._place_conditional_order_and_record(
        "ETHUSDT", "SELL", "STOP", Decimal("98"), Decimal("97"),
        Decimal("0.3"), "STOP_LOSS", "btc_eth_aggressive",
    )
    assert order_id == 2001
    strategy._record_stop_binding.assert_awaited_once_with(
        "ETHUSDT", "SELL", Decimal("0.3"), 2001,
    )
