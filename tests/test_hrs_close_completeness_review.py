"""HRS 全平结算必须覆盖本轮完整退出量，不能只看到早先的止盈。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategies.hrs.strategy import HRSStrategy
from strategies.hrs.position_manager import PositionManager
from strategies.hrs.risk_manager import RiskManager


def strategy_with_delayed_stop():
    strategy = HRSStrategy.__new__(HRSStrategy)
    strategy._total_pnl = 0.0
    strategy.position_manager = PositionManager({}, MagicMock())
    strategy.position_manager.add_position("AAVEUSDT", "long", 100, 1, 2)
    pos = strategy.position_manager.get_position("AAVEUSDT")
    pos["opened_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
    strategy.risk_manager = RiskManager({})
    strategy._get_exchange_position_qty = AsyncMock(return_value=0)
    strategy._enforce_allocation_holdings = AsyncMock()
    strategy._should_unregister = lambda symbol: False
    strategy._save_state = AsyncMock()
    strategy._reconcile_closed_position = AsyncMock()
    strategy.position_manager.cancel_all_orders = AsyncMock(return_value={"failed": 0})
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_ticker = AsyncMock(return_value={"lastPrice": "80"})
    strategy.binance_client.get_income_history = AsyncMock(return_value=[
        {"symbol": "AAVEUSDT", "incomeType": "REALIZED_PNL", "asset": "USDT",
         "tranId": 91, "tradeId": 71, "income": "3"}])
    strategy.binance_client.get_user_trades = AsyncMock(return_value=[
        {"id": 71, "orderId": 11, "symbol": "AAVEUSDT", "side": "SELL", "qty": ".3", "realizedPnl": "3"}])
    strategy.binance_client.trade_logger = MagicMock(insert_pnl_summary=AsyncMock(return_value=True))
    strategy.db = MagicMock(fetch_all=AsyncMock(return_value=[
        {"order_id": "11", "account_id": "test", "strategy": "hrs"},
        {"order_id": "12", "account_id": "test", "strategy": "hrs"}]))
    strategy.strategy_name = "hrs"
    return strategy


@pytest.mark.asyncio
async def test_full_close_waits_for_final_stop_then_settles_once(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "test")
    strategy = strategy_with_delayed_stop()
    with patch("strategies.hrs.strategy.release_claim", new_callable=AsyncMock):
        await strategy._monitor_positions()
        assert strategy.position_manager.has_position("AAVEUSDT")
        assert strategy._total_pnl == 0
        assert "AAVEUSDT" in strategy._pending_close_pnl
        strategy.binance_client.trade_logger.insert_pnl_summary.assert_not_awaited()

        strategy.binance_client.get_income_history.return_value.append(
            {"symbol": "AAVEUSDT", "incomeType": "REALIZED_PNL", "asset": "USDT",
             "tranId": 92, "tradeId": 72, "income": "-14"})
        strategy.binance_client.get_user_trades.return_value.append(
            {"id": 72, "orderId": 12, "symbol": "AAVEUSDT", "side": "SELL", "qty": ".7", "realizedPnl": "-14"})
        await strategy._monitor_positions()
        assert not strategy.position_manager.has_position("AAVEUSDT")
        assert strategy._total_pnl == -11
        assert not strategy._pending_close_pnl
        await strategy._monitor_positions()
        assert strategy._total_pnl == -11
        strategy.binance_client.trade_logger.insert_pnl_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_entry_fills_do_not_make_incomplete_exit_look_complete(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "test")
    strategy = strategy_with_delayed_stop()
    strategy.binance_client.get_user_trades.return_value.append(
        {"id": 70, "orderId": 10, "symbol": "AAVEUSDT", "side": "BUY", "qty": ".7", "realizedPnl": "0"})
    await strategy._monitor_positions()
    assert strategy.position_manager.has_position("AAVEUSDT")
    assert strategy._total_pnl == 0


@pytest.mark.asyncio
async def test_summary_write_failure_preserves_retry_identity(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "test")
    strategy = strategy_with_delayed_stop()
    strategy.position_manager.get_position("AAVEUSDT")["entry_quantity"] = .3
    strategy.binance_client.trade_logger.insert_pnl_summary.return_value = False
    await strategy._monitor_positions()
    assert strategy.position_manager.has_position("AAVEUSDT")
    assert strategy._total_pnl == 0
    assert "AAVEUSDT" in strategy._pending_close_pnl


@pytest.mark.asyncio
async def test_complete_zero_pnl_exit_can_settle_without_income_row(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "test")
    strategy = strategy_with_delayed_stop()
    strategy.binance_client.get_income_history.return_value = []
    strategy.binance_client.get_user_trades.return_value[0].update(qty="1", realizedPnl="0")
    pos = strategy.position_manager.get_position("AAVEUSDT")
    assert await strategy._get_actual_pnl_from_binance(
        "AAVEUSDT", pos["opened_at"], direction="long", entry_quantity=1) == D("0")


@pytest.mark.asyncio
async def test_only_float_representation_noise_is_tolerated(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "test")
    strategy = strategy_with_delayed_stop()
    pos = strategy.position_manager.get_position("AAVEUSDT")
    assert await strategy._get_actual_pnl_from_binance(
        "AAVEUSDT", pos["opened_at"], direction="long", entry_quantity=.1 + .2) == D("3")
    assert await strategy._get_actual_pnl_from_binance(
        "AAVEUSDT", pos["opened_at"], direction="long", entry_quantity=.30001) is None
