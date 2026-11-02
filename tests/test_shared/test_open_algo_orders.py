"""The current PM algo endpoint must report live protection orders."""

from unittest.mock import AsyncMock

import pytest

from shared.binance_api import BinanceClient


@pytest.mark.asyncio
async def test_pm_open_algo_orders_uses_current_endpoint():
    client = object.__new__(BinanceClient)
    client.use_unified_account = True
    client._request = AsyncMock(return_value=[{"algoId": 123, "symbol": "ETHUSDT"}])

    rows = await client.get_open_algo_orders("ethusdt")

    assert rows == [{"algoId": 123, "symbol": "ETHUSDT"}]
    client._request.assert_awaited_once_with(
        "GET", "/papi/v1/um/algo/openAlgoOrders",
        {"symbol": "ETHUSDT", "algoType": "CONDITIONAL"}, signed=True,
    )


@pytest.mark.asyncio
async def test_pm_open_algo_orders_rejects_invalid_response():
    client = object.__new__(BinanceClient)
    client.use_unified_account = True
    client._request = AsyncMock(return_value={"error": "unexpected"})

    with pytest.raises(ValueError, match="响应数据格式错误"):
        await client.get_open_algo_orders("ETHUSDT")
