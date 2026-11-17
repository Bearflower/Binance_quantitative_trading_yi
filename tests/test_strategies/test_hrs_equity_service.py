import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import pytest_asyncio

from strategies.hrs.equity_service import HrsEquityService

DSN = os.environ.get('HRS_TEST_POSTGRES_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='isolated PostgreSQL DSN required')
T = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def setup(monkeypatch):
    import asyncpg
    from shared.database import DatabaseManager
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    account = 'equity-test-' + uuid.uuid4().hex
    monkeypatch.setenv('TRADE_ACCOUNT_ID', account)
    monkeypatch.setenv('HRS_EQUITY_ALLOW_INCEPTION', 'true')
    async with pool.acquire() as c:
        await c.execute('CREATE SCHEMA IF NOT EXISTS trading')
        # Existing schemas are reused; no production or other test rows are changed.
        source = Path('database/postgres/init-scripts/11-execution-facts.sql').read_text()
        source = '\n'.join(line for line in source.splitlines() if not line.strip().startswith('GRANT '))
        await c.execute(source)
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.trade_records
            (id bigserial PRIMARY KEY,account_id text,symbol text,order_id text,strategy text,
             side text,status text,order_type text,quantity numeric,realized_pnl numeric,
             executed_at timestamp)''')
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.income_ingestion_attempt
            (attempt_id text PRIMARY KEY,account_id text,observations jsonb,state text DEFAULT 'pending',
             created_at timestamptz DEFAULT now(),finished_at timestamptz)''')
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.reviewed_income_attribution
            (account_id text,income_type text,tran_id bigint,strategy text,symbol text,income numeric,
             asset text,event_time_utc timestamptz,evidence jsonb,reviewed_by text)''')
        await c.execute('''CREATE TABLE IF NOT EXISTS public.capital_allocation
            (id serial PRIMARY KEY, month text,entries jsonb,status text,created_at timestamp)''')
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.strategy_risk_policy
            (account_id text,strategy_id text,revision bigint,effective_at timestamptz,expires_at timestamptz,
             PRIMARY KEY(account_id,strategy_id,revision))''')
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.strategy_risk_active
            (account_id text,strategy_id text,revision bigint,PRIMARY KEY(account_id,strategy_id))''')
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.entry_risk_reservation
            (client_order_id text PRIMARY KEY,account_id text,strategy_id text,state text,symbol text)''')
        for path in ('database/postgres/migrations/2026-11-17-hrs-risk-ledger-loss-gates.sql',
                     'database/postgres/migrations/2026-11-17-hrs-equity-service.sql'):
            sql = Path(path).read_text()
            await c.execute(sql)
            await c.execute(sql)
        allocation_id = await c.fetchval('''INSERT INTO public.capital_allocation(month,entries,status,created_at)
            VALUES ('2026-11',$1::jsonb,'active',$2) RETURNING id''', json.dumps([dict(strategy_id='hrs', allocated_amount='1000')]),
            T.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
        # Schema may have the full risk policy columns from another acceptance test.
        cols = {r['column_name'] for r in await c.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema='trading' AND table_name='strategy_risk_policy'")}
        if 'mode' in cols:
            await c.execute('''INSERT INTO trading.strategy_risk_policy
                (account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason)
                VALUES ($1,'hrs',1,$2,$3,'reduced',false,.5,.15,100,'test')''',account,T-timedelta(days=1),T+timedelta(days=2))
        else:
            await c.execute("INSERT INTO trading.strategy_risk_policy VALUES ($1,'hrs',1,$2,$3)",account,T-timedelta(days=1),T+timedelta(days=2))
        await c.execute("INSERT INTO trading.strategy_risk_active VALUES ($1,'hrs',1)",account)
    db = DatabaseManager('unused',1,'unused','unused','',1,4)
    db.pool = pool
    api = AsyncMock()
    api.use_unified_account = True
    api.get_position.return_value = []
    api.get_um_account_positions.return_value = []
    api.get_open_orders.return_value = []
    api.get_income_history.return_value = []
    api.get_user_trades.return_value = []
    pm = SimpleNamespace(get_all_positions=lambda: {})
    service = HrsEquityService(db, api, SimpleNamespace(strategy_id='hrs'), pm)
    clock = [T]
    service.clock = lambda: clock[0]
    try:
        yield service, account, pool, api, clock
    finally:
        await service.close()
        async with pool.acquire() as c:
            for table in ('hrs_equity_evidence','hrs_equity_segment','strategy_equity_gate',
                          'hrs_loss_gate_reset_audit','hrs_risk_ledger_events','hrs_risk_equity_checkpoints',
                          'hrs_loss_gate_state','execution_fact_income','execution_fact_fill',
                          'execution_fact_cursor','income_ingestion_attempt','trade_records',
                          'entry_risk_reservation','strategy_risk_active'):
                await c.execute(f'DELETE FROM trading.{table} WHERE account_id=$1', account)
            await c.execute('DELETE FROM public.capital_allocation WHERE id=$1',allocation_id)
        await pool.close()


@pytest.mark.asyncio
async def test_actual_flat_bootstrap_is_persistent_but_partial_day_stays_not_ready(setup):
    service, account, pool, api, clock = setup
    result = await service.refresh()
    assert not result['allowed']
    assert 'daily_origin_missing' in result['reasons']
    async with pool.acquire() as c:
        segment = await c.fetchrow('SELECT * FROM trading.hrs_equity_segment WHERE account_id=$1',account)
        row = await c.fetchrow('SELECT * FROM trading.strategy_equity_gate WHERE account_id=$1',account)
        assert segment['inception'] == T
        assert row['ready'] is False and row['allowed'] is False
        assert row['valid_until'] <= row['as_of'] + timedelta(seconds=120)
        assert len(row['evidence_sha256']) == 64
    restarted = HrsEquityService(service.db,api,service.capital_mgr,service.position_manager)
    restarted.clock = service.clock
    clock[0] += timedelta(seconds=30)
    await restarted.refresh()
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT count(*) FROM trading.hrs_equity_segment WHERE account_id=$1',account) == 1
    await restarted.close()


@pytest.mark.asyncio
async def test_midnight_flat_reconstruction_has_real_available_at_and_fresh_projection(setup):
    service, account, pool, api, clock = setup
    await service.refresh()
    clock[0] = T + timedelta(hours=1, seconds=30)
    result = await service.refresh()
    assert result['ready'] and result['allowed']
    async with pool.acquire() as c:
        boundary = await c.fetchrow('SELECT as_of,payload FROM trading.hrs_risk_equity_checkpoints WHERE account_id=$1 AND as_of=$2',account,T+timedelta(hours=1))
        assert boundary is not None
        evidence = await c.fetchrow('SELECT * FROM trading.hrs_equity_evidence WHERE account_id=$1 ORDER BY available_at DESC LIMIT 1',account)
        assert evidence['available_at'] == clock[0]
        gate = await c.fetchrow('SELECT * FROM trading.strategy_equity_gate WHERE account_id=$1',account)
        assert gate['as_of'] == clock[0]
        assert gate['policy_revision'] == 1


@pytest.mark.asyncio
async def test_pending_or_unknown_entry_prevents_bootstrap_without_resetting_later(setup):
    service, account, pool, api, clock = setup
    api.get_open_orders.return_value = [dict(symbol='BTCUSDT',orderId=9,reduceOnly=False)]
    result = await service.refresh()
    assert not result['allowed']
    assert 'outstanding_entry_owner_unproven' in result['reasons']
    async with pool.acquire() as c:
        assert not await c.fetchval('SELECT 1 FROM trading.hrs_equity_segment WHERE account_id=$1',account)
    api.get_open_orders.return_value=[]
    await service.refresh()
    clock[0] += timedelta(seconds=30)
    api.get_income_history.side_effect=RuntimeError('offline')
    result=await service.refresh()
    assert not result['allowed']
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT count(*) FROM trading.hrs_equity_segment WHERE account_id=$1',account)==1
        assert not await c.fetchval('SELECT allowed FROM trading.strategy_equity_gate WHERE account_id=$1',account)


@pytest.mark.asyncio
async def test_source_collection_requeries_completed_windows_and_detects_late_income(setup):
    service, account, pool, api, clock = setup
    await service.refresh()
    old = T + timedelta(seconds=10)
    clock[0] += timedelta(seconds=30)
    await service.refresh()
    calls=api.get_income_history.await_count
    # Late HRS income lacks exact fill proof and must invalidate, never become zero.
    api.get_income_history.side_effect=lambda **kw: ([dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-1',
        asset='USDT',tranId=8,tradeId='3',time=int(old.timestamp()*1000),info='')]
        if kw['income_type']=='COMMISSION' else [])
    clock[0]+=timedelta(seconds=30)
    result=await service.refresh()
    assert not result['allowed']
    assert api.get_income_history.await_count > calls
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT count(*) FROM trading.execution_fact_income WHERE account_id=$1',account)==1


@pytest.mark.asyncio
async def test_income_exactly_at_valuation_millisecond_is_not_omitted(setup):
    service, account, pool, api, clock = setup
    await service.refresh()
    clock[0] += timedelta(seconds=30)
    ms = int(clock[0].timestamp()*1000)
    api.get_income_history.side_effect=lambda **kw: ([dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-1',asset='USDT',
        tranId=50,tradeId='7',time=ms,info='')] if kw['income_type']=='COMMISSION' else [])
    api.get_user_trades.return_value=[dict(symbol='BTCUSDT',id=7,orderId=10,side='BUY',qty='1',price='100',
        quoteQty='100',commission='1',commissionAsset='USDT',realizedPnl='0',time=ms)]
    async with pool.acquire() as c:
        await c.execute("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,executed_at) VALUES ($1,'BTCUSDT','10','hrs','BUY','NEW','LIMIT',$2)",account,clock[0].replace(tzinfo=None))
    snapshot=await service._capture()
    await service._collect(int(T.timestamp()*1000),ms,snapshot)
    async with service._locked() as c:
        report,*_=await service._read_source(c,int(T.timestamp()*1000),ms,snapshot)
    assert any(row['income_type']=='COMMISSION' and row['amount']=='-1.00000000' for row in report['events'])


@pytest.mark.asyncio
async def test_projection_denial_and_final_authorization_share_account_lock(setup, monkeypatch):
    from shared.equity_entry_gate import enforce_equity_gate, EquityGateDenied
    service, account, pool, api, clock=setup
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
    async with service._locked() as c:
        now=await c.fetchval('SELECT clock_timestamp()')
        await service._projection(c,1,dict(ready=True,allowed=True,reasons=[]),
                                  dict(as_of=now,available_at=now,segment_id=None))
    entered=asyncio.Event()
    release=asyncio.Event()
    async def authorization():
        async with pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,7183))',account)
            await enforce_equity_gate(c,'hrs',account,1)
            entered.set()
            await release.wait()
    reader=asyncio.create_task(authorization())
    await entered.wait()
    writer=asyncio.create_task(service._deny('bad_source'))
    await asyncio.sleep(.05)
    assert not writer.done()
    release.set()
    await reader
    result=await writer
    assert result['valid_until']==result['as_of']
    assert result['policy_revision']==0
    async with pool.acquire() as c:
        with pytest.raises(EquityGateDenied):
            await enforce_equity_gate(c,'hrs',account,1)
