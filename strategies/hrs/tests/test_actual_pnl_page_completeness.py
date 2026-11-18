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
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1)
    )

    assert result is None


@pytest.mark.asyncio
async def test_non_list_income_response_is_not_reported_as_zero_pnl():
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_income_history = AsyncMock(return_value={"error": "unavailable"})

    result = await strategy._get_actual_pnl_from_binance(
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1)
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
        {"symbol": "ABCUSDT", "id": 11, "orderId": 101, "realizedPnl": "5"}
    ])
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[
        {"account_id": "main", "order_id": "101", "strategy": "hrs"}
    ])
    result = await strategy._get_actual_pnl_from_binance(
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1)
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
        {"symbol": "ABCUSDT", "id": 11, "orderId": 101, "realizedPnl": "5"}
    ])
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[
        {"account_id": "main", "order_id": "101", "strategy": "new_coin"}
    ])
    result = await strategy._get_actual_pnl_from_binance(
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1)
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
        {"symbol": "ABCUSDT", "id": 11, "orderId": 101, "realizedPnl": "5"},
        {"symbol": "ABCUSDT", "id": 12, "orderId": 102, "realizedPnl": "2"},
    ])
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[
        {"account_id": "main", "order_id": "101", "strategy": "hrs"},
        {"account_id": "main", "order_id": "102", "strategy": "hrs"},
    ])
    result = await strategy._get_actual_pnl_from_binance(
        "ABCUSDT", datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert result is None


@pytest.mark.asyncio
async def test_finished_hrs_tp_algo_attributes_its_actual_order(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_income_history = AsyncMock(return_value=[
        {"symbol": "ARIAUSDT", "tranId": 9, "asset": "USDT", "income": "0.17484",
         "tradeId": 22, "incomeType": "REALIZED_PNL"}
    ])
    strategy.binance_client.get_user_trades = AsyncMock(return_value=[
        {"symbol": "ARIAUSDT", "id": 22, "orderId": 200, "realizedPnl": "0.17484",
         "qty": "31", "side": "BUY"}
    ])
    strategy.binance_client.get_algo_order = AsyncMock(return_value={
        "algoId": 100, "symbol": "ARIAUSDT", "algoStatus": "FINISHED",
        "actualOrderId": "200", "side": "BUY", "actualQty": "31",
        "orderType": "TAKE_PROFIT_MARKET",
    })
    strategy.binance_client.get_order = AsyncMock(return_value={
        "orderId": 200, "symbol": "ARIAUSDT", "side": "BUY", "status": "FILLED",
        "reduceOnly": True, "executedQty": "31",
    })
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(side_effect=[
        [], [{"account_id": "main", "strategy": "hrs", "algo_id": "100",
              "side": "BUY"}],
    ])
    result = await strategy._get_actual_pnl_from_binance(
        "ARIAUSDT", datetime.now(timezone.utc) - timedelta(hours=1)
    )
    assert result == Decimal("0.17484")


@pytest.mark.asyncio
async def test_foreign_tp_algo_cannot_attribute_hrs_pnl(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    strategy = object.__new__(HRSStrategy)
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_income_history = AsyncMock(return_value=[
        {"symbol": "ARIAUSDT", "tranId": 9, "asset": "USDT", "income": "1",
         "tradeId": 22, "incomeType": "REALIZED_PNL"}
    ])
    strategy.binance_client.get_user_trades = AsyncMock(return_value=[
        {"symbol": "ARIAUSDT", "id": 22, "orderId": 200,
         "realizedPnl": "1", "qty": "31", "side": "BUY"}
    ])
    strategy.binance_client.get_algo_order = AsyncMock(return_value={
        "algoId": 100, "symbol": "ARIAUSDT", "algoStatus": "FINISHED",
        "actualOrderId": "200", "side": "BUY", "actualQty": "31",
        "orderType": "TAKE_PROFIT_MARKET",
    })
    strategy.binance_client.get_order = AsyncMock(return_value={
        "orderId": 200, "symbol": "ARIAUSDT", "side": "BUY", "status": "FILLED",
        "reduceOnly": True, "executedQty": "31",
    })
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(side_effect=[
        [], [{"account_id": "main", "strategy": "new_coin", "algo_id": "100",
              "side": "BUY"}],
    ])
    assert await strategy._get_actual_pnl_from_binance(
        "ARIAUSDT", datetime.now(timezone.utc) - timedelta(hours=1)
    ) is None
