"""交易所过滤器缺失时不能把猜测值永久缓存。"""

from unittest.mock import AsyncMock

import pytest

from shared.binance_api import BinanceClient


@pytest.mark.asyncio
async def test_missing_symbol_uses_transient_default_then_refreshes():
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

    assert (await client.get_symbol_info("BNBUSDT"))["stepSize"] == "0.001"
    assert "BNBUSDT" not in client._symbol_precision_cache
    assert (await client.get_symbol_info("BNBUSDT"))["stepSize"] == "0.01"
    assert client.get_exchange_info.await_count == 2
