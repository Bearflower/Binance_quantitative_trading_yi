from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.binance_api import BinanceClient
from strategies.hrs.decision_capture import DecisionRecorder


class ReachedTransport(RuntimeError):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize('reduce_only', [False, True, 'true'])
async def test_capture_failure_during_last_preparation_blocks_only_new_risk(reduce_only):
    client = BinanceClient('test_api_key_12345', 'test_api_secret_67890')
    client.use_unified_account = False
    client._init_session = AsyncMock()
    client.session = MagicMock()
    client.session.request.side_effect = ReachedTransport('HTTP transport reached')
    recorder = DecisionRecorder(AsyncMock())
    recorder.attach_public_transport(client)

    async def late_preparation():
        recorder.failed = True

    client.rate_limiter.acquire = AsyncMock(side_effect=late_preparation)
    expected = ReachedTransport if reduce_only else RuntimeError
    message = 'HTTP transport reached' if reduce_only else 'decision_evidence_incomplete'
    with pytest.raises(expected, match=message):
        await client._request_once('POST', '/fapi/v1/order',
            {'symbol': 'TESTUSDT', 'side': 'BUY', 'type': 'MARKET', 'reduceOnly': reduce_only})
    assert client.session.request.call_count == (1 if reduce_only else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('reduce_only', [False, True])
async def test_policy_is_rechecked_after_rate_limit_wait(reduce_only):
    from contextlib import asynccontextmanager
    from shared.entry_risk_policy import RiskPolicyStore, EntryDenied

    connection = AsyncMock()
    connection.transaction = MagicMock()
    @asynccontextmanager
    async def transaction():
        yield
    connection.transaction.side_effect = transaction
    connection.fetchrow.return_value = {'revision': 6, 'symbol': 'TESTUSDT'}
    pool = MagicMock()
    @asynccontextmanager
    async def acquire():
        yield connection
    pool.acquire.side_effect = acquire
    db = MagicMock(pool=pool)
    store = RiskPolicyStore(db)
    valid = True
    async def policy(strategy, held_connection=None):
        assert held_connection is connection
        if not valid:
            raise EntryDenied('equity_gate_stale_or_invalid')
        return {'revision': 6, 'mode': 'reduced'}
    store.policy = policy
    client = BinanceClient('test_api_key_12345', 'test_api_secret_67890')
    client.use_unified_account = False
    client._init_session = AsyncMock()
    client.session = MagicMock()
    client.session.request.side_effect = ReachedTransport('HTTP transport reached')
    async def rate_wait():
        nonlocal valid
        valid = False
    client.rate_limiter.acquire = AsyncMock(side_effect=rate_wait)
    expected = ReachedTransport if reduce_only else EntryDenied
    async with store.submitting('hrs', 'test-intent'):
        with pytest.raises(expected):
            await client._request_once('POST', '/fapi/v1/order',
                {'symbol': 'TESTUSDT', 'reduceOnly': reduce_only})
    assert client.session.request.call_count == int(reduce_only)
    # The authorization context must not leak to other clients/tasks after exit.
    with pytest.raises(ReachedTransport):
        await client._request_once('POST', '/fapi/v1/order', {'symbol': 'TESTUSDT'})
