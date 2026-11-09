"""PM STOP placement transmits the prewritten clientAlgoId to Binance."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from shared.binance_api import BinanceClient


@pytest.mark.asyncio
async def test_pm_conditional_order_passes_client_algo_id_without_ordinary_retry_key():
    client = object.__new__(BinanceClient)
    client.use_unified_account = True
    client.trade_logger = None
    client.get_symbol_info = AsyncMock(return_value={"stepSize": "0.001", "tickSize": Decimal("0.01")})
    client.get_ticker = AsyncMock(return_value={"lastPrice": "100"})
    client._request = AsyncMock(return_value={"algoId": 200, "clientAlgoId": "sa123"})

    await client.place_conditional_order(
        "ETHUSDT", "SELL", Decimal("95"), Decimal("0.3"),
        price=Decimal("94"), order_type="STOP", reduce_only=True,
        clientAlgoId="sa123",
    )

    method, endpoint, params = client._request.await_args.args
    assert (method, endpoint) == ("POST", "/papi/v1/um/algo/order")
    assert params["clientAlgoId"] == "sa123"
    assert params["reduceOnly"] == "true"
    assert client._request.await_args.kwargs["idempotency_key"] is None
