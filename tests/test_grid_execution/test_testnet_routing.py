from unittest.mock import AsyncMock

import pytest

from shared.binance_api import BinanceClient


class Response:
    content_type = "application/json"
    status = 200
    reason = "OK"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def json(self):
        return {"serverTime": 1}

    def raise_for_status(self):
        pass


class Session:
    closed = False

    def __init__(self):
        self.url = None

    def request(self, method, url, params):
        self.url = url
        return Response()


@pytest.mark.asyncio
async def test_unsigned_ordinary_usdm_requests_obey_testnet_base():
    client = BinanceClient("test-key", "test-secret", testnet=True,
                           use_unified_account=False)
    client.session = Session()
    client.rate_limiter.acquire = AsyncMock()
    await client._request_once("GET", "/fapi/v1/time", signed=False)
    assert client.session.url == "https://testnet.binancefuture.com/fapi/v1/time"
