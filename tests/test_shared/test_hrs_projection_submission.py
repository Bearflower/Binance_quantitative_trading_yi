"""Real PostgreSQL consumer invalidation after entry transport attempts."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import os
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio

from shared.entry_risk_policy import EntryDenied, RiskPolicyStore
from shared.equity_entry_gate import EquityGateDenied, enforce_equity_gate
from shared.order_submission_authorization import (
    authorize_submission, mark_transport_started, reject_before_transport,
)

PARAMS = {'symbol': 'TESTUSDT'}
POST = ('POST', '/fapi/v1/order', PARAMS)


@pytest_asyncio.fixture
async def setup(monkeypatch):
    import asyncpg
    dsn = os.getenv('HRS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('isolated PostgreSQL DSN required')
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES', 'hrs')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    schema = 'hrs_projection_'+uuid.uuid4().hex
    class Connection:
        def __init__(self, connection): self.connection = connection
        def transaction(self): return self.connection.transaction()
        async def execute(self, sql, *args):
            return await self.connection.execute(sql.replace('trading.', schema+'.'), *args)
        async def fetchrow(self, sql, *args):
            return await self.connection.fetchrow(sql.replace('trading.', schema+'.'), *args)
        async def fetchval(self, sql, *args):
            return await self.connection.fetchval(sql.replace('trading.', schema+'.'), *args)
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            async with pool.acquire() as connection:
                yield Connection(connection)
    wrapped = Pool()
    try:
        async with pool.acquire() as c:
            await c.execute(f'''CREATE SCHEMA {schema};
                CREATE TABLE {schema}.income_ingestion_attempt(account_id text,state text);
                CREATE TABLE {schema}.strategy_risk_policy(account_id text,strategy_id text,
                    revision bigint,mode text,allow_add boolean,effective_at timestamptz,expires_at timestamptz);
                CREATE TABLE {schema}.strategy_risk_active(account_id text,strategy_id text,revision bigint);
                CREATE TABLE {schema}.strategy_equity_gate(account_id text,strategy_id text,policy_revision bigint,
                    ready boolean,allowed boolean,as_of timestamptz,valid_until timestamptz,reasons jsonb,
                    evidence_sha256 text,updated_at timestamptz DEFAULT clock_timestamp());
                CREATE TABLE {schema}.entry_risk_reservation(client_order_id text PRIMARY KEY,
                    account_id text,strategy_id text,revision bigint,state text,symbol text,
                    updated_at timestamptz,geometry jsonb DEFAULT '{{}}');
                CREATE TABLE {schema}.position_claims(symbol text,strategy text,claim_state text,
                    released_at timestamptz,reason text);
                INSERT INTO {schema}.strategy_risk_policy VALUES('audit','hrs',6,'reduced',false,
                    clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour');
                INSERT INTO {schema}.strategy_risk_active VALUES('audit','hrs',6);
                INSERT INTO {schema}.strategy_equity_gate(account_id,strategy_id,policy_revision,ready,allowed,as_of,valid_until,reasons)
                    SELECT account,strategy,6,true,true,clock_timestamp()-interval '1 second',clock_timestamp()+interval '90 seconds','[]'
                    FROM (VALUES('audit','hrs'),('audit','mtpc'),('other','hrs')) AS scopes(account,strategy);
                INSERT INTO {schema}.entry_risk_reservation VALUES('intent','audit','hrs',6,'reserved','TESTUSDT',clock_timestamp()-interval '5 seconds','{{}}');
                INSERT INTO {schema}.position_claims VALUES('TESTUSDT','hrs','PENDING',NULL,NULL);''')
        yield RiskPolicyStore(SimpleNamespace(pool=wrapped), account='audit'), wrapped, schema, pool
    finally:
        async with pool.acquire() as c:
            await c.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        await pool.close()


async def current_gate(setup):
    _, wrapped, _, _ = setup
    async with wrapped.acquire() as c:
        return await c.fetchrow("SELECT * FROM trading.strategy_equity_gate WHERE account_id='audit' AND strategy_id='hrs'")


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'unknown', 'cancel'])
async def test_any_attempt_invalidates_and_commits_before_return_or_error(setup, outcome):
    store, wrapped, _, _ = setup
    entered = asyncio.Event()
    async def submit():
        async with store.submitting('hrs', 'intent'):
            # Its own phase1 marker must not self-deny the final HTTP check.
            await authorize_submission(*POST)
            assert (await current_gate(setup))['ready'] is True
            mark_transport_started(*POST)
            entered.set()
            if outcome == 'unknown':
                raise TimeoutError('synthetic unknown exchange result')
            if outcome == 'cancel':
                await asyncio.Event().wait()
    task = asyncio.create_task(submit())
    if outcome == 'cancel':
        await entered.wait()
        task.cancel()
    if outcome == 'success':
        await task
    else:
        with pytest.raises(asyncio.CancelledError if outcome == 'cancel' else TimeoutError):
            await task
    row = await current_gate(setup)
    assert row['ready'] is False and row['allowed'] is False
    assert 'entry_submission_requires_equity_refresh' in row['reasons']
    async with wrapped.acquire() as c:
        assert await c.fetchval("SELECT state FROM trading.entry_risk_reservation WHERE client_order_id='intent'") == 'submitting'
        assert await c.fetchval("SELECT updated_at > $1 FROM trading.entry_risk_reservation WHERE client_order_id='intent'", row['as_of']) is True
        for account,strategy in [('other','hrs'),('audit','mtpc')]:
            assert await c.fetchval('SELECT ready FROM trading.strategy_equity_gate WHERE account_id=$1 AND strategy_id=$2',account,strategy) is True


@pytest.mark.asyncio
async def test_proven_pre_http_denial_keeps_fresh_projection_and_releases_only_own_intent(setup):
    store, wrapped, _, _ = setup
    with pytest.raises(RuntimeError, match='synthetic_capture_denial'):
        async with store.submitting('hrs','intent'):
            await authorize_submission(*POST)
            reject_before_transport(*POST)
            raise RuntimeError('synthetic_capture_denial')
    assert (await current_gate(setup))['ready'] is True
    async with wrapped.acquire() as c:
        assert await c.fetchval("SELECT state FROM trading.entry_risk_reservation WHERE client_order_id='intent'") == 'released'
    await store.policy('hrs')


@pytest.mark.asyncio
@pytest.mark.parametrize('account,strategy,state,newer,blocked', [
    ('audit','hrs','submitting',True,True),
    ('audit','hrs','submitting',False,False),
    ('audit','hrs','reserved',True,False),
    ('other','hrs','submitting',True,False),
    ('audit','mtpc','submitting',True,False),
])
async def test_other_submitting_marker_blocks_stale_projection_with_exact_scope(
        setup,account,strategy,state,newer,blocked):
    store, wrapped, _, _ = setup
    async with wrapped.acquire() as c:
        await c.execute('''INSERT INTO trading.entry_risk_reservation VALUES
            ('other-intent',$1,$2,6,$3,'OTHERUSDT',clock_timestamp()+$4::interval,'{}')''',
            account,strategy,state,timedelta(0) if newer else timedelta(seconds=-5))
    if blocked:
        with pytest.raises(EntryDenied,match='equity_gate_unreconciled_submission'):
            await store.policy('hrs')
    else:
        await store.policy('hrs')


@pytest.mark.asyncio
async def test_equal_microsecond_submission_cannot_be_proven_in_projection(setup):
    store, wrapped, _, _ = setup
    async with wrapped.acquire() as c:
        await c.execute("""INSERT INTO trading.entry_risk_reservation
            SELECT 'same-instant','audit','hrs',6,'submitting','OTHERUSDT',as_of,'{}'
            FROM trading.strategy_equity_gate WHERE account_id='audit' AND strategy_id='hrs'""")
    with pytest.raises(EntryDenied,match='equity_gate_unreconciled_submission'):
        await store.policy('hrs')


@pytest.mark.asyncio
async def test_only_explicit_current_submission_gets_self_exclusion(setup):
    store, wrapped, _, _ = setup
    async with store.submitting('hrs','intent'):
        await authorize_submission(*POST)
        # Ordinary sizing/reservation policy reads have no exception for it.
        with pytest.raises(EntryDenied,match='equity_gate_unreconciled_submission'):
            await store.policy('hrs')
        async with wrapped.acquire() as c:
            with pytest.raises(EquityGateDenied,match='equity_gate_unreconciled_submission'):
                await enforce_equity_gate(c,'hrs','audit',6)
    # No HTTP and no synthetic denial: conservatively retain occupancy.
    async with wrapped.acquire() as c:
        assert await c.fetchval("SELECT state FROM trading.entry_risk_reservation WHERE client_order_id='intent'") == 'submitting'
    with pytest.raises(EntryDenied,match='equity_gate_unreconciled_submission'):
        await store.policy('hrs')


@pytest.mark.asyncio
async def test_freshly_verified_producer_projection_can_supersede_old_marker(setup):
    store, wrapped, _, _ = setup
    async with store.submitting('hrs','intent'):
        await authorize_submission(*POST)
        mark_transport_started(*POST)
    async with wrapped.acquire() as c:
        # Simulates a later producer reconciliation, not a consumer bypass.
        await c.execute("""UPDATE trading.strategy_equity_gate SET ready=true,allowed=true,
            as_of=clock_timestamp(),valid_until=clock_timestamp()+interval '90 seconds',reasons='[]'
            WHERE account_id='audit' AND strategy_id='hrs'""")
    await store.policy('hrs')


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_invalidation_commit(setup,monkeypatch):
    import shared.equity_entry_gate as gate
    store, _, _, _ = setup
    paused, resume = asyncio.Event(), asyncio.Event()
    original = gate.invalidate_after_submission
    async def delayed(*args):
        paused.set()
        await resume.wait()
        await original(*args)
    monkeypatch.setattr(gate,'invalidate_after_submission',delayed)
    async def submit():
        async with store.submitting('hrs','intent'):
            await authorize_submission(*POST)
            mark_transport_started(*POST)
    task = asyncio.create_task(submit())
    await paused.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), 'connection must remain held until invalidation commits'
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await current_gate(setup))['ready'] is False


@pytest.mark.asyncio
async def test_failed_invalidation_rolls_back_but_durable_marker_blocks_reuse(setup,monkeypatch):
    import shared.equity_entry_gate as gate
    store, wrapped, _, _ = setup
    async def fail(*args):
        raise RuntimeError('synthetic_database_failure')
    monkeypatch.setattr(gate,'invalidate_after_submission',fail)
    with pytest.raises(RuntimeError,match='synthetic_database_failure'):
        async with store.submitting('hrs','intent'):
            await authorize_submission(*POST)
            mark_transport_started(*POST)
    # Simulates a crash/rollback gap: the older ready projection still exists,
    # but it cannot authorize another entry after a newer durable marker.
    assert (await current_gate(setup))['ready'] is True
    async with wrapped.acquire() as c:
        assert await c.fetchval("SELECT state FROM trading.entry_risk_reservation WHERE client_order_id='intent'") == 'submitting'
    with pytest.raises(EntryDenied,match='equity_gate_unreconciled_submission'):
        await store.policy('hrs')


@pytest.mark.asyncio
async def test_another_newer_intent_blocks_current_self_exclusion(setup):
    store, wrapped, _, _ = setup
    async with store.submitting('hrs','intent'):
        async with wrapped.acquire() as c:
            await c.execute("""INSERT INTO trading.entry_risk_reservation VALUES
                ('other-intent','audit','hrs',6,'submitting','OTHERUSDT',clock_timestamp(),'{}')""")
        with pytest.raises(EntryDenied,match='equity_gate_unreconciled_submission'):
            await authorize_submission(*POST)
    async with wrapped.acquire() as c:
        assert await c.fetchval("SELECT state FROM trading.entry_risk_reservation WHERE client_order_id='intent'") == 'released'
        assert await c.fetchval("SELECT state FROM trading.entry_risk_reservation WHERE client_order_id='other-intent'") == 'submitting'


@pytest.mark.asyncio
async def test_exit_authorization_is_not_blocked_by_invalid_projection(setup):
    store, _, _, _ = setup
    async with store.submitting('hrs','intent'):
        await authorize_submission(*POST)
        mark_transport_started(*POST)
    assert (await current_gate(setup))['ready'] is False
    await authorize_submission('POST','/fapi/v1/order',{'symbol':'TESTUSDT','reduceOnly':True})
    await authorize_submission('POST','/papi/v1/um/order',{'symbol':'TESTUSDT','closePosition':'true'})
