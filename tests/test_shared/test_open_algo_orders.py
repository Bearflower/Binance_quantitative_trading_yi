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


@pytest.mark.asyncio
async def test_pm_query_algo_order_uses_signed_identity_lookup():
    client = object.__new__(BinanceClient)
    client.use_unified_account = True
    client._request = AsyncMock(return_value={"algoId": 123, "actualOrderId": "456"})

    result = await client.get_algo_order(123)

    assert result["actualOrderId"] == "456"
    client._request.assert_awaited_once_with(
        "GET", "/papi/v1/um/algo/algoOrder", {"algoId": 123}, signed=True,
    )


@pytest.mark.asyncio
async def test_pm_query_algo_order_rejects_identity_mismatch():
    client = object.__new__(BinanceClient)
    client.use_unified_account = True
    client._request = AsyncMock(return_value={"algoId": 124, "actualOrderId": "456"})

    with pytest.raises(ValueError, match="algoId"):
        await client.get_algo_order(123)
