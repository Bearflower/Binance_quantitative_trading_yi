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
    async def policy(strategy, held_connection=None, *, submitting_client_id=None):
        assert held_connection is connection
        assert submitting_client_id in (None, 'test-intent')
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


@pytest.mark.asyncio
@pytest.mark.parametrize('first_attempt_unknown', [False, True])
async def test_proven_pre_http_denial_releases_only_when_no_attempt_was_ever_sent(first_attempt_unknown):
    from contextlib import asynccontextmanager
    from shared.entry_risk_policy import RiskPolicyStore, EntryDenied
    conn = AsyncMock()
    @asynccontextmanager
    async def transaction():
        yield
    conn.transaction = MagicMock(side_effect=transaction)
    conn.fetchrow.return_value = {'revision': 6, 'symbol': 'TESTUSDT'}
    @asynccontextmanager
    async def acquire():
        yield conn
    store = RiskPolicyStore(MagicMock(pool=MagicMock(acquire=MagicMock(side_effect=acquire))))
    valid = True
    async def policy(*args, **kwargs):
        if not valid:
            raise EntryDenied('equity_gate_stale_or_invalid')
        return {'revision': 6, 'mode': 'reduced'}
    store.policy = policy
    client = BinanceClient('test_api_key_12345', 'test_api_secret_67890')
    client.use_unified_account = False
    client._init_session = AsyncMock()
    client.rate_limiter.acquire = AsyncMock()
    client.session = MagicMock()
    client.session.request.side_effect = ReachedTransport('unknown HTTP result')
    with pytest.raises(EntryDenied):
        async with store.submitting('hrs', 'test-intent'):
            if first_attempt_unknown:
                with pytest.raises(ReachedTransport):
                    await client._request_once('POST', '/fapi/v1/order', {'symbol': 'TESTUSDT'})
            valid = False
            await client._request_once('POST', '/fapi/v1/order', {'symbol': 'TESTUSDT'})
    releases = [c for c in conn.execute.await_args_list if "state='released'" in c.args[0]]
    assert len(releases) == (0 if first_attempt_unknown else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('after_scope_exit', [False, True])
async def test_authorization_cannot_leak_a_held_connection_to_child_tasks(after_scope_exit):
    import asyncio
    from shared.order_submission_authorization import bind_submission_authorization, authorize_submission
    ready = asyncio.Event()
    callback = AsyncMock()
    async def child():
        await ready.wait()
        await authorize_submission('POST', '/fapi/v1/order', {'symbol': 'TESTUSDT'})
    with bind_submission_authorization(callback):
        task = asyncio.create_task(child())
        if not after_scope_exit:
            ready.set()
            with pytest.raises(RuntimeError, match='scope_invalid'):
                await task
    if after_scope_exit:
        ready.set()
        with pytest.raises(RuntimeError, match='scope_invalid'):
            await task
    callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('denied_policy_read', [1, 2])
async def test_policy_denial_before_yield_does_not_strand_proven_unsent_reservation(denied_policy_read):
    from contextlib import asynccontextmanager
    from shared.entry_risk_policy import RiskPolicyStore, EntryDenied
    conn = AsyncMock()
    @asynccontextmanager
    async def transaction():
        yield
    conn.transaction = MagicMock(side_effect=transaction)
    conn.fetchrow.return_value = {'revision': 6, 'symbol': 'TESTUSDT', 'state': 'reserved'}
    @asynccontextmanager
    async def acquire():
        yield conn
    store = RiskPolicyStore(MagicMock(pool=MagicMock(acquire=MagicMock(side_effect=acquire))))
    reads = 0
    async def policy(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == denied_policy_read:
            raise EntryDenied('equity_gate_stale_or_invalid')
        return {'revision': 6, 'mode': 'reduced'}
    store.policy = policy
    with pytest.raises(EntryDenied):
        async with store.submitting('hrs', 'test-intent'):
            pytest.fail('submission body must not be reached')
    releases = [c for c in conn.execute.await_args_list if "state='released'" in c.args[0]]
    assert len(releases) == 1


@pytest.mark.asyncio
async def test_pre_http_denial_resolution_commits_in_real_postgres():
    import os
    import uuid
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import asyncpg
    from shared.entry_risk_policy import RiskPolicyStore, EntryDenied
    dsn = os.getenv('HRS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('isolated PostgreSQL DSN required')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    schema = 'submit_test_' + uuid.uuid4().hex
    class Connection:
        def __init__(self, conn): self.conn = conn
        def transaction(self): return self.conn.transaction()
        async def execute(self, sql, *args):
            return await self.conn.execute(sql.replace('trading.', schema+'.'), *args)
        async def fetchrow(self, sql, *args):
            return await self.conn.fetchrow(sql.replace('trading.', schema+'.'), *args)
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            async with pool.acquire() as conn:
                yield Connection(conn)
    try:
        async with pool.acquire() as conn:
            await conn.execute(f'''CREATE SCHEMA {schema};
                CREATE TABLE {schema}.entry_risk_reservation(client_order_id text PRIMARY KEY,
                    account_id text,strategy_id text,state text,symbol text,revision bigint,
                    updated_at timestamptz,geometry jsonb DEFAULT '{{}}');
                CREATE TABLE {schema}.position_claims(symbol text,strategy text,claim_state text,
                    released_at timestamptz,reason text);
                INSERT INTO {schema}.entry_risk_reservation VALUES
                    ('intent','test-account','hrs','reserved','TESTUSDT',6,NULL,'{{}}');
                INSERT INTO {schema}.position_claims VALUES ('TESTUSDT','hrs','PENDING',NULL,NULL);''')
        store = RiskPolicyStore(SimpleNamespace(pool=Pool()), account='test-account')
        valid = True
        async def policy(*args, **kwargs):
            if not valid: raise EntryDenied('equity_gate_stale_or_invalid')
            return {'revision': 6, 'mode': 'reduced'}
        store.policy = policy
        client = BinanceClient('test_api_key_12345', 'test_api_secret_67890')
        client.use_unified_account = False
        client._init_session = AsyncMock()
        client.rate_limiter.acquire = AsyncMock()
        client.session = MagicMock()
        with pytest.raises(EntryDenied):
            async with store.submitting('hrs', 'intent'):
                valid = False
                await client._request_once('POST', '/fapi/v1/order', {'symbol': 'TESTUSDT'})
        client.session.request.assert_not_called()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f'SELECT * FROM {schema}.entry_risk_reservation')
            assert row['state'] == 'released'
            assert 'denied_before_http' in row['geometry']
            assert await conn.fetchval(f'SELECT claim_state FROM {schema}.position_claims') == 'RELEASED'
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        await pool.close()
