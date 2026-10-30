"""交易所过滤器缺失时停止下单，并允许后续重新查询。"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from shared.binance_api import BinanceClient


@pytest.mark.asyncio
async def test_missing_symbol_fails_closed_then_refreshes():
    client = BinanceClient.__new__(BinanceClient)
    client._symbol_precision_cache = {}
    client.get_exchange_info = AsyncMock(side_effect=[
        {"symbols": []},
        {"symbols": [{
            "symbol": "BNBUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.01"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
            ],
        }]},
    ])

    with pytest.raises(ValueError, match="BNBUSDT"):
        await client.get_symbol_info("BNBUSDT")
    assert "BNBUSDT" not in client._symbol_precision_cache
    assert (await client.get_symbol_info("BNBUSDT"))["stepSize"] == "0.01"
    assert client.get_exchange_info.await_count == 2


@pytest.mark.asyncio
async def test_incomplete_symbol_filters_are_rejected_without_cache():
    client = BinanceClient.__new__(BinanceClient)
    client._symbol_precision_cache = {}
    client.get_exchange_info = AsyncMock(return_value={"symbols": [{
        "symbol": "BNBUSDT",
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.01"}],
    }]})

    with pytest.raises(ValueError, match="PRICE_FILTER"):
        await client.get_symbol_info("BNBUSDT")
    assert "BNBUSDT" not in client._symbol_precision_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("filters", [
    [{"filterType": "LOT_SIZE"}, {"filterType": "PRICE_FILTER", "tickSize": "0.001"}],
    [{"filterType": "LOT_SIZE", "stepSize": "0"}, {"filterType": "PRICE_FILTER", "tickSize": "0.001"}],
    [{"filterType": "LOT_SIZE", "stepSize": "Infinity"}, {"filterType": "PRICE_FILTER", "tickSize": "0.001"}],
    [{"filterType": "LOT_SIZE", "stepSize": "0.01"}, {"filterType": "PRICE_FILTER", "tickSize": "NaN"}],
])
async def test_missing_or_zero_step_size_is_rejected(filters):
    client = BinanceClient.__new__(BinanceClient)
    client._symbol_precision_cache = {}
    client.get_exchange_info = AsyncMock(return_value={"symbols": [{
        "symbol": "BNBUSDT", "filters": filters,
    }]})

    with pytest.raises(ValueError, match="BNBUSDT"):
        await client.get_symbol_info("BNBUSDT")
    assert "BNBUSDT" not in client._symbol_precision_cache


@pytest.mark.asyncio
async def test_order_is_not_sent_without_exchange_precision():
    client = BinanceClient.__new__(BinanceClient)
    client._symbol_precision_cache = {}
    client.get_exchange_info = AsyncMock(return_value={"symbols": []})
    client._request = AsyncMock()

    with pytest.raises(ValueError, match="BNBUSDT"):
        await client.place_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "LIMIT",
            client_order_id="precision-guard-test",
        )
    client._request.assert_not_awaited()
