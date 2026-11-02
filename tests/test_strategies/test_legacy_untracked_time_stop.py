from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy


def strategy_with_exchange(amount="1"):
    strategy = object.__new__(BTCEthStrategy)
    strategy.positions = {}
    strategy.symbols = ["ETHUSDT"]
    strategy.my_record_name = "btc_eth_aggressive"
    strategy.db_manager = None
    strategy.min_position_amt = 0.00001
    strategy._untracked_stop_retry_symbols = set()
    strategy.binance = MagicMock()
    strategy.binance.cancel_all_algo_orders = AsyncMock()
    strategy.binance.get_position = AsyncMock(return_value=[
        {"symbol": "ETHUSDT", "positionAmt": amount, "entryPrice": "100"}
    ])
    strategy.binance.get_open_algo_orders = AsyncMock(return_value=[
        {"symbol": "ETHUSDT", "side": "SELL", "orderType": "STOP",
         "reduceOnly": True, "quantity": amount, "triggerPrice": "90"}
    ])
    strategy._close_untracked_exchange_position = AsyncMock(return_value=True)
    return strategy


@pytest.mark.asyncio
async def test_expired_verified_legacy_position_uses_existing_close_path():
    strategy = strategy_with_exchange()
    entry_ms = 1793409086845
    strategy.binance.get_user_trades = AsyncMock(return_value=[
        {"id": 3, "time": entry_ms, "side": "BUY", "qty": "1"},
    ])
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth_aggressive")):
        with patch("strategies.btc_eth_aggressive.strategy.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 6, tzinfo=timezone.utc)
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            assert await strategy._check_legacy_untracked_time_stop() is True
    strategy._close_untracked_exchange_position.assert_awaited_once()
    assert strategy._close_untracked_exchange_position.await_args.args[2] == "LEGACY_TIME_STOP"
    assert strategy._verified_legacy_positions == {}
    assert strategy.binance.get_user_trades.await_args.args == ("ETHUSDT",)
    assert strategy.binance.get_user_trades.await_args.kwargs["start_time"] == entry_ms


@pytest.mark.asyncio
async def test_legacy_position_before_deadline_keeps_exchange_orders():
    strategy = strategy_with_exchange()
    entry_ms = 1793409086845
    strategy.binance.get_user_trades = AsyncMock(return_value=[
        {"id": 3, "time": entry_ms, "side": "BUY", "qty": "1"},
    ])
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth_aggressive")):
        with patch("strategies.btc_eth_aggressive.strategy.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 2, tzinfo=timezone.utc)
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            assert await strategy._check_legacy_untracked_time_stop() is True
    strategy._close_untracked_exchange_position.assert_not_awaited()
    strategy.binance.cancel_all_algo_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_or_other_owner_never_closes():
    strategy = strategy_with_exchange()
    strategy.binance.get_user_trades = AsyncMock(return_value=[
        {"id": 3, "time": 1793409086845, "side": "BUY", "qty": "0.9"}
    ])
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth_aggressive")):
        assert await strategy._check_legacy_untracked_time_stop() is False
    strategy._close_untracked_exchange_position.assert_not_awaited()

    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth")):
        assert await strategy._check_legacy_untracked_time_stop() is True
    strategy._close_untracked_exchange_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_close_rechecks_owner_and_direction_before_order():
    strategy = strategy_with_exchange()
    del strategy._close_untracked_exchange_position
    strategy._close_position = AsyncMock()
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth")):
        assert not await strategy._close_untracked_exchange_position(
            "ETHUSDT", Decimal("1"), "LEGACY_TIME_STOP"
        )
    strategy._close_position.assert_not_awaited()

    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth_aggressive")):
        strategy.binance.get_position = AsyncMock(return_value=[
            {"symbol": "ETHUSDT", "positionAmt": "-1", "entryPrice": "100"}
        ])
        assert not await strategy._close_untracked_exchange_position(
            "ETHUSDT", Decimal("1"), "LEGACY_TIME_STOP"
        )
    strategy._close_position.assert_not_awaited()



@pytest.mark.asyncio
async def test_legacy_expiry_full_close_path_confirms_flat_then_cleans_protection():
    strategy = strategy_with_exchange("0.023")
    del strategy._close_untracked_exchange_position
    strategy.strategy_name = "btc_eth_aggressive"
    strategy.db_manager = MagicMock()
    strategy.db_manager.execute = AsyncMock()
    strategy.binance.get_user_trades = AsyncMock(return_value=[
        {"id": 3, "time": 1793409086845, "side": "BUY", "qty": "0.023"},
    ])
    strategy._sync_position_with_exchange = AsyncMock(return_value={"closed": True})
    strategy._cancel_symbol_conditional_orders = AsyncMock(return_value=True)
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth_aggressive")):
        with patch("strategies.btc_eth_aggressive.strategy.release_claim",
                   new=AsyncMock()) as release:
            with patch("strategies.btc_eth_aggressive.strategy.close_remaining",
                       new=AsyncMock(return_value=MagicMock(success=True))):
                assert await strategy._close_untracked_exchange_position(
                    "ETHUSDT", Decimal("0.023"), "LEGACY_TIME_STOP"
                )
    strategy._sync_position_with_exchange.assert_awaited_once()
    strategy._cancel_symbol_conditional_orders.assert_awaited_once()
    strategy.db_manager.execute.assert_awaited_once()
    release.assert_awaited_once()

@pytest.mark.asyncio
async def test_partially_closed_sol_legacy_lot_recovers_owner_from_first_fill_order():
    strategy = strategy_with_exchange("0.47")
    strategy.binance.get_user_trades = AsyncMock(return_value=[
        {"id": 1, "time": 1793347876165, "side": "BUY", "qty": "0.47",
         "orderId": 253886663232},
        {"id": 2, "time": 1793409091965, "side": "BUY", "qty": "0.54",
         "orderId": 254133410855},
        {"id": 3, "time": 1793559708166, "side": "SELL", "qty": "0.54",
         "orderId": 254537742120},
    ])
    strategy.db_manager = MagicMock()
    strategy.db_manager.fetch_one = AsyncMock(side_effect=[
        None, {"strategy": "btc_eth_aggressive", "order_type": "LIMIT", "side": "BUY"}
    ])
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value=None)):
        assert await strategy._owns_exchange_position(
            "SOLUSDT", Decimal("0.47")
        )
    assert strategy.db_manager.fetch_one.await_args.args[1:] == (
        "SOLUSDT", "253886663232"
    )


@pytest.mark.asyncio
async def test_legacy_owner_fallback_rejects_competing_owner_or_unmatched_lot():
    strategy = strategy_with_exchange("0.47")
    strategy.db_manager = MagicMock()
    strategy.db_manager.fetch_one = AsyncMock(return_value=None)
    strategy.binance.get_user_trades = AsyncMock(return_value=[
        {"id": 1, "time": 1793347876165, "side": "BUY", "qty": "0.47",
         "orderId": 253886663232},
    ])
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value="btc_eth")):
        assert not await strategy._owns_exchange_position(
            "SOLUSDT", Decimal("0.47")
        )
    strategy.db_manager.fetch_one.assert_not_awaited()
    with patch("strategies.btc_eth_aggressive.strategy.resolve_position_owner",
               new=AsyncMock(return_value=None)):
        assert await strategy._owns_exchange_position(
            "SOLUSDT", Decimal("0.48")
        ) is None
    strategy.db_manager.fetch_one.assert_awaited_once()
