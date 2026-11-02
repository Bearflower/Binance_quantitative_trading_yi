"""A failed allocation close may not remove an existing exchange hard stop."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch

import pytest

from strategies.btc_eth.strategy import BTCEthStrategy
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggressiveStrategy
from shared.binance_api import BinanceAPIError


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_type", [BTCEthStrategy, AggressiveStrategy])
async def test_close_order_failure_keeps_existing_conditional_protection(strategy_type):
    strategy = object.__new__(strategy_type)
    position = SimpleNamespace(
        direction="LONG", current_quantity=Decimal("1"), entry_price=Decimal("100")
    )
    strategy.positions = {"BTCUSDT": position}
    strategy.risk_config = {
        "position_sizing": {"min_close_notional_usdt": 20},
        "close_limit_order": {"max_retries": 0, "poll_interval_seconds": 0, "timeout_seconds": 0},
    }
    strategy._sync_position_with_exchange = AsyncMock(return_value={"closed": False})
    strategy._get_symbol_precision = AsyncMock(return_value={
        "stepSize": "0.001", "tickSize": Decimal("0.01")
    })
    strategy._adjust_quantity_precision = MagicMock(side_effect=lambda qty, step: qty)
    strategy._cancel_symbol_conditional_orders = AsyncMock(return_value=True)
    strategy.binance = MagicMock()
    strategy.binance.get_ticker_price = AsyncMock(return_value="100")
    strategy.binance.get_orderbook = AsyncMock(side_effect=RuntimeError("exchange unavailable"))

    assert await strategy._close_position("BTCUSDT", position, Decimal("1"), "ALLOCATION_LIMIT") is False
    strategy._cancel_symbol_conditional_orders.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_type", [BTCEthStrategy, AggressiveStrategy])
async def test_confirmed_allocation_close_retains_direction_for_pnl(strategy_type):
    strategy = object.__new__(strategy_type)
    position = SimpleNamespace(
        direction="LONG", current_quantity=Decimal("1"), entry_price=Decimal("100"),
        cancel_pending=False,
    )
    strategy.positions = {"BTCUSDT": position}
    strategy.strategy_name = "test"
    strategy.risk_config = {
        "position_sizing": {"min_close_notional_usdt": 20},
        "close_limit_order": {"max_retries": 0, "poll_interval_seconds": 1, "timeout_seconds": 1},
    }

    async def sync(*args):
        if strategy._sync_position_with_exchange.await_count == 1:
            return {"closed": False}
        position.direction = "FLAT"
        position.current_quantity = Decimal("0")
        return {"closed": True}

    strategy._sync_position_with_exchange = AsyncMock(side_effect=sync)
    strategy._get_symbol_precision = AsyncMock(return_value={
        "stepSize": "0.001", "tickSize": Decimal("0.01")
    })
    strategy._adjust_quantity_precision = MagicMock(side_effect=lambda qty, step: qty)
    strategy._adjust_price_precision = MagicMock(side_effect=lambda price, tick: price)
    strategy._cancel_symbol_conditional_orders = AsyncMock(return_value=True)
    strategy._cleanup_position_orders = AsyncMock()
    strategy.binance = MagicMock()
    strategy.binance.get_orderbook = AsyncMock(return_value={"bids": [["110", "1"]]})
    strategy.binance.place_order = AsyncMock(return_value={"orderId": 123, "avgPrice": "110"})
    strategy.binance.get_open_orders = AsyncMock(return_value=[])
    strategy.binance.trade_logger = MagicMock()
    strategy.binance.trade_logger.update_realized_pnl = AsyncMock()
    strategy.binance.trade_logger.log_stop_loss = AsyncMock()
    strategy.notification = MagicMock()
    strategy.notification.send_trade_notification = AsyncMock()

    with patch(f"{strategy_type.__module__}.asyncio.sleep", new=AsyncMock()):
        assert await strategy._close_position(
            "BTCUSDT", position, Decimal("1"), "ALLOCATION_LIMIT", Decimal("110")
        ) is True

    strategy.binance.trade_logger.update_realized_pnl.assert_awaited_once()
    assert strategy.binance.trade_logger.update_realized_pnl.await_args.kwargs["realized_pnl"] == Decimal("10")
    assert strategy.binance.trade_logger.log_stop_loss.await_args.kwargs["side"] == "SELL"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_type", [BTCEthStrategy, AggressiveStrategy])
async def test_reduce_only_rejection_keeps_hard_stop_for_allocation_close(strategy_type):
    strategy = object.__new__(strategy_type)
    position = SimpleNamespace(
        direction="LONG", current_quantity=Decimal("1"), entry_price=Decimal("100")
    )
    strategy.positions = {"BTCUSDT": position}
    strategy.risk_config = {
        "position_sizing": {"min_close_notional_usdt": 20},
        "close_limit_order": {"max_retries": 0, "poll_interval_seconds": 0, "timeout_seconds": 0},
    }
    strategy._sync_position_with_exchange = AsyncMock(return_value={"closed": False})
    strategy._get_symbol_precision = AsyncMock(return_value={
        "stepSize": "0.001", "tickSize": Decimal("0.01")
    })
    strategy._adjust_quantity_precision = MagicMock(side_effect=lambda qty, step: qty)
    strategy._adjust_price_precision = MagicMock(side_effect=lambda price, tick: price)
    strategy._cancel_symbol_conditional_orders = AsyncMock(return_value=True)
    strategy.binance = MagicMock()
    strategy.binance.get_ticker_price = AsyncMock(return_value="100")
    strategy.binance.get_orderbook = AsyncMock(return_value={"bids": [["99", "1"]]})
    strategy.binance.place_order = AsyncMock(
        side_effect=BinanceAPIError(-4118, "ReduceOnly Order Failed")
    )

    assert await strategy._close_position("BTCUSDT", position, Decimal("1"), "ALLOCATION_LIMIT") is False
    strategy._cancel_symbol_conditional_orders.assert_not_awaited()
