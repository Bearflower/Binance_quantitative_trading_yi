"""Semi grid mode must remain unable to submit a live order directly."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.grid.grid_calculator import GridLevel
from strategies.grid.order_manager import OrderManager
from strategies.grid.strategy import GridStrategy


@pytest.mark.asyncio
async def test_semi_mode_rejects_direct_order_manager_submission():
    exchange = type("Exchange", (), {})()
    exchange.place_order = AsyncMock(return_value={"orderId": 77})
    manager = OrderManager(
        exchange, None, None,
        {"signal_bot": {"enabled": False, "mode": "semi"}, "trading": {}},
    )
    level = GridLevel(price=Decimal("100"), side="BUY", quantity=Decimal("1"))

    result = await manager.place_grid_order("BTCUSDT", level)

    assert result is None
    exchange.place_order.assert_not_awaited()
    assert manager.pending_orders == {}


def test_semi_mode_stays_nontrading_if_signal_bot_is_disabled():
    strategy = GridStrategy({
        "strategy": {"name": "grid"},
        "symbols": ["BTCUSDT"],
        "signal_bot": {"enabled": False, "mode": "semi"},
    })

    assert strategy.is_signal_mode is True
