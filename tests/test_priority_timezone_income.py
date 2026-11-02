"""Regression checks for live timezone and income availability paths."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.new_coin.strategy import BEIJING_TZ, NewCoinStrategy
from strategies.btc_eth.strategy import BTCEthStrategy as StandardStrategy
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggressiveStrategy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard" / "backend"))
from dashboard.backend.services.data_service_docker import DataService


@pytest.mark.asyncio
async def test_new_coin_recent_aware_entry_is_not_treated_as_closed():
    strategy = object.__new__(NewCoinStrategy)
    strategy.positions = {
        "TESTUSDT": {
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_price": 1,
        }
    }
    strategy.trading_executor = MagicMock()
    strategy.trading_executor.check_position_management = AsyncMock()
    strategy.trading_executor._update_short_position_closed = AsyncMock()
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_position = AsyncMock(return_value=[])

    await strategy._monitor_positions()

    strategy.trading_executor._update_short_position_closed.assert_not_awaited()
    assert "TESTUSDT" in strategy.positions


@pytest.mark.asyncio
async def test_new_coin_recent_legacy_naive_utc_entry_is_not_treated_as_closed():
    strategy = object.__new__(NewCoinStrategy)
    strategy.positions = {
        "TESTUSDT": {
            "entry_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "entry_price": 1,
        }
    }
    strategy.trading_executor = MagicMock()
    strategy.trading_executor.check_position_management = AsyncMock()
    strategy.trading_executor._update_short_position_closed = AsyncMock()
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_position = AsyncMock(return_value=[])

    await strategy._monitor_positions()

    strategy.trading_executor._update_short_position_closed.assert_not_awaited()
    assert "TESTUSDT" in strategy.positions


@pytest.mark.asyncio
async def test_new_coin_weekly_review_queries_beijing_monday_midnight():
    strategy = object.__new__(NewCoinStrategy)
    strategy.my_record_name = "new_coin"
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[])

    await strategy._weekly_review()

    bound_start = strategy.db.fetch_all.await_args.args[2]
    now_beijing = datetime.now(BEIJING_TZ)
    expected = (now_beijing - timedelta(days=now_beijing.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    assert bound_start == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_class", [StandardStrategy, AggressiveStrategy])
async def test_conditional_trade_lookup_binds_naive_beijing_window(strategy_class, monkeypatch):
    import shared.condition_orders as condition_orders

    monkeypatch.setattr(condition_orders, "mark_order_executed", AsyncMock())
    strategy = MagicMock()
    strategy.db_manager.fetch_all = AsyncMock(return_value=[])
    strategy._get_cancel_retry_config.return_value = 5

    await strategy_class._record_executed_conditional_order(strategy, "BTCUSDT", "STOP", 123)

    args = strategy.db_manager.fetch_all.await_args.args
    start, end = args[2], args[3]
    assert start.tzinfo is None and end.tzinfo is None
    expected_now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    assert abs((start + timedelta(minutes=5) - expected_now).total_seconds()) < 5


@pytest.mark.asyncio
async def test_dashboard_income_failure_propagates_and_is_not_cached():
    service = DataService()
    service._binance_client = MagicMock()
    service._binance_client.get_income_history = AsyncMock(side_effect=RuntimeError("income unavailable"))

    with pytest.raises(RuntimeError, match="income unavailable"):
        await service._get_income_data(0, 1000)

    assert service._income_cache == {}


@pytest.mark.asyncio
async def test_dashboard_paginated_income_rejects_partial_window():
    service = DataService()
    service._binance_client = MagicMock()
    service._binance_client.get_income_history = AsyncMock(side_effect=[
        [{"income": "1"}], RuntimeError("second window unavailable")
    ])

    with pytest.raises(RuntimeError, match="second window unavailable"):
        await service._get_income_data_paginated(0, 2 * 24 * 3600 * 1000, slice_days=1)

