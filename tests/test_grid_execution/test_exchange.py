from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution.exchange import (
    AccountModeError, GridExchange, UnknownOrderResultError,
)


class FakeClient:
    use_unified_account = False

    def __init__(self):
        self.calls = []
        self.responses = {}

    async def _request(self, method, path, params=None, signed=True):
        self.calls.append((method, path, params))
        response = self.responses.get((method, path), {})
        return response

    async def _request_once(self, method, path, params=None, signed=True):
        return await self._request(method, path, params, signed)


@pytest.mark.asyncio
async def test_order_and_protection_use_current_endpoints():
    client = FakeClient()
    client.responses[("GET", "/fapi/v1/openAlgoOrders")] = []
    exchange = GridExchange(client)
    await exchange.place_limit("BUY", Decimal("0.02"), Decimal("1900"), "eg-1")
    await exchange.close_position("SELL", Decimal("0.02"), "eg-close")
    await exchange.place_stop("SELL", Decimal("1850"), "eg-stop")
    await exchange.open_stops()
    await exchange.query_stop("eg-stop")
    await exchange.cancel_stop("eg-stop")
    paths = [(method, path) for method, path, _ in client.calls]
    assert paths[:3] == [
        ("POST", "/fapi/v1/order"), ("POST", "/fapi/v1/order"),
        ("POST", "/fapi/v1/algoOrder")]
    assert ("GET", "/fapi/v1/openAlgoOrders") in paths
    assert ("GET", "/fapi/v1/algoOrder") in paths
    assert ("DELETE", "/fapi/v1/algoOrder") in paths
    assert client.calls[1][2]["reduceOnly"] == "true"
    assert client.calls[2][2]["closePosition"] == "true"
    assert client.calls[2][2]["algoType"] == "CONDITIONAL"


@pytest.mark.asyncio
async def test_account_mode_mismatch_refuses_openings():
    client = FakeClient()
    client.responses[("GET", "/fapi/v1/positionSide/dual")] = {"dualSidePosition": True}
    with pytest.raises(AccountModeError):
        await GridExchange(client).verify_account(10)


@pytest.mark.asyncio
async def test_timeout_does_not_reissue_write():
    client = FakeClient()
    calls = []

    async def uncertain(method, path, params=None, signed=True):
        calls.append((method, path))
        raise TimeoutError("reply lost")

    client._request_once = uncertain
    client._request = AsyncMock(return_value={"code": -2013})
    with pytest.raises(UnknownOrderResultError):
        await GridExchange(client).place_limit("BUY", Decimal("0.02"),
                                               Decimal("1900"), "eg-1")
    assert calls == [("POST", "/fapi/v1/order")]


@pytest.mark.asyncio
async def test_income_uses_direct_ordinary_usdm_endpoint():
    client = FakeClient()
    client.responses[("GET", "/fapi/v1/income")] = []
    rows = await GridExchange(client).income(1000, 2000)
    assert rows == []
    assert client.calls[-1] == ("GET", "/fapi/v1/income",
                                {"startTime": 1000, "endTime": 2000,
                                 "limit": 1000})


@pytest.mark.asyncio
async def test_user_stream_listen_key_uses_ordinary_usdm_rest():
    client = FakeClient()
    client.responses[("POST", "/fapi/v1/listenKey")] = {"listenKey": "abc"}
    client.responses[("PUT", "/fapi/v1/listenKey")] = {"listenKey": "abc"}
    exchange = GridExchange(client)
    assert await exchange.start_user_stream() == "abc"
    assert await exchange.keepalive_user_stream() == "abc"
    assert client.calls[-2:] == [("POST", "/fapi/v1/listenKey", None),
                                  ("PUT", "/fapi/v1/listenKey", None)]


@pytest.mark.asyncio
async def test_order_trade_backfill_is_exact_and_rejects_full_page():
    client = FakeClient()
    client.responses[("GET", "/fapi/v1/userTrades")] = [{"orderId": 7, "id": 10}]
    assert await GridExchange(client).order_trades(7) == [{"orderId": 7, "id": 10}]
    assert client.calls[-1] == ("GET", "/fapi/v1/userTrades",
                                {"symbol": "ETHUSDT", "orderId": 7, "limit": 1000})
    client.responses[("GET", "/fapi/v1/userTrades")] = [{}] * 1000
    with pytest.raises(ValueError, match="incomplete"):
        await GridExchange(client).order_trades(7)
