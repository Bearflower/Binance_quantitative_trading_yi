"""HRS 实盘盈亏不得把交易所收入满页误认为完整结果。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.hrs.strategy import HRSStrategy


@pytest.mark.asyncio
async def test_full_income_page_is_not_reported_as_actual_pnl():
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_income_history = AsyncMock(return_value=[
        {"symbol": "ABCUSDT", "income": "1"} for _ in range(1000)
    ])

    result = await strategy._get_actual_pnl_from_binance(
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1),
        direction="long", entry_quantity=1
    )

    assert result is None


@pytest.mark.asyncio
async def test_non_list_income_response_is_not_reported_as_zero_pnl():
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_income_history = AsyncMock(return_value={"error": "unavailable"})

    result = await strategy._get_actual_pnl_from_binance(
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1),
        direction="long", entry_quantity=1
    )

    assert result is None


@pytest.mark.asyncio
async def test_exact_fill_and_owned_order_can_confirm_actual_pnl(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_income_history = AsyncMock(return_value=[
        {"symbol": "ABCUSDT", "tranId": 1, "asset": "USDT", "income": "5", "tradeId": 11,
         "incomeType": "REALIZED_PNL"}
    ])
    strategy.binance_client.get_user_trades = AsyncMock(return_value=[
        {"symbol": "ABCUSDT", "id": 11, "orderId": 101, "realizedPnl": "5", "side": "SELL", "qty": "1"}
    ])
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[
        {"account_id": "main", "order_id": "101", "strategy": "hrs"}
    ])
    result = await strategy._get_actual_pnl_from_binance(
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1),
        direction="long", entry_quantity=1
    )
    assert result == Decimal("5")


@pytest.mark.asyncio
async def test_unowned_or_unlinked_income_cannot_confirm_actual_pnl(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_income_history = AsyncMock(return_value=[
        {"symbol": "ABCUSDT", "tranId": 1, "asset": "USDT", "income": "5", "tradeId": 11,
         "incomeType": "REALIZED_PNL"}
    ])
    strategy.binance_client.get_user_trades = AsyncMock(return_value=[
        {"symbol": "ABCUSDT", "id": 11, "orderId": 101, "realizedPnl": "5", "side": "SELL", "qty": "1"}
    ])
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[
        {"account_id": "main", "order_id": "101", "strategy": "new_coin"}
    ])
    result = await strategy._get_actual_pnl_from_binance(
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1),
        direction="long", entry_quantity=1
    )
    assert result is None


@pytest.mark.asyncio
async def test_unsettled_extra_fill_prevents_partial_actual_pnl(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_income_history = AsyncMock(return_value=[
        {"symbol": "ABCUSDT", "tranId": 1, "asset": "USDT", "income": "5", "tradeId": 11,
         "incomeType": "REALIZED_PNL"}
    ])
    strategy.binance_client.get_user_trades = AsyncMock(return_value=[
        {"symbol": "ABCUSDT", "id": 11, "orderId": 101, "realizedPnl": "5", "side": "SELL", "qty": "1"},
        {"symbol": "ABCUSDT", "id": 12, "orderId": 102, "realizedPnl": "2", "side": "SELL", "qty": "1"},
    ])
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[
        {"account_id": "main", "order_id": "101", "strategy": "hrs"},
        {"account_id": "main", "order_id": "102", "strategy": "hrs"},
    ])
    result = await strategy._get_actual_pnl_from_binance(
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1),
        direction="long", entry_quantity=1
    )
    assert result is None


@pytest.mark.asyncio
async def test_classic_fill_cannot_inherit_pm_order_id_or_duplicate_income(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock(use_unified_account=False)
    at = datetime.fromtimestamp(1, timezone.utc)
    income = dict(symbol="XUSDT", tranId=7, incomeType="REALIZED_PNL", income="3", asset="USDT", tradeId=8,time=1000)
    strategy.binance_client.get_income_history = AsyncMock(return_value=[income])
    strategy.binance_client.get_user_trades = AsyncMock(return_value=[
        dict(symbol="XUSDT", id=8,orderId=9,realizedPnl="3",time=1000,side="SELL",qty="1")])
    historical = dict(account_id="main",order_id="9",strategy="hrs",executed_at=at-timedelta(milliseconds=1))
    strategy.db = MagicMock()
    order_rows = [historical]
    strategy.db.fetch_all = AsyncMock(side_effect=lambda sql,*args: [] if "order_strategy_evidence" in sql else order_rows)
    assert await strategy._get_actual_pnl_from_binance("XUSDT", at, direction="long", entry_quantity=1) is None
    order_rows[:] = [dict(historical,strategy="btc_eth"),dict(historical,executed_at=at)]
    assert await strategy._get_actual_pnl_from_binance("XUSDT", at, direction="long", entry_quantity=1) == Decimal(3)
    strategy.binance_client.get_income_history.return_value.append(dict(income,tranId=10))
    assert await strategy._get_actual_pnl_from_binance("XUSDT", at, direction="long", entry_quantity=1) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fill_time, fill_qty, expected", [(100, "2", Decimal("-7.25")), (99, "2", None), (100, "1", None)])
async def test_live_close_detail_uses_terminal_fills_in_current_epoch(monkeypatch, fill_time, fill_qty, expected):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    strategy = object.__new__(HRSStrategy)
    api = strategy.binance_client = MagicMock(testnet=False, use_unified_account=False)
    api.trade_logger.update_realized_pnl = AsyncMock()
    order = dict(symbol="ABCUSDT", side="SELL", orderId=9, status="FILLED", origQty="2", executedQty="2", time=100)
    api.get_order = AsyncMock(return_value=order)
    api.get_user_trades = AsyncMock(return_value=[
        dict(symbol="ABCUSDT", side="SELL", orderId=9, id=7, qty=fill_qty, price="15", realizedPnl="-7.25", time=fill_time)
    ])
    await strategy._writeback_pnl("ABCUSDT", "long", 10, 15, 2, {"orderId": 9})
    if expected is None:
        api.trade_logger.update_realized_pnl.assert_not_awaited()
    else:
        api.trade_logger.update_realized_pnl.assert_awaited_once_with(
            order_id="9", realized_pnl=expected, side="SELL", symbol="ABCUSDT", executed_at=None
        )
