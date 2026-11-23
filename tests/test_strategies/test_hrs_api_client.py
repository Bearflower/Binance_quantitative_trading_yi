"""Exercise the real HRS read/write dispatch with synthetic HTTP responses."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest


class Response:
    def __init__(self, status, data=None, html=False, cancelled=False):
        self.status, self.data, self.html, self.cancelled = status, data, html, cancelled
        self.content_type = 'text/html' if html else 'application/json'
        self.reason = 'synthetic'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if self.cancelled:
            raise asyncio.CancelledError
        if self.html:
            raise aiohttp.ContentTypeError(
                SimpleNamespace(real_url='https://papi.binance.com/private?signature=SENSITIVE'),
                (), status=self.status, headers={'Content-Type': self.content_type},
                message='Unexpected mimetype: text/html')
        return self.data


@pytest.fixture
def api(monkeypatch):
    from shared import binance_api
    from strategies.hrs import main
    monkeypatch.setattr(binance_api, 'is_api_retry_enabled', lambda: True)
    monkeypatch.setattr(binance_api, 'get_api_retry', lambda: dict(
        read=dict(max_retries=2, delay_seconds=0, backoff=1),
        write=dict(max_retries=0, retry_after_verify=False)))
    item = main.BinanceClient('synthetic-api-key', 'synthetic-api-secret', use_unified_account=True)
    item._init_session = AsyncMock()
    item.session = SimpleNamespace(request=None)
    return item


def responses(api, values):
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url))
        return values[min(len(calls)-1, len(values)-1)]
    api.session.request = request
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [500, 502, 503, 504])
async def test_transient_html_read_uses_existing_retry_budget_and_same_pm_endpoint(api, status):
    calls = responses(api, [Response(status, html=True), Response(200, data=[])])
    assert await api.get_income_history(start_time=10, end_time=20) == []
    assert calls == [('GET', 'https://papi.binance.com/papi/v1/um/income')]*2


@pytest.mark.asyncio
async def test_exhausted_transient_html_preserves_status_and_stops_at_read_budget(api):
    calls = responses(api, [Response(502, html=True)])
    with pytest.raises(Exception) as error:
        await api.get_user_trades('BNBUSDT', start_time=10, end_time=20)
    assert len(calls) == 3
    assert error.value.status_code == 502
    assert error.value.content_type == 'text/html'
    assert '废弃' not in str(error.value)
    assert 'SENSITIVE' not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 401, 403, 404, 418, 429])
async def test_nontransient_html_does_not_retry_or_change_hosts(api, status):
    calls = responses(api, [Response(status, html=True)])
    with pytest.raises(Exception) as error:
        await api.get_income_history()
    assert len(calls) == 1
    assert error.value.status_code == status
    assert '废弃' not in str(error.value)


@pytest.mark.asyncio
async def test_html_order_post_is_never_reclassified_as_a_retryable_read(api):
    from shared.binance_api import BinanceAPIError
    calls = responses(api, [Response(502, html=True)])
    with pytest.raises(BinanceAPIError) as error:
        await api._request('POST', '/papi/v1/um/order', dict(symbol='BNBUSDT'), idempotency_key='synthetic')
    assert len(calls) == 1
    assert error.value.code == -9999


@pytest.mark.asyncio
async def test_cancelled_read_is_propagated_without_reissue(api):
    calls = responses(api, [Response(200, cancelled=True)])
    with pytest.raises(asyncio.CancelledError):
        await api.get_income_history()
    assert len(calls) == 1
