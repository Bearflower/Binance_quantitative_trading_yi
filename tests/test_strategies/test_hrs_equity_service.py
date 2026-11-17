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
        await c.execute('ALTER TABLE public.capital_allocation ADD COLUMN IF NOT EXISTS total_capital numeric')
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.strategy_risk_policy
            (account_id text,strategy_id text,revision bigint,effective_at timestamptz,expires_at timestamptz,
             PRIMARY KEY(account_id,strategy_id,revision))''')
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.strategy_risk_active
            (account_id text,strategy_id text,revision bigint,PRIMARY KEY(account_id,strategy_id))''')
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.entry_risk_reservation
            (client_order_id text PRIMARY KEY,account_id text,strategy_id text,state text,symbol text)''')
        for path in ('database/postgres/migrations/2026-11-17-hrs-risk-ledger-loss-gates.sql',
                     'database/postgres/migrations/2026-11-17-hrs-equity-service.sql',
                     'database/postgres/migrations/2026-11-17-hrs-allocation-event-audit.sql',
                     'database/postgres/migrations/2026-11-17-hrs-equity-reconciliation.sql',
                     'database/postgres/migrations/2026-11-17-hrs-funding-publication.sql'):
            sql = Path(path).read_text()
            await c.execute(sql)
            await c.execute(sql)
        await c.execute('''CREATE TABLE IF NOT EXISTS public.ai_tuner_funding_schedule_observations
            (attempt_id uuid,symbol text,source_time timestamptz,received_at timestamptz,next_funding_time timestamptz,
             funding_interval_hours int,payload_hash text,raw_payload jsonb)''')
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
    api.get_funding_rate_history.return_value = []
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


@pytest.mark.asyncio
async def test_waiting_collector_snapshot_sees_reservation_committed_by_lock_holder(setup):
    service, account, pool, api, clock = setup
    async with pool.acquire() as holder:
        transaction = holder.transaction()
        await transaction.start()
        await holder.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,7183))',account)
        pid = await holder.fetchval('SELECT pg_backend_pid()')
        async def reader():
            async with service._locked() as c:
                return await c.fetchval("SELECT count(*) FROM trading.entry_risk_reservation WHERE account_id=$1 AND state<>'released'",account)
        task=asyncio.create_task(reader())
        try:
            async with pool.acquire() as observer:
                for _ in range(100):
                    blocked=await observer.fetchval('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE $1=ANY(pg_blocking_pids(pid)))',pid)
                    if blocked:
                        break
                    await asyncio.sleep(.01)
                assert blocked
            await holder.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,state,symbol) VALUES ($1,$2,'hrs','reserved','BTCUSDT')",uuid.uuid4().hex,account)
            await transaction.commit()
            assert await task == 1
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            if holder.is_in_transaction():
                await transaction.rollback()


@pytest.mark.asyncio
async def test_allocation_deposit_and_withdrawal_are_two_flows_even_when_net_zero(setup):
    service, account, pool, api, clock=setup
    # Use actual source time so the database trigger's causal timestamps and the
    # observation clock describe the same prospective segment.
    async with pool.acquire() as c:
        now=await c.fetchval('SELECT clock_timestamp()')
        await c.execute("UPDATE public.capital_allocation SET created_at=$1,total_capital=1000.12345678 WHERE status='active'",(now-timedelta(minutes=1)).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
    service.clock=lambda:datetime.now(timezone.utc)
    opening=await service.refresh()
    assert 'daily_origin_missing' in opening['reasons']
    async with pool.acquire() as c:
        await c.execute("UPDATE public.capital_allocation SET entries='[{\"strategy_id\":\"hrs\",\"allocated_amount\":\"2000\"}]'::jsonb WHERE status='active'")
        await c.execute("UPDATE public.capital_allocation SET entries='[{\"strategy_id\":\"hrs\",\"allocated_amount\":\"1000\"}]'::jsonb WHERE status='active'")
    result=await service.refresh()
    assert 'allocation_event_history_missing' not in result['reasons']
    async with pool.acquire() as c:
        raw=await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account)
        state=json.loads(raw) if isinstance(raw,str) else raw
        assert [D(event['amount']) for event in state['cash_flows']]==[D('1000'),D('-1000')]
        assert D(state['units'])==D('1000')
        assert D(state['nav'])==D('1')


@pytest.mark.asyncio
async def test_cancelled_collection_releases_policy_and_income_session_locks(setup):
    service,account,pool,api,clock=setup
    entered=asyncio.Event()
    async def work():
        async with service._locked():
            entered.set()
            await asyncio.Event().wait()
    async with pool.acquire() as observer:
        task=asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await observer.fetchval('SELECT pg_try_advisory_xact_lock(hashtextextended($1,7183))',account)
        key='income-integrity-v1:'+account
        assert await observer.fetchval('SELECT pg_try_advisory_lock(hashtextextended($1,0))',key)
        await observer.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))',key)


@pytest.mark.asyncio
async def test_allocation_trigger_records_inplace_activation_and_history_is_immutable(setup):
    service,account,pool,api,clock=setup
    async with pool.acquire() as c:
        maximum=await c.fetchval('SELECT COALESCE(max(event_id),0) FROM trading.hrs_allocation_event')
        await c.execute("UPDATE public.capital_allocation SET status='draft' WHERE status='active'")
        await c.execute("UPDATE public.capital_allocation SET status='active' WHERE status='draft'")
        events=await c.fetch('SELECT * FROM trading.hrs_allocation_event WHERE event_id>$1 ORDER BY event_id',maximum)
        assert len(events)==2
        assert events[0]['transaction_id']!=events[1]['transaction_id']
        first=json.loads(events[0]['new_row'])
        second=json.loads(events[1]['new_row'])
        assert first['status']=='draft' and second['status']=='active'
        for query in ('DELETE FROM trading.hrs_allocation_event',
                      'UPDATE trading.hrs_allocation_audit_control SET version=1',
                      'TRUNCATE public.capital_allocation'):
            with pytest.raises(Exception,match='immutable'):
                await c.execute(query)
        assert await c.fetchval("SELECT count(*) FROM public.capital_allocation WHERE status='active'")==1


@pytest.mark.asyncio
async def test_existing_segment_without_prospective_audit_origin_cannot_restart_clean(setup):
    service,account,pool,api,clock=setup
    await service.refresh()
    async with pool.acquire() as c:
        await c.execute('UPDATE trading.hrs_equity_segment SET allocation_audit_event_id=NULL WHERE account_id=$1',account)
        version=await c.fetchval('SELECT version FROM trading.hrs_loss_gate_state WHERE account_id=$1',account)
    clock[0]+=timedelta(seconds=30)
    result=await service.refresh()
    assert result['allowed'] is False
    assert result['reasons']==['allocation_event_history_missing']
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT version FROM trading.hrs_loss_gate_state WHERE account_id=$1',account)==version


@pytest.mark.asyncio
@pytest.mark.parametrize('pre_anchor',[False,True])
async def test_late_proven_roundtrip_is_reconciled_append_only_and_next_refresh_is_normal(setup,pre_anchor):
    from strategies.hrs.equity_reconciliation import effective_checkpoints
    service,account,pool,api,clock=setup
    await service.refresh()
    clock[0]=T+timedelta(seconds=30)
    await service.refresh()
    async with pool.acquire() as c:
        for order_id,side in ((71,'BUY'),(72,'SELL')):
            await c.execute("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,executed_at) VALUES ($1,'BTCUSDT',$2,'hrs',$3,'FILLED','LIMIT',$4)",account,str(order_id),side,T.replace(tzinfo=None))
    async with pool.acquire() as c:
        await c.execute('''INSERT INTO public.ai_tuner_funding_schedule_observations(attempt_id,symbol,source_time,received_at,next_funding_time,funding_interval_hours,payload_hash,raw_payload) VALUES($1,'BTCUSDT',$2,$2,$3,24,'test','{}')''',uuid.uuid4(),T-timedelta(minutes=1),T+timedelta(days=1))
    records=[dict(symbol='BTCUSDT',id=71,orderId=71,side='BUY',qty='1',price='100',quoteQty='100',commission='1',commissionAsset='USDT',realizedPnl='0',time=int((T+timedelta(seconds=-20 if pre_anchor else 10)).timestamp()*1000)),
             dict(symbol='BTCUSDT',id=72,orderId=72,side='SELL',qty='1',price='100',quoteQty='100',commission='1',commissionAsset='USDT',realizedPnl='0',time=int((T+timedelta(seconds=-10 if pre_anchor else 20)).timestamp()*1000))]
    api.get_user_trades.return_value=records
    api.get_income_history.side_effect=lambda **kw: ([dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-1',asset='USDT',tranId=r['id'],tradeId=str(r['id']),time=r['time'],info='') for r in records] if kw['income_type']=='COMMISSION' else [])
    clock[0]=T+timedelta(seconds=60)
    result=await service.refresh()
    assert 'late_income_requires_reconciliation' not in result['reasons']
    assert result.get('reconciliation_receipt_sha256')
    async with pool.acquire() as c:
        original=await c.fetchval('SELECT nav FROM trading.hrs_risk_equity_checkpoints WHERE account_id=$1 AND as_of=$2',account,T+timedelta(seconds=30))
        assert original==D('1')
        segment=await c.fetchval('SELECT segment_id FROM trading.hrs_equity_segment WHERE account_id=$1',account)
        effective={cp.as_of:cp for cp in await effective_checkpoints(c,account,segment)}
        assert effective[T+timedelta(seconds=30)].nav==(D('1') if pre_anchor else D('.998'))
        assert effective[T+timedelta(seconds=30)].units==(D('998') if pre_anchor else D('1000'))
        earlier={cp.as_of:cp for cp in await effective_checkpoints(c,account,segment,known_at=T+timedelta(seconds=45))}
        assert earlier[T+timedelta(seconds=30)].nav==D('1')
        row=await c.fetchrow('SELECT * FROM trading.hrs_equity_reconciliation WHERE account_id=$1',account)
        assert row['available_at']==clock[0]
        assert row['before_state_version']==2 and row['after_state_version']==3
        if pre_anchor:
            receipt=json.loads(row['receipt'])
            assert receipt['opening_anchor_revision']['new_opening_cash']=='998.00000000'
            assert await c.fetchval('SELECT opening_cash FROM trading.hrs_equity_segment WHERE account_id=$1',account)==D('1000')
        with pytest.raises(Exception,match='immutable'):
            await c.execute('DELETE FROM trading.hrs_equity_reconciliation WHERE account_id=$1',account)
    clock[0]+=timedelta(seconds=30)
    again=await service.refresh()
    assert 'reconciliation_receipt_sha256' not in again
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT count(*) FROM trading.hrs_equity_reconciliation WHERE account_id=$1',account)==1
        ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
        assert D(ledger['nav'])==(D('1') if pre_anchor else D('.998'))
        assert D(ledger['cash_allocation'])==(D('998') if pre_anchor else D('1000'))


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_owner',[False,True])
@pytest.mark.parametrize('funding_pending',[False,True])
async def test_existing_hrs_position_has_economic_opening_cash_and_terminal_fill_proof(setup,legacy_owner,funding_pending):
    service,account,pool,api,clock=setup
    entered=T-timedelta(days=1)
    clock[0]=T+timedelta(minutes=59,seconds=30)
    order_id=81
    due=T+timedelta(hours=1) if funding_pending else T+timedelta(days=1)
    client='entry-'+uuid.uuid4().hex
    async with pool.acquire() as c:
        await c.execute('''CREATE TABLE IF NOT EXISTS trading.position_claims
            (id serial PRIMARY KEY,symbol text,strategy text,claim_state text,expires_at timestamptz)''')
        await c.execute('ALTER TABLE trading.entry_risk_reservation ADD COLUMN IF NOT EXISTS quantity numeric')
        schedule_id=uuid.uuid4()
        await c.execute('''INSERT INTO public.ai_tuner_funding_schedule_observations(attempt_id,symbol,source_time,received_at,next_funding_time,funding_interval_hours,payload_hash,raw_payload) VALUES($1,'BTCUSDT',$2,$2,$3,24,'test','{}')''',schedule_id,entered,due)
        claim=await c.fetchval("INSERT INTO trading.position_claims(symbol,strategy,claim_state,expires_at) VALUES ('BTCUSDT','hrs','ACTIVE','infinity') RETURNING id")
        await c.execute("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,executed_at) VALUES ($1,'BTCUSDT','81','hrs','BUY','NEW','LIMIT',$2)",None if legacy_owner else account,entered.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
        await c.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,state,symbol,quantity) VALUES ($1,$2,'hrs','submitting','BTCUSDT',2)",client,account)
    try:
        inventory=[dict(symbol='BTCUSDT',positionAmt='2',entryPrice='100',positionSide='BOTH')]
        api.get_position.return_value=inventory
        api.get_um_account_positions.return_value=inventory
        service.position_manager=SimpleNamespace(get_all_positions=lambda:{'BTCUSDT':dict(direction='long',remaining_quantity='2',position_id='p81')})
        def market_request(method,path,params=None,**kwargs):
            if path=='/fapi/v1/premiumIndex':
                return dict(symbol='BTCUSDT',markPrice='99',time=int(clock[0].timestamp()*1000),nextFundingTime=int((due if clock[0]<due else due+timedelta(days=1)).timestamp()*1000))
            assert path=='/fapi/v1/markPriceKlines'
            return [[params['startTime'],'100','100','99','99','0',params['endTime']]]
        api._request.side_effect=market_request
        api.get_order.return_value=dict(symbol='BTCUSDT',orderId=order_id,side='BUY',status='FILLED')
        api.get_order_by_client_id.return_value=dict(symbol='BTCUSDT',orderId=order_id,clientOrderId=client,status='FILLED',executedQty='2',origQty='2',side='BUY')
        api.get_user_trades.return_value=[dict(symbol='BTCUSDT',id=81,orderId=81,side='BUY',qty='2',price='100',quoteQty='200',commission='1',commissionAsset='USDT',realizedPnl='0',time=int(entered.timestamp()*1000))]
        api.get_income_history.side_effect=lambda **kw: ([dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-1',asset='USDT',tranId=81,tradeId='81',time=int(entered.timestamp()*1000),info='')] if kw['income_type']=='COMMISSION' else [])
        result=await service.refresh()
        assert result['reasons']==['daily_origin_missing']
        async with pool.acquire() as c:
            assert await c.fetchval('SELECT opening_cash FROM trading.hrs_equity_segment WHERE account_id=$1',account)==D('999')
            ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
            assert D(ledger['equity'])==D('997') and D(ledger['income_total'])==0
        if funding_pending:
            async with pool.acquire() as c:
                await c.execute('DELETE FROM public.ai_tuner_funding_schedule_observations WHERE attempt_id=$1',schedule_id)
                with pytest.raises(Exception,match='append-only'):
                    await c.execute('DELETE FROM trading.hrs_funding_observation WHERE account_id=$1',account)
            restarted=HrsEquityService(service.db,api,service.capital_mgr,service.position_manager)
            restarted.clock=service.clock
            service=restarted
        clock[0]+=timedelta(seconds=60)
        again=await service.refresh()
        if funding_pending:
            assert not again['ready'] and not again['allowed']
            assert 'funding_settlement_pending' in again['reasons']
            api.get_funding_rate_history.return_value=[dict(symbol='BTCUSDT',fundingTime=int(due.timestamp()*1000),fundingRate='0',markPrice='99')]
            again=await service.refresh()
        assert again['ready'] and again['allowed']
        assert 'bootstrap_opening_inventory_unproven' not in again['reasons']
        async with pool.acquire() as c:
            ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
            assert D(ledger['nav'])==1
        api.get_order_by_client_id.return_value=dict(symbol='BTCUSDT',orderId=81,clientOrderId=client,status='NEW',executedQty='0',origQty='2',side='BUY')
        clock[0]+=timedelta(seconds=30)
        assert (await service.refresh())['allowed'] is False
    finally:
        async with pool.acquire() as c:
            await c.execute('DELETE FROM trading.position_claims WHERE id=$1',claim)
            await c.execute('DELETE FROM public.ai_tuner_funding_schedule_observations WHERE attempt_id=$1',schedule_id)


def test_held_gap_that_aged_out_of_actual_rolling_window_does_not_block_forever():
    from strategies.hrs.risk_ledger import EquityCheckpoint
    service=HrsEquityService(None,None,None,None)
    now=T+timedelta(days=40)
    inception=now-timedelta(days=32)
    service._anchor_inventory=(dict(strategy='hrs',symbol='BTCUSDT',quantity='1',entry_price='100'),)
    def cp(at):return EquityCheckpoint('seg',at,D('1000'),D('1000'),D('1'))
    samples=[cp(inception),cp(inception+timedelta(days=1))]
    begin=now-timedelta(days=30)
    samples.extend(cp(begin+timedelta(minutes=i)) for i in range(30*24*60+1))
    assert service._history_complete(samples,[],[],inception)
    # The same missing held interval is still a blocker before it ages out.
    assert not service._history_complete(samples[:3],[],[],inception)


@pytest.mark.asyncio
async def test_ready_projection_expires_at_known_held_settlement_before_refresh(setup):
    service,account,pool,api,clock=setup
    due=T+timedelta(seconds=30)
    evidence=dict(account_id=account,as_of=T,available_at=T,capture=dict(local={'BTCUSDT':{}},
        funding_publication=dict(forecasts=[dict(symbol='BTCUSDT',source_time=T,received_at=T,next_funding_time=due)])))
    async with service._locked() as c:
        result=await service._projection(c,1,dict(ready=True,allowed=True,reasons=[]),evidence)
    assert result['valid_until']==due
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT valid_until FROM trading.strategy_equity_gate WHERE account_id=$1',account)==due
