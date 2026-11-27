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
# Synthetic observations stay after real trigger installation, independent of
# wall-clock run date. The17th keeps preperiod/period fixtures well defined.
T = (datetime.now(timezone.utc).replace(day=28)+timedelta(days=4)).replace(day=17,hour=15,minute=0,second=0,microsecond=0)


def funding_source_response(path):
    if path=='/fapi/v1/fundingInfo':return []
    if path=='/fapi/v1/exchangeInfo':
        return dict(symbols=[dict(symbol='BTCUSDT',underlyingType='COIN',contractType='PERPETUAL',quoteAsset='USDT',marginAsset='USDT')])
    raise AssertionError(path)


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
        await c.execute('ALTER TABLE trading.entry_risk_reservation ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()')
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
            VALUES ($3,$1::jsonb,'active',$2) RETURNING id''', json.dumps([dict(strategy_id='hrs', allocated_amount='1000')]),
            T.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None),T.strftime('%Y-%m'))
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
    api._request.side_effect=lambda method,path,**kw:funding_source_response(path)
    pm = SimpleNamespace(get_all_positions=lambda: {})
    service = HrsEquityService(db, api, SimpleNamespace(strategy_id='hrs'), pm)
    clock = [T]
    service.clock = lambda: clock[0]
    service._test_schedule_ids=[]
    try:
        yield service, account, pool, api, clock
    finally:
        await service.close()
        async with pool.acquire() as c:
            # Segment and reconciliation proofs are append-only. Keep their
            # UUID-account rows rather than disabling their guards for teardown.
            for table in ('hrs_equity_evidence','strategy_equity_gate',
                          'hrs_loss_gate_reset_audit','hrs_risk_ledger_events','hrs_risk_equity_checkpoints',
                          'hrs_loss_gate_state','execution_fact_income','execution_fact_fill',
                          'execution_fact_cursor','income_ingestion_attempt','trade_records',
                          'entry_risk_reservation','strategy_risk_active'):
                await c.execute(f'DELETE FROM trading.{table} WHERE account_id=$1', account)
            await c.execute('DELETE FROM public.capital_allocation WHERE id=$1',allocation_id)
            await c.execute('DELETE FROM public.ai_tuner_funding_schedule_observations WHERE attempt_id=ANY($1::uuid[])',service._test_schedule_ids)
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
    await asyncio.wait_for(entered.wait(),timeout=2)
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
        await c.execute("UPDATE public.capital_allocation SET created_at=$1,total_capital=1000.12345678,month=$2 WHERE status='active'",(now-timedelta(minutes=1)).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None),now.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m'))
        await c.execute("UPDATE trading.strategy_risk_policy SET effective_at=$2,expires_at=$3 WHERE account_id=$1",account,now-timedelta(days=1),now+timedelta(days=2))
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
    from strategies.hrs.equity_evidence import allocation_evidence,canonical
    from strategies.hrs.risk_ledger import LedgerObservation,initialize_ledger
    from strategies.hrs.loss_gate import GateHistory,evaluate_loss_gate
    from strategies.hrs.loss_gate_repository import LossGateSession
    # Seed a genuinely pre-audit format through INSERT. Current segments cannot
    # be damaged via UPDATE, and the production immutable trigger stays enabled.
    segment='legacy-'+uuid.uuid4().hex
    observation=LedgerObservation(T,segment,(),(),(),T,T,True,True)
    ledger=initialize_ledger(segment,D('1000'),observation)
    gate=evaluate_loss_gate(None,ledger.checkpoint,GateHistory((ledger.checkpoint,),T,T,True),allow_inception_segment=True)
    async with pool.acquire() as c,c.transaction():
        row=await c.fetchrow("SELECT id,month,entries,created_at FROM public.capital_allocation WHERE status='active'")
        allocation=allocation_evidence(dict(row),T)
        evidence=dict(account_flat_anchor_certified=True,source_start_ms=int(T.astimezone(timezone(timedelta(hours=8))).replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()*1000))
        await c.execute('''INSERT INTO trading.hrs_equity_segment(account_id,segment_id,inception,opening_cash,opening_allocation_sha256,opening_evidence)
            VALUES($1,$2,$3,1000,$4,$5::jsonb)''',account,segment,T,allocation.identity,canonical(evidence)[0])
        stored=await LossGateSession(c,account,segment).commit(ledger,gate.state,expected_version=None,checkpoint_id='legacy-origin')
        version=stored.version
    clock[0]+=timedelta(seconds=30)
    result=await service.refresh()
    assert result['allowed'] is False
    assert result['reasons']==['allocation_event_history_missing']
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT version FROM trading.hrs_loss_gate_state WHERE account_id=$1',account)==version


@pytest.mark.asyncio
@pytest.mark.parametrize('pre_anchor',[False,True])
@pytest.mark.parametrize('damaged_history',[None,'parent_hash','source_closure','missing_capture','missing_ledger_version','valid_denial'])
async def test_late_proven_roundtrip_is_reconciled_append_only_and_next_refresh_is_normal(setup,pre_anchor,damaged_history):
    from strategies.hrs.equity_reconciliation import effective_checkpoints
    service,account,pool,api,clock=setup
    await service.refresh()
    clock[0]=T+timedelta(seconds=30)
    await service.refresh()
    if damaged_history:
        from strategies.hrs.equity_evidence import canonical
        async with pool.acquire() as c:
            old=await c.fetchrow('SELECT evidence_sha256,payload FROM trading.hrs_equity_evidence WHERE account_id=$1 ORDER BY available_at LIMIT 1',account)
            payload=json.loads(old['payload'])
            if damaged_history=='valid_denial':
                payload={key:payload[key] for key in ('account_id','segment_id','as_of','available_at')}
                payload['result']=dict(ready=False,allowed=False,reasons=['source_collection_incomplete'])
            elif damaged_history=='missing_capture':del payload['capture']
            elif damaged_history=='missing_ledger_version':del payload['result']['ledger_version']
            else:payload['capture']['funding_publication']['forecasts_ref']['ordered_sha256']='0'*64
            encoded,digest=canonical(payload)
            await c.execute('UPDATE trading.hrs_equity_evidence SET payload=$1::jsonb,evidence_sha256=$2 WHERE account_id=$3 AND evidence_sha256=$4',
                encoded,digest if damaged_history in ('source_closure','valid_denial') else old['evidence_sha256'],account,old['evidence_sha256'])
    async with pool.acquire() as c:
        for order_id,side in ((71,'BUY'),(72,'SELL')):
            await c.execute("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,executed_at) VALUES ($1,'BTCUSDT',$2,'hrs',$3,'FILLED','LIMIT',$4)",account,str(order_id),side,T.replace(tzinfo=None))
    async with pool.acquire() as c:
        schedule_id=uuid.uuid4();service._test_schedule_ids.append(schedule_id)
        await c.execute('''INSERT INTO public.ai_tuner_funding_schedule_observations(attempt_id,symbol,source_time,received_at,next_funding_time,funding_interval_hours,payload_hash,raw_payload) VALUES($1,'BTCUSDT',$2,$2,$3,24,'test','{}')''',schedule_id,T-timedelta(minutes=1),T+timedelta(days=1))
    records=[dict(symbol='BTCUSDT',id=71,orderId=71,side='BUY',qty='1',price='100',quoteQty='100',commission='1',commissionAsset='USDT',realizedPnl='0',time=int((T+timedelta(seconds=-20 if pre_anchor else 10)).timestamp()*1000)),
             dict(symbol='BTCUSDT',id=72,orderId=72,side='SELL',qty='1',price='100',quoteQty='100',commission='1',commissionAsset='USDT',realizedPnl='0',time=int((T+timedelta(seconds=-10 if pre_anchor else 20)).timestamp()*1000))]
    api.get_user_trades.return_value=records
    api.get_income_history.side_effect=lambda **kw: ([dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-1',asset='USDT',tranId=r['id'],tradeId=str(r['id']),time=r['time'],info='') for r in records] if kw['income_type']=='COMMISSION' else [])
    clock[0]=T+timedelta(seconds=60)
    result=await service.refresh()
    if damaged_history and damaged_history!='valid_denial':
        assert not result['ready'] and not result['allowed']
        assert result['reasons']==['funding_forecasts_ref_'+('closure_mismatch' if damaged_history=='source_closure' else 'parent_hash_mismatch')]
        async with pool.acquire() as c:
            assert await c.fetchval('SELECT version FROM trading.hrs_loss_gate_state WHERE account_id=$1',account)==2
            assert await c.fetchval('SELECT count(*) FROM trading.hrs_equity_reconciliation WHERE account_id=$1',account)==0
        return
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
    assert again['reasons']==['daily_origin_missing']
    assert again['ledger_version']==4
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT count(*) FROM trading.hrs_equity_reconciliation WHERE account_id=$1',account)==1
        ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
        assert D(ledger['nav'])==(D('1') if pre_anchor else D('.998'))
        assert D(ledger['cash_allocation'])==(D('998') if pre_anchor else D('1000'))
        assert datetime.fromisoformat(ledger['as_of'])==clock[0]
    restarted=HrsEquityService(service.db,api,service.capital_mgr,service.position_manager)
    restarted.clock=service.clock
    clock[0]+=timedelta(seconds=30)
    resumed=await restarted.refresh()
    assert resumed['ledger_version']==5 and resumed['reasons']==['daily_origin_missing']
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT count(*) FROM trading.hrs_equity_reconciliation WHERE account_id=$1',account)==1
        ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
        assert datetime.fromisoformat(ledger['as_of'])==clock[0]
        assert D(ledger['cash_allocation'])==(D('998') if pre_anchor else D('1000'))
    await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_owner',[False,True])
@pytest.mark.parametrize('funding_pending',[False,True])
async def test_existing_hrs_position_has_economic_opening_cash_and_terminal_fill_proof(setup,legacy_owner,funding_pending,repair_damage=None):
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
        schedule_id=uuid.uuid4();service._test_schedule_ids.append(schedule_id)
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
            if path in ('/fapi/v1/fundingInfo','/fapi/v1/exchangeInfo'):return funding_source_response(path)
            assert path=='/fapi/v1/markPriceKlines'
            # 130.78 makes the original gap drawdown just below6%; two
            # genuine late opening fees move its revalued path above6%.
            return [[at,'100','130.78' if repair_damage=='late_income_latch' else '100','99','99','0',at+59999]
                for at in range(params['startTime'],params['endTime']+1,60000)]
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
        if repair_damage in ('funding_gap','funding_exit_gap'):
            event_at=clock[0]+timedelta(seconds=30)
            close=dict(symbol='BTCUSDT',id=82,orderId=82,side='SELL',qty='2',price='101',quoteQty='202',commission='0.2',commissionAsset='USDT',realizedPnl='2',time=int((event_at+timedelta(seconds=30)).timestamp()*1000))
            def gap_income(**kw):
                if kw['income_type']=='COMMISSION':
                    return [dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-1',asset='USDT',tranId=81,tradeId='81',time=int(entered.timestamp()*1000),info='')]+([dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-0.2',asset='USDT',tranId=82,tradeId='82',time=close['time'],info='')] if repair_damage=='funding_exit_gap' else [])
                if kw['income_type']=='FUNDING_FEE':return [dict(symbol='BTCUSDT',incomeType='FUNDING_FEE',income='-1',asset='USDT',tranId=900,tradeId='',time=int(event_at.timestamp()*1000),info='')]
                if kw['income_type']=='REALIZED_PNL' and repair_damage=='funding_exit_gap':return [dict(symbol='BTCUSDT',incomeType='REALIZED_PNL',income='2',asset='USDT',tranId=82,tradeId='82',time=close['time'],info='')]
                return []
            api.get_income_history.side_effect=gap_income
            if repair_damage=='funding_exit_gap':
                async with pool.acquire() as c:
                    await c.execute("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,executed_at) VALUES ($1,'BTCUSDT','82','hrs','SELL','FILLED','MARKET',$2)",account,event_at.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
                api.get_user_trades.return_value=[*api.get_user_trades.return_value,close]
                api.get_order.side_effect=lambda symbol,identity:dict(symbol=symbol,orderId=identity,side='SELL' if str(identity)=='82' else 'BUY',status='FILLED')
                api.get_position.return_value=[];api.get_um_account_positions.return_value=[]
                service.position_manager=SimpleNamespace(get_all_positions=lambda:{})
        # Persist the genuine delayed observation first. The next normal refresh
        # can recover it using original closed marks and exact economic proof.
        clock[0]+=timedelta(seconds=123)
        assert not (await service.refresh())['allowed']
        async with pool.acquire() as c:
            original_points=[dict(row) for row in await c.fetch('SELECT checkpoint_id,payload FROM trading.hrs_risk_equity_checkpoints WHERE account_id=$1 ORDER BY as_of',account)]
        if repair_damage=='many_gaps':
            prepare=service._prepare_automatic_history
            service._prepare_automatic_history=AsyncMock()
            for _ in range(8):
                clock[0]+=timedelta(seconds=123)
                assert not (await service.refresh())['allowed']
            service._prepare_automatic_history=prepare
        if repair_damage=='parent_hash':
            async with pool.acquire() as c:
                await c.execute("UPDATE trading.hrs_equity_evidence SET payload=payload||'{\"damaged\":true}'::jsonb WHERE account_id=$1 AND as_of=$2",account,clock[0])
        elif repair_damage=='missing_marks':
            api._request.side_effect=lambda method,path,**kw: ([] if path=='/fapi/v1/markPriceKlines' else market_request(method,path,**kw))
        elif repair_damage in ('parent_version','receipt_hash'):
            prepare=service._prepare_automatic_history
            async def changed_plan(*args):
                await prepare(*args)
                plan=args[1].get('automatic_history_gap',{})
                if plan.get('status')=='complete':
                    if repair_damage=='parent_version':plan['parent_ledger_version']-=1
                    else:
                        bar=plan['gaps'][0]['bars']['BTCUSDT'][0]
                        bar[3]=bar[4]='98'  # Raw response digest is deliberately unchanged.
            service._prepare_automatic_history=changed_plan
        clock[0]+=timedelta(seconds=30)
        repaired=await service.refresh()
        if repair_damage=='many_gaps':
            assert not repaired['ready'] and not repaired['allowed']
            async with pool.acquire() as c:
                first=json.loads(await c.fetchval('SELECT receipt FROM trading.hrs_equity_reconciliation WHERE account_id=$1 ORDER BY after_state_version DESC LIMIT 1',account))
                assert first['history_complete'] is False and len(first['gap_endpoints'])==8
            clock[0]+=timedelta(seconds=30)
            repaired=await service.refresh()
        if repair_damage and repair_damage not in ('many_gaps','late_income','late_income_latch','funding_gap','funding_exit_gap'):
            assert not repaired['ready'] and not repaired['allowed'],repaired
            async with pool.acquire() as c:
                assert not await c.fetchval("SELECT EXISTS(SELECT 1 FROM trading.hrs_equity_reconciliation WHERE account_id=$1 AND receipt->>'kind' IN ('automatic_constant_mark_gap_v1','automatic_event_mark_gap_v2'))",account)
            return
        assert repaired['ready'] and repaired['allowed'],repaired
        assert repaired['window_label']=='inception_segment' and not repaired['qualified_30_day']
        async with pool.acquire() as c:
            revision=await c.fetchrow('SELECT * FROM trading.hrs_equity_reconciliation WHERE account_id=$1 ORDER BY after_state_version DESC LIMIT 1',account)
            receipt=json.loads(revision['receipt'])
            assert receipt['kind']=='automatic_event_mark_gap_v2'
            from strategies.hrs.equity_evidence import canonical
            after_state=json.loads(revision['after_state'])
            assert receipt['after_gate_sha256']==canonical(after_state['gate'])[1]
            assert receipt['after_ledger_sha256']==canonical(after_state['ledger'])[1]
            assert all(datetime.fromisoformat(bound['as_of'])<=datetime.fromisoformat(receipt['as_of']) for bound in receipt['conservative_bounds'])
            for source in receipt['http_receipts']:
                rows=receipt['mark_gaps'][0]['bars'][source['params']['symbol']]
                assert source['response_sha256']==canonical(rows)[1]
                assert rows[-1][6]<int(datetime.fromisoformat(source['requested_at']).timestamp()*1000)
            assert receipt['available_at']==clock[0].isoformat()
            assert len(receipt['http_receipts'])==1 and receipt['backfilled_points']>=2
            assert all([await c.fetchval('SELECT payload FROM trading.hrs_risk_equity_checkpoints WHERE account_id=$1 AND checkpoint_id=$2',account,row['checkpoint_id'])==row['payload'] for row in original_points])
        if repair_damage in ('late_income','late_income_latch'):
            old_receipt=revision['receipt'];old_units=receipt['conservative_bounds'][0]['source_units']
            original_records=list(api.get_user_trades.return_value)
            records=[dict(symbol='BTCUSDT',id=i,orderId=i,side=side,qty='1',price='100',quoteQty='100',commission='1',commissionAsset='USDT',realizedPnl='0',time=int((entered+timedelta(hours=1,seconds=i)).timestamp()*1000)) for i,side in ((82,'BUY'),(83,'SELL'))]
            async with pool.acquire() as c:
                for row in records:
                    await c.execute("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,executed_at) VALUES ($1,'BTCUSDT',$2,'hrs',$3,'FILLED','LIMIT',$4)",account,str(row['orderId']),row['side'],entered.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
            api.get_user_trades.return_value=original_records+records
            api.get_income_history.side_effect=lambda **kw:([dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-1',asset='USDT',tranId=row['id'],tradeId=str(row['id']),time=row['time'],info='') for row in original_records+records] if kw['income_type']=='COMMISSION' else [])
            api.get_order.side_effect=lambda symbol,identity:dict(symbol=symbol,orderId=identity,side='SELL' if identity==83 else 'BUY',status='FILLED')
            clock[0]+=timedelta(seconds=30)
            reconciled=await service.refresh()
            assert reconciled['ready'] and reconciled['allowed'] is (repair_damage!='late_income_latch'),reconciled
            if repair_damage=='late_income_latch':
                assert 'inception_segment_drawdown_latched' in reconciled['reasons']
            assert reconciled.get('reconciliation_receipt_sha256')
            async with pool.acquire() as c:
                assert await c.fetchval('SELECT receipt FROM trading.hrs_equity_reconciliation WHERE account_id=$1 AND receipt_sha256=$2',account,revision['receipt_sha256'])==old_receipt
                latest=await c.fetchrow('SELECT receipt,after_state FROM trading.hrs_equity_reconciliation WHERE account_id=$1 ORDER BY after_state_version DESC LIMIT 1',account)
                from strategies.hrs.equity_evidence import canonical
                final_receipt=json.loads(latest['receipt']);after=json.loads(latest['after_state'])
                assert final_receipt['after_gate_sha256']==canonical(after['gate'])[1]
                if repair_damage=='late_income_latch':
                    assert after['gate']['rolling_latched'] is True
                parent=json.loads(await c.fetchval('SELECT payload FROM trading.hrs_equity_evidence WHERE account_id=$1 AND evidence_sha256=$2',account,reconciled['evidence_sha256']))
                active=parent['history_gap_bounds']
                if repair_damage=='late_income_latch':
                    assert final_receipt['gap_bounds_valuation_sha256']==active['valuation_sha256']
                    assert final_receipt['gap_bounds_receipts']==sorted(item['receipt_sha256'] for item in active['receipts'])
                    assert datetime.fromisoformat(final_receipt['gap_bounds_valuation_available_at'])<=datetime.fromisoformat(final_receipt['available_at'])
                assert all(bound['source_units']!=old_units for bound in active['bounds'])
                assert active['valuation']['ledger_state']['units']==active['bounds'][0]['source_units']
                assert datetime.fromisoformat(active['bounds'][0]['valuation_available_at'])>datetime.fromisoformat(active['bounds'][0]['source_available_at'])
            return
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


@pytest.mark.asyncio
async def test_exact_legacy_condition_row_and_signed_child_order_prove_owner_without_link_table(setup):
    service,account,pool,api,clock=setup
    algo=9123456700000+int(uuid.uuid4().hex[:6],16)
    async with pool.acquire() as c:
        await c.execute('CREATE SCHEMA IF NOT EXISTS btc_eth')
        await c.execute('''CREATE TABLE IF NOT EXISTS btc_eth.condition_orders
            (id bigserial PRIMARY KEY,strategy_name text,account_id text,symbol text,algo_id bigint,order_id bigint,
             order_type text,status text,created_at timestamp,updated_at timestamp)''')
        source=await c.fetchval("INSERT INTO btc_eth.condition_orders(strategy_name,account_id,symbol,algo_id,order_type,status) VALUES('btc_eth_aggressive',$1,'BNBUSDT',$2,'STOP_LOSS','CANCELED') RETURNING id",account,algo)
        await c.execute("INSERT INTO trading.execution_fact_fill(account_id,symbol,trade_id,order_id,side,qty,price,trade_time_utc) VALUES($1,'BNBUSDT',101,202,'SELL',.06,690,$2)",account,T)
    try:
        api.get_algo_order.return_value=dict(algoId=algo,actualOrderId='202',symbol='BNBUSDT',side='SELL',algoStatus='FINISHED',quantity='.06')
        snapshot={}
        await service._authenticate_order_owners(snapshot,int((T-timedelta(seconds=1)).timestamp()*1000),int(T.timestamp()*1000))
        proofs=snapshot['authenticated_order_owners']
        assert len(proofs)==1 and proofs[0]['strategy']=='btc_eth_aggressive'
        assert proofs[0]['source_rows'][0]['table']=='btc_eth.condition_orders'
        assert proofs[0]['source_rows'][0]['row']['id']==source
        assert proofs[0]['response']['actualOrderId']=='202'
        assert proofs[0]['available_at']==T
        from strategies.hrs.equity_evidence import canonical,EvidenceBlocked
        canonical(snapshot)
        async with pool.acquire() as c:
            conflict=await c.fetchval("INSERT INTO btc_eth.condition_orders(strategy_name,account_id,symbol,algo_id) VALUES('hrs',$1,'BNBUSDT',$2) RETURNING id",account,algo)
        try:
            async with service._locked() as c:
                with pytest.raises(EvidenceBlocked,match='authenticated_condition_source_changed'):
                    await service._read_source(c,int((T-timedelta(seconds=1)).timestamp()*1000),int(T.timestamp()*1000),snapshot)
        finally:
            async with pool.acquire() as c:
                await c.execute('DELETE FROM btc_eth.condition_orders WHERE id=$1',conflict)
        async with pool.acquire() as c:
            await c.execute("UPDATE btc_eth.condition_orders SET strategy_name='btc_eth' WHERE id=$1",source)
        async with service._locked() as c:
            with pytest.raises(EvidenceBlocked,match='authenticated_condition_source_changed'):
                await service._read_source(c,int((T-timedelta(seconds=1)).timestamp()*1000),int(T.timestamp()*1000),snapshot)
        async with pool.acquire() as c:
            await c.execute("UPDATE btc_eth.condition_orders SET strategy_name='btc_eth_aggressive' WHERE id=$1",source)
        api.get_algo_order.return_value=dict(algoId=algo,actualOrderId='999',symbol='BNBUSDT',side='SELL',algoStatus='FINISHED')
        await service._authenticate_order_owners(snapshot,int((T-timedelta(seconds=1)).timestamp()*1000),int(T.timestamp()*1000))
        assert snapshot['authenticated_order_owners']==[]
        async with pool.acquire() as c:
            assert not await c.fetchval("SELECT 1 FROM trading.trade_records WHERE account_id=$1 AND order_id='202'",account)
    finally:
        async with pool.acquire() as c:
            await c.execute('DELETE FROM btc_eth.condition_orders WHERE id=$1',source)


@pytest.mark.asyncio
async def test_preperiod_new_coin_entry_proves_reduce_only_close_without_crediting_prior_income(setup):
    service,account,pool,api,clock=setup
    entered=T.replace(day=1,hour=14)-timedelta(days=2)
    closed=T.replace(day=4,hour=14)
    funding=T.replace(day=2,hour=0)
    entry_id=9000000+int(uuid.uuid4().hex[:6],16);close_id=entry_id+1
    async with pool.acquire() as c:
        await c.execute('CREATE SCHEMA IF NOT EXISTS new_coin')
        await c.execute('''CREATE TABLE IF NOT EXISTS new_coin.orders(id bigserial PRIMARY KEY,order_id text,symbol text,
            strategy text,side text,type text,quantity numeric,status text,created_at timestamp)''')
        source=await c.fetchval("INSERT INTO new_coin.orders(order_id,symbol,strategy,side,type,quantity,status,created_at) VALUES($1,'MELIUSDT','new_coin','SELL','LIMIT',.06,'FILLED',$2) RETURNING id",str(entry_id),entered.replace(tzinfo=None))
    try:
        records=[dict(symbol='MELIUSDT',id=1,orderId=entry_id,side='SELL',qty='.06',price='1600',quoteQty='96',commission='0',commissionAsset='USDT',realizedPnl='0',time=int(entered.timestamp()*1000)),
            dict(symbol='MELIUSDT',id=2,orderId=close_id,side='BUY',qty='.06',price='1500',quoteQty='90',commission='0',commissionAsset='USDT',realizedPnl='0',time=int(closed.timestamp()*1000))]
        api.get_user_trades.side_effect=lambda symbol,**kw:[r for r in records if r['symbol']==symbol and (kw.get('start_time') is None or r['time']>=kw['start_time']) and (kw.get('end_time') is None or r['time']<=kw['end_time'])]
        api.get_income_history.side_effect=lambda **kw:([dict(symbol='MELIUSDT',incomeType='FUNDING_FEE',income='-.01',asset='USDT',tranId=44,tradeId='',time=int(funding.timestamp()*1000),info='')] if kw['income_type']=='FUNDING_FEE' and kw['start_time'] <= int(funding.timestamp()*1000) <= kw['end_time'] else [])
        api.get_order.side_effect=lambda symbol,identity:dict(orderId=identity,symbol=symbol,side='SELL' if identity==entry_id else 'BUY',status='FILLED',reduceOnly=identity!=entry_id,executedQty='.06',origQty='.06',time=int((entered if identity==entry_id else closed).timestamp()*1000))
        result=await service.refresh()
        assert result['reasons']==['daily_origin_missing']
        async with pool.acquire() as c:
            row=await c.fetchrow('SELECT opening_cash,opening_evidence FROM trading.hrs_equity_segment WHERE account_id=$1',account)
            assert row['opening_cash']==D('1000')
            evidence=json.loads(row['opening_evidence'])
            assert evidence['credited_opening_income']==[]
            assert len(evidence['income_report']['events'])==1
            assert evidence['income_report']['events'][0]['strategy']=='new_coin'
            assert len(evidence['fills'])==2
            assert evidence['capture']['extended_fill_coverage'][0]['start_ms']==int(entered.timestamp()*1000)-1
            inherited=[r for r in evidence['order_owners'] if r.get('source_kind')=='certified_unique_inventory_reduce_only_close']
            assert len(inherited)==1 and inherited[0]['order_id']==str(close_id)
            assert not await c.fetchval('SELECT 1 FROM trading.trade_records WHERE account_id=$1',account)
    finally:
        async with pool.acquire() as c:
            await c.execute('DELETE FROM new_coin.orders WHERE id=$1',source)


@pytest.mark.asyncio
async def test_formal_gate_does_not_promote_manual_initialization_without_source_proof(setup):
    service,account,pool,api,clock=setup
    ms=int(T.timestamp()*1000)
    api.get_income_history.side_effect=lambda **kw:([dict(symbol='BTCUSDT',incomeType='FUNDING_FEE',income='-1',asset='USDT',tranId=300,tradeId='',time=ms,info='')] if kw['income_type']=='FUNDING_FEE' else [])
    async with pool.acquire() as c:
        await c.execute("INSERT INTO trading.reviewed_income_attribution(account_id,income_type,tran_id,strategy,symbol,income,asset,event_time_utc,evidence,reviewed_by) VALUES($1,'FUNDING_FEE',300,'hrs','BTCUSDT',-1,'USDT',$2,$3::jsonb,'user_manual_initialization')",account,T,json.dumps(dict(source='manual_workbook_initialization',evidence_status='not_provided',evidence_ref='')))
    try:
        snapshot=await service._capture()
        await service._collect(ms-1,ms,snapshot)
        async with service._locked() as c:
            report,*_=await service._read_source(c,ms-1,ms,snapshot)
        assert report['events'][0]['strategy'] is None
        assert report['events'][0]['reason']=='funding_owner_unproven'
    finally:
        async with pool.acquire() as c:
            await c.execute('DELETE FROM trading.reviewed_income_attribution WHERE account_id=$1',account)


@pytest.mark.asyncio
async def test_funding_history_covers_authenticated_closed_hrs_symbols(setup):
    service,account,pool,api,clock=setup
    snapshot=dict(local={},marks={},captured_at=T,authenticated_order_owners=[dict(symbol='IDOLUSDT',strategy='hrs')])
    start=int((T-timedelta(days=1)).timestamp()*1000);end=int(T.timestamp()*1000)
    await service._funding_publication_evidence(snapshot,start,end)
    assert 'IDOLUSDT' in snapshot['funding_publication']['published_coverage']
    assert api.get_funding_rate_history.await_args.kwargs['start_time']==start


@pytest.mark.asyncio
@pytest.mark.parametrize('entry_owner',['hrs','new_coin'])
async def test_preperiod_economic_entry_owner_must_agree_with_protection_manager(setup,entry_owner):
    service,account,pool,api,clock=setup
    entered=T.replace(day=1,hour=12)-timedelta(days=1)
    closed=T.replace(day=2,hour=12);funding=T.replace(day=1,hour=8)
    entry_id=50000000+int(uuid.uuid4().hex[:6],16);close_id=entry_id+1;algo=entry_id+80000000
    async with pool.acquire() as c:
        await c.execute('CREATE SCHEMA IF NOT EXISTS btc_eth')
        await c.execute('''CREATE TABLE IF NOT EXISTS btc_eth.condition_orders(id bigserial PRIMARY KEY,strategy_name text,account_id text,symbol text,algo_id bigint,order_id bigint,order_type text,status text,created_at timestamp,updated_at timestamp)''')
        entry=await c.fetchval("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,quantity,executed_at) VALUES(NULL,'SRCUSDT',$1,$2,'SELL','NEW','LIMIT',0,$3) RETURNING id",str(entry_id),entry_owner,entered.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
        manager=await c.fetchval("INSERT INTO btc_eth.condition_orders(strategy_name,account_id,symbol,algo_id,order_type,status) VALUES('hrs',$1,'SRCUSDT',$2,'TAKE_PROFIT','CANCELED') RETURNING id",account,algo)
    try:
        records=[dict(symbol='SRCUSDT',id=1,orderId=entry_id,side='SELL',qty='2',price='100',quoteQty='200',commission='0',commissionAsset='USDT',realizedPnl='0',time=int(entered.timestamp()*1000)),dict(symbol='SRCUSDT',id=2,orderId=close_id,side='BUY',qty='2',price='100',quoteQty='200',commission='0',commissionAsset='USDT',realizedPnl='0',time=int(closed.timestamp()*1000))]
        api.get_user_trades.side_effect=lambda symbol,**kw:[r for r in records if r['symbol']==symbol and (kw.get('start_time') is None or r['time']>=kw['start_time']) and (kw.get('end_time') is None or r['time']<=kw['end_time'])]
        api.get_income_history.side_effect=lambda **kw:([dict(symbol='SRCUSDT',incomeType='FUNDING_FEE',income='.2',asset='USDT',tranId=301,tradeId='',time=int(funding.timestamp()*1000),info='')] if kw['income_type']=='FUNDING_FEE' and kw['start_time'] <= int(funding.timestamp()*1000) <= kw['end_time'] else [])
        api.get_order.return_value=dict(orderId=entry_id,symbol='SRCUSDT',side='SELL',status='FILLED',reduceOnly=False,executedQty='2',origQty='2',time=int(entered.timestamp()*1000))
        api.get_algo_order.return_value=dict(algoId=algo,actualOrderId=str(close_id),symbol='SRCUSDT',side='BUY',algoStatus='FINISHED',quantity='2')
        api.get_funding_rate_history.return_value=[dict(symbol='SRCUSDT',fundingTime=int(funding.timestamp()*1000),fundingRate='.001',markPrice='100')]
        result=await service.refresh()
        if entry_owner=='new_coin':
            assert result['reasons']==['inventory_owner_path_conflict']
            async with pool.acquire() as c:
                assert not await c.fetchval('SELECT 1 FROM trading.hrs_equity_segment WHERE account_id=$1',account)
        else:
            assert result['reasons']==['daily_origin_missing']
            async with pool.acquire() as c:
                row=await c.fetchrow('SELECT opening_cash,opening_evidence FROM trading.hrs_equity_segment WHERE account_id=$1',account)
                assert row['opening_cash']==D('1000.2')
                evidence=json.loads(row['opening_evidence'])
                assert len(evidence['fills'])==2 and evidence['inventory_owner_path_proofs'][0]['verified_zero_to_current_path'] is True
                assert len(evidence['income_report']['events'])==1
    finally:
        async with pool.acquire() as c:
            await c.execute('DELETE FROM btc_eth.condition_orders WHERE id=$1',manager)
            await c.execute('DELETE FROM trading.trade_records WHERE id=$1',entry)


@pytest.mark.asyncio
async def test_referenced_forecasts_preserve_funding_deadline_expiry_and_raw_parent_hash(setup):
    from strategies.hrs.equity_evidence import canonical
    from strategies.hrs.funding_reference import read_forecasts_prefix,resolve_evidence_forecasts,COUNT_SQL,ROWS_SQL
    service,account,pool,api,clock=setup
    due=T+timedelta(seconds=20)
    forecast=dict(symbol='BTCUSDT',source_time=T,received_at=T,next_funding_time=due,source='authenticated_premium_index')
    encoded,digest=canonical(dict(forecasts=[forecast]))
    async with pool.acquire() as c:
        await c.execute('INSERT INTO trading.hrs_funding_observation VALUES($1,$2,$3,$4::jsonb)',account,digest,T,encoded)
        ref,rows=await read_forecasts_prefix(c,account,T)
        evidence=dict(account_id=account,as_of=T,available_at=T,capture=dict(local={'BTCUSDT':{}},
            funding_publication=dict(forecasts=[forecast],forecasts_ref=ref)))
        saved=await service._projection(c,1,dict(ready=True,allowed=True,reasons=[]),evidence)
        assert saved['valid_until']==due
        parent=dict(await c.fetchrow('SELECT account_id,evidence_sha256,payload FROM trading.hrs_equity_evidence WHERE account_id=$1',account))
        original=json.loads(parent['payload'])
        assert 'forecasts' not in original['capture']['funding_publication']
        assert canonical(original)[1]==parent['evidence_sha256']
        assert len(resolve_evidence_forecasts(parent,await c.fetch(ROWS_SQL,account,T),account_id=account,
            expected_count=await c.fetchval(COUNT_SQL,account,T)))==1
        # A lost/changed runtime source cannot turn a held deadline into +120s.
        evidence['capture']['funding_publication']['forecasts']=[]
        with pytest.raises(Exception,match='runtime_mismatch'):
            await service._projection(c,1,dict(ready=True,allowed=True,reasons=[]),evidence)


@pytest.mark.asyncio
async def test_late_reconciliation_reads_funding_source_prefix_once_not_per_parent(setup,monkeypatch):
    from contextlib import asynccontextmanager
    from strategies.hrs.funding_reference import COUNT_SQL,ROWS_SQL
    service,*_=setup
    import strategies.hrs.equity_service as module
    rebuild=module.rebuild_history
    def retained_only(previous,source,**kwargs):
        assert source.captures
        for captured in source.captures:
            assert set(captured['capture'])<=set(('inventory','independent','local','marks','historical_mark_bars'))
            assert captured['evidence_sha256']
        return rebuild(previous,source,**kwargs)
    monkeypatch.setattr(module,'rebuild_history',retained_only)
    locked=service._locked
    calls=[]
    @asynccontextmanager
    async def traced():
        async with locked() as connection:
            queries=[]
            def record(query):
                if query.query in (COUNT_SQL,ROWS_SQL):queries.append(query.query)
            connection.add_query_logger(record)
            try:yield connection
            finally:
                await asyncio.sleep(0)
                connection.remove_query_logger(record)
                calls.append(queries)
    service._locked=traced
    await test_late_proven_roundtrip_is_reconciled_append_only_and_next_refresh_is_normal(setup,False,None)
    # Two saved parents are both checked, but read only one maximum prefix.
    # One additional read validates the new single projection, as before.
    assert calls[2].count(COUNT_SQL)==2
    assert calls[2].count(ROWS_SQL)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['parent_hash','missing_capture','missing_ledger_version'])
async def test_all_reconciliation_raw_parents_are_checked_before_source_prefix_read(setup,damage):
    from contextlib import asynccontextmanager
    from strategies.hrs.funding_reference import COUNT_SQL,ROWS_SQL
    service,*_=setup
    locked=service._locked
    calls=[]
    @asynccontextmanager
    async def traced():
        async with locked() as connection:
            queries=[]
            def record(query):
                if query.query in (COUNT_SQL,ROWS_SQL):queries.append(query.query)
            connection.add_query_logger(record)
            try:yield connection
            finally:
                await asyncio.sleep(0)
                connection.remove_query_logger(record)
                calls.append(queries)
    service._locked=traced
    # The oldest parent has damage in an otherwise-unused funding field. A
    # newer valid parent is encountered first; neither may trigger prefix IO.
    await test_late_proven_roundtrip_is_reconciled_append_only_and_next_refresh_is_normal(setup,False,damage)
    assert calls[2]==[]


@pytest.mark.asyncio
async def test_refresh_loop_uses_start_cadence_when_collection_takes_95_seconds(monkeypatch):
    import strategies.hrs.equity_service as module
    service=HrsEquityService(None,None,None,None)
    elapsed=[0.0];starts=[]
    monkeypatch.setattr(module,'monotonic',lambda:elapsed[0],raising=False)
    async def refresh():
        starts.append(elapsed[0]);elapsed[0]+=95
        if len(starts)==2:await service.close()
    async def wait_for(waiter,timeout):
        waiter.close();elapsed[0]+=timeout
        raise asyncio.TimeoutError
    service.refresh=refresh
    monkeypatch.setattr(module.asyncio,'wait_for',wait_for)
    await service.run()
    assert starts==[0,95]  # A fixed post-refresh30s wait would make125s.


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_scan',[False,True])
async def test_start_cadence_yields_to_queued_scan_and_preserves_lock_cancellation(monkeypatch,cancel_scan):
    import strategies.hrs.equity_service as module
    service=HrsEquityService(None,None,None,None)
    elapsed=[0.0];order=[];first=asyncio.Event();release=asyncio.Event()
    monkeypatch.setattr(module,'monotonic',lambda:elapsed[0])
    async def refresh():
        async with service._refresh_lock:
            who=asyncio.current_task().get_name();order.append(who)
            if len(order)==1:
                first.set();await release.wait();elapsed[0]+=95
            elif who=='scan':
                elapsed[0]+=24;await asyncio.sleep(0)
            else:await service.close()
    service.refresh=refresh
    background=asyncio.create_task(service.run(),name='background')
    await first.wait()
    scan=asyncio.create_task(service.refresh(),name='scan')
    await asyncio.sleep(0)
    assert not scan.done()
    if cancel_scan:
        scan.cancel()
        with pytest.raises(asyncio.CancelledError):await scan
    release.set()
    if not cancel_scan:await asyncio.wait_for(scan,timeout=120)
    await asyncio.wait_for(background,timeout=1)
    assert order==(['background','background'] if cancel_scan else ['background','scan','background'])
    assert not service._refresh_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['parent_hash','missing_marks','parent_version','receipt_hash'])
async def test_automatic_repair_never_authorizes_damaged_original_sources(setup,damage):
    await test_existing_hrs_position_has_economic_opening_cash_and_terminal_fill_proof(setup,False,False,repair_damage=damage)


@pytest.mark.asyncio
async def test_automatic_repair_batches_leave_gate_denied_until_all_owned_gaps_are_proven(setup):
    await test_existing_hrs_position_has_economic_opening_cash_and_terminal_fill_proof(setup,False,False,repair_damage='many_gaps')


@pytest.mark.asyncio
async def test_late_opening_income_reconciliation_revalues_original_bounds_with_new_units(setup):
    await test_existing_hrs_position_has_economic_opening_cash_and_terminal_fill_proof(setup,False,False,repair_damage='late_income')


@pytest.mark.asyncio
async def test_late_income_bound_latch_receipt_matches_persisted_final_gate(setup):
    await test_existing_hrs_position_has_economic_opening_cash_and_terminal_fill_proof(setup,False,False,repair_damage='late_income_latch')


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['funding_gap','funding_exit_gap'])
async def test_event_v2_recovers_exact_independent_income_and_full_exit_end_to_end(setup,case):
    await test_existing_hrs_position_has_economic_opening_cash_and_terminal_fill_proof(setup,False,False,repair_damage=case)


async def _approve_history_for_existing_segment(setup, monkeypatch, *, latched=False, automatic=False):
    """Real PG policy/repository; exchange GET replies remain fixture inputs."""
    from shared.accounting.history_tolerance import acceptance_digest, CLASSIC_RISK_METHOD
    from strategies.hrs.equity_evidence import canonical
    service, account, pool, api, clock = setup
    if automatic:
        async with pool.acquire() as c:
            await c.execute("UPDATE public.capital_allocation SET entries=$1::jsonb WHERE status='active'",
                json.dumps([dict(strategy_id=s,allocated_amount=1000.0 if s=='hrs' else 0.0)
                            for s in ('btc_eth','btc_eth_aggressive','hrs','new_coin')]))
    await service.refresh()
    async with pool.acquire() as c:
        if latched:
            await c.execute("UPDATE trading.hrs_loss_gate_state SET gate_state=jsonb_set(jsonb_set(gate_state,'{rolling_latched}','true'),'{rolling_latch_scope}','\"inception_segment\"') WHERE account_id=$1", account)
        old=dict(await c.fetchrow('SELECT * FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
        if not await c.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user')"):
            await c.execute('CREATE ROLE trading_user NOLOGIN')
        await c.execute(Path('database/postgres/migrations/2026-11-09-derived-income-attribution.sql').read_text())
        if not await c.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_migration_admin')"):
            await c.execute('CREATE ROLE trading_migration_admin NOLOGIN')
        await c.execute('GRANT USAGE,CREATE ON SCHEMA trading TO trading_migration_admin')
        await c.execute('SET ROLE trading_migration_admin')
        try:
            await c.execute(Path('database/postgres/migrations/2026-11-27-historical-risk-acceptance.sql').read_text())
        finally:
            await c.execute('RESET ROLE')
    cut=int((T+timedelta(milliseconds=500)).timestamp()*1000)
    flat=T+timedelta(seconds=1); through=T+timedelta(hours=1)
    clock[0]=through+timedelta(seconds=30)
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS',str(cut))
    api.use_unified_account=False;api._api_key='isolated-test-key'
    monkeypatch.setattr('strategies.hrs.equity_service.signed_spot_uid_sha256',AsyncMock(return_value='b'*64))
    ledger=json.loads(old['ledger_state']);gate=json.loads(old['gate_state'])
    payload=dict(kind='historical_risk_acceptance_v1',account_id=account,revision=1,enabled=True,
        approval_ref='isolated-test-acceptance',exchange_uid_sha256='b'*64,
        accepted_from=T.replace(day=1,hour=0).isoformat(),accepted_through=through.isoformat(),
        effective_at=clock[0].isoformat(),classic_cutover_ms=cut,classic_flat_at=flat.isoformat(),
        classic_flat_evidence_sha256='c'*64,classic_open_equity='1000',risk_limit_ratio='.05',
        hrs_daily_limit='.03',hrs_rolling_limit='.06',automatic_allocation=automatic,
        classic_risk_method=CLASSIC_RISK_METHOD,
        allocation_ceilings_usdt=dict(btc_eth='0',btc_eth_aggressive='0',hrs='1000',new_coin='0'),
        allowed_gap_codes=['pm_native_income_coverage','pm_native_fill_coverage','historical_nav_sampling',
                           'historical_cashflow_nav','pm_final_equity_missing','pm_classic_valuation_discontinuity'],
        hrs_segment_id=old['segment_id'],hrs_baseline_state_version=old['version'],
        hrs_baseline_ledger_sha256=canonical(ledger)[1],hrs_baseline_gate_sha256=canonical(gate)[1])
    async with pool.acquire() as c:
        if automatic:
            from shared.accounting.allocation_provenance import entries_digest
            row=await c.fetchrow("SELECT id,entries FROM public.capital_allocation WHERE status='active' ORDER BY id DESC LIMIT 1")
            payload.update(allocation_anchor_id=row['id'],allocation_anchor_entries_sha256=entries_digest(row['entries']))
        await c.execute('INSERT INTO trading.historical_risk_acceptance(account_id,revision,payload,receipt_sha256) VALUES($1,1,$2::jsonb,$3)',account,json.dumps(payload),acceptance_digest(payload))
    await service._collect(cut,int(flat.timestamp()*1000),{'inventory':[]})
    return old,payload


@pytest.mark.asyncio
async def test_accepted_history_restarts_original_ledger_without_completing_history(setup, monkeypatch):
    service, account, pool, api, clock=setup
    old,payload=await _approve_history_for_existing_segment(setup,monkeypatch)
    result=await service.refresh()
    assert result['ready'] and result['allowed'],result
    assert result['history_status']=='ACCEPTED_GAPS' and not result['qualified_30_day']
    assert result['window_label']=='observed_history_accepted_gaps'
    async with pool.acquire() as c:
        current=await c.fetchrow('SELECT * FROM trading.hrs_loss_gate_state WHERE account_id=$1',account)
        ledger=json.loads(current['ledger_state']);before=json.loads(old['ledger_state'])
        for field in ('segment_id','inception','cash_allocation','units','income_total','incomes','cash_flows'):
            assert ledger[field]==before[field]
        assert await c.fetchval('SELECT count(*) FROM trading.hrs_equity_segment WHERE account_id=$1',account)==1
        evidence=json.loads(await c.fetchval('SELECT payload FROM trading.hrs_equity_evidence WHERE account_id=$1 AND evidence_sha256=$2',account,result['evidence_sha256']))
        assert evidence['historical_income_report']['account_coverage_complete'] is False
        assert evidence['current_income_report']['account_coverage_complete'] is True
    restarted=HrsEquityService(service.db,api,service.capital_mgr,service.position_manager)
    restarted.clock=service.clock;clock[0]+=timedelta(seconds=30)
    again=await restarted.refresh()
    assert again['allowed'] and again['history_acceptance_sha256']==result['history_acceptance_sha256'],again
    await restarted.close()


@pytest.mark.asyncio
async def test_accepted_history_preserves_persisted_known_loss_latch(setup,monkeypatch):
    service, account, pool, api, clock=setup
    await _approve_history_for_existing_segment(setup,monkeypatch,latched=True)
    result=await service.refresh()
    assert result['ready'] and not result['allowed'],result
    assert 'inception_segment_drawdown_latched' in result['reasons']
    async with pool.acquire() as c:
        gate=json.loads(await c.fetchval('SELECT gate_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
        assert gate['rolling_latched'] is True


@pytest.mark.asyncio
async def test_current_collection_failure_still_denies_after_accepted_history(setup,monkeypatch):
    service, account, pool, api, clock=setup
    await _approve_history_for_existing_segment(setup,monkeypatch)
    assert (await service.refresh())['allowed']
    clock[0]+=timedelta(seconds=30);api.get_income_history.side_effect=RuntimeError('current exchange unavailable')
    result=await service.refresh()
    assert not result['allowed'] and not result['ready']
    async with pool.acquire() as c:
        assert not await c.fetchval("SELECT allowed FROM trading.strategy_equity_gate WHERE account_id=$1",account)


@pytest.mark.asyncio
async def test_current_independent_inventory_conflict_is_not_historical_gap(setup,monkeypatch):
    service, account, pool, api, clock=setup
    await _approve_history_for_existing_segment(setup,monkeypatch)
    api.get_um_account_positions.return_value=[dict(symbol='BTCUSDT',positionAmt='1',entryPrice='100',positionSide='BOTH')]
    result=await service.refresh()
    assert not result['allowed']
    assert any('inventory' in reason for reason in result['reasons']),result


@pytest.mark.asyncio
async def test_accepted_history_accounts_new_classic_fees_without_reseeding(setup,monkeypatch):
    service, account, pool, api, clock=setup
    await _approve_history_for_existing_segment(setup,monkeypatch)
    first=await service.refresh();assert first['allowed'],first
    start=clock[0];clock[0]+=timedelta(seconds=60)
    records=[dict(symbol='BTCUSDT',id=801,orderId=801,side='BUY',qty='1',price='100',quoteQty='100',commission='.5',commissionAsset='USDT',realizedPnl='0',time=int((start+timedelta(seconds=10)).timestamp()*1000)),
             dict(symbol='BTCUSDT',id=802,orderId=802,side='SELL',qty='1',price='100',quoteQty='100',commission='.5',commissionAsset='USDT',realizedPnl='0',time=int((start+timedelta(seconds=20)).timestamp()*1000))]
    async with pool.acquire() as c:
        for row in records:
            when=datetime.fromtimestamp(row['time']/1000,timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
            await c.execute("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,executed_at) VALUES($1,'BTCUSDT',$2,'hrs',$3,'FILLED','LIMIT',$4)",account,str(row['orderId']),row['side'],when)
        schedule_id=uuid.uuid4();service._test_schedule_ids.append(schedule_id)
        await c.execute("INSERT INTO public.ai_tuner_funding_schedule_observations(attempt_id,symbol,source_time,received_at,next_funding_time,funding_interval_hours,payload_hash,raw_payload) VALUES($1,'BTCUSDT',$2,$2,$3,8,'test','{}')",schedule_id,start,start+timedelta(hours=8))
    api.get_user_trades.return_value=records
    api.get_income_history.side_effect=lambda **kw: ([dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-.5',asset='USDT',tranId=r['id'],tradeId=str(r['id']),time=r['time'],info='') for r in records if kw['start_time']<=r['time']<=kw['end_time']] if kw['income_type']=='COMMISSION' else [])
    result=await service.refresh()
    assert result['ready'] and result['allowed'],result
    async with pool.acquire() as c:
        ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
        assert D(ledger['cash_allocation'])==1000 and D(ledger['units'])==1000
        assert D(ledger['income_total'])==-1 and len(ledger['incomes'])==2
        assert D(ledger['nav'])==D('.999')
    clock[0]+=timedelta(seconds=30)
    repeat=await service.refresh();assert repeat['allowed'],repeat
    async with pool.acquire() as c:
        ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
        assert D(ledger['income_total'])==-1 and len(ledger['incomes'])==2


@pytest_asyncio.fixture
async def rollover_setup(monkeypatch):
    # Synthetic future journal timestamps are intentionally immutable. Isolate
    # each scenario so its source clock cannot contaminate another segment.
    import asyncpg
    import sys
    from urllib.parse import urlsplit,urlunsplit
    name='hrs_rollover_'+uuid.uuid4().hex
    admin=await asyncpg.connect(DSN)
    await admin.execute('CREATE DATABASE '+name)
    parts=urlsplit(DSN)
    isolated=urlunsplit(parts._replace(path='/'+name))
    monkeypatch.setattr(sys.modules[__name__],'DSN',isolated)
    generator=setup.__wrapped__(monkeypatch)
    try:
        yield await anext(generator)
    finally:
        await generator.aclose()
        await admin.execute('DROP DATABASE '+name+' WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_accepted_month_missing_continues_ledger_but_denies_entry(rollover_setup,monkeypatch):
    setup=rollover_setup
    service,account,pool,api,clock=setup
    old,payload=await _approve_history_for_existing_segment(setup,monkeypatch,automatic=True)
    from shared.accounting import history_tolerance
    validate=history_tolerance.validate_history_acceptance
    def observed_validation(*args,**kwargs):
        kwargs.setdefault('now',clock[0])
        return validate(*args,**kwargs)
    monkeypatch.setattr(history_tolerance,'validate_history_acceptance',observed_validation)
    boundary=(T.replace(day=28)+timedelta(days=4)).replace(day=1,hour=0)-timedelta(hours=8)
    async with pool.acquire() as c:
        await c.execute('UPDATE trading.strategy_risk_policy SET expires_at=$2 WHERE account_id=$1',account,boundary+timedelta(days=2))
    clock[0]=boundary-timedelta(seconds=30)
    before=await service.refresh()
    assert before['allowed'],before
    assert before['valid_until']==boundary
    clock[0]=boundary
    result=await service.refresh()
    assert result['ready'] and not result['allowed'],result
    assert 'allocation_month_missing' in result['reasons']
    async with pool.acquire() as c:
        ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
        assert datetime.fromisoformat(ledger['as_of'])==boundary
        assert ledger['units']==json.loads(old['ledger_state'])['units']
        assert ledger['cash_flows']==[]


async def _publish_at_observed_test_time(pool, at, month, entries):
    """Real trigger and journal; inject only its wall clock inside one local transaction.

    The original trigger definition is restored before commit. No guard/trigger is
    disabled and no immutable journal row is edited to construct the scenario.
    """
    async with pool.acquire() as c, c.transaction():
        original=await c.fetchval("SELECT pg_get_functiondef('trading.hrs_capture_allocation_event()'::regprocedure)")
        observed=original.replace('clock_timestamp()', "TIMESTAMPTZ '"+at.isoformat()+"'")
        await c.execute(observed)
        identity=await c.fetchval("INSERT INTO public.capital_allocation(month,entries,status,created_at) VALUES($1,$2::jsonb,'active',$3) RETURNING id",
            month,json.dumps(entries),at.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
        await c.execute(original)
        return identity


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['same_amount','late_changed_amount','latched','malformed','new_daily_loss'])
async def test_accepted_nonflat_month_rollover_samples_then_publishes(rollover_setup,monkeypatch,case):
    setup=rollover_setup
    from shared.accounting import history_tolerance
    from shared.accounting.allocation_provenance import METHOD,entries_digest
    service,account,pool,api,clock=setup
    old,payload=await _approve_history_for_existing_segment(setup,monkeypatch,automatic=True,latched=case=='latched')
    validate=history_tolerance.validate_history_acceptance
    def observed_validation(*args,**kwargs):
        kwargs.setdefault('now',clock[0])
        return validate(*args,**kwargs)
    monkeypatch.setattr(history_tolerance,'validate_history_acceptance',observed_validation)
    boundary=(T.replace(day=28)+timedelta(days=4)).replace(day=1,hour=0)-timedelta(hours=8)
    async with pool.acquire() as c:
        await c.execute('UPDATE trading.strategy_risk_policy SET expires_at=$2 WHERE account_id=$1',account,boundary+timedelta(days=2))
        parent=await c.fetchrow('SELECT id,entries FROM public.capital_allocation WHERE id=$1',payload['allocation_anchor_id'])
    clock[0]=boundary-timedelta(seconds=60)
    first=await service.refresh();assert first['ready'],first
    opened=boundary-timedelta(seconds=45)
    trade=dict(symbol='BTCUSDT',id=811,orderId=811,side='BUY',qty='1',price='100',quoteQty='100',
        commission='0',commissionAsset='USDT',realizedPnl='0',time=int(opened.timestamp()*1000))
    async with pool.acquire() as c:
        await c.execute("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,executed_at) VALUES($1,'BTCUSDT','811','hrs','BUY','FILLED','LIMIT',$2)",
            account,opened.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
        schedule_id=uuid.uuid4();service._test_schedule_ids.append(schedule_id)
        await c.execute("INSERT INTO public.ai_tuner_funding_schedule_observations(attempt_id,symbol,source_time,received_at,next_funding_time,funding_interval_hours,payload_hash,raw_payload) VALUES($1,'BTCUSDT',$2,$2,$3,8,'test','{}')",schedule_id,opened,opened+timedelta(hours=8))
    api.get_user_trades.return_value=[trade]
    inventory=[dict(symbol='BTCUSDT',positionAmt='1',entryPrice='100',positionSide='BOTH')]
    api.get_position.return_value=inventory;api.get_um_account_positions.return_value=inventory
    service.position_manager.get_all_positions=lambda:{'BTCUSDT':dict(direction='long',remaining_quantity='1',position_id='month-held')}
    def expected_nav():
        if case=='new_daily_loss':return D('.96') if clock[0]>=boundary+timedelta(seconds=60) else D('1')
        return D('.999')
    def response(method,path,**kwargs):
        if path=='/fapi/v1/premiumIndex':
            mark=100+(expected_nav()-1)*1000
            return dict(symbol='BTCUSDT',markPrice=str(mark),time=int(clock[0].timestamp()*1000))
        return funding_source_response(path)
    api._request.side_effect=response
    for instant in (boundary-timedelta(seconds=30),boundary,boundary+timedelta(seconds=60)):
        clock[0]=instant
        result=await service.refresh()
        assert result['ready'],result
        if instant>=boundary:
            assert not result['allowed'] and 'allocation_month_missing' in result['reasons'],result
        if case=='latched':assert 'inception_segment_drawdown_latched' in result['reasons']
        async with pool.acquire() as c:
            ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
            assert datetime.fromisoformat(ledger['as_of'])==instant
            assert D(ledger['nav'])==expected_nav() and D(ledger['units'])==1000
            assert ledger['cash_flows']==[]
            if case=='new_daily_loss':
                gate=json.loads(await c.fetchval('SELECT gate_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
                assert gate['daily_latched']==(instant>=boundary+timedelta(seconds=60))
                if instant>=boundary+timedelta(seconds=60):
                    assert 'daily_loss_latched' in result['reasons']
                    assert result['daily_return']=='-0.04'
    publish_at=boundary+timedelta(seconds=30 if case=='late_changed_amount' else 90)
    receipt=history_tolerance.acceptance_digest(payload)
    entries=[dict(e,publication_method=METHOD,previous_allocation_id=parent['id'],previous_entries_sha256=entries_digest(parent['entries']),
                  history_acceptance_sha256=receipt,holdings_floor_usdt=str(e['allocated_amount'])) for e in json.loads(parent['entries'])]
    if case=='late_changed_amount':next(e for e in entries if e['strategy_id']=='hrs')['allocated_amount']='900'
    if case=='malformed':entries=[]
    inserted=await _publish_at_observed_test_time(pool,publish_at,boundary.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m'),entries)
    try:
        clock[0]=boundary+timedelta(seconds=120)
        result=await service.refresh()
        if case=='late_changed_amount':
            assert not result['allowed'] and result['reasons']==['late_allocation_requires_reconciliation'],result
        elif case=='malformed':
            assert not result['allowed'] and 'allocation_identity_ambiguous' in result['reasons'],result
        else:
            assert result['ready'] and result['allowed']==(case not in ('latched','new_daily_loss')),result
            assert 'allocation_month_missing' not in result['reasons']
            async with pool.acquire() as c:
                ledger=json.loads(await c.fetchval('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
                assert datetime.fromisoformat(ledger['as_of'])==clock[0]
                assert D(ledger['nav'])==expected_nav() and D(ledger['units'])==1000 and ledger['cash_flows']==[]
                if case=='new_daily_loss':
                    gate=json.loads(await c.fetchval('SELECT gate_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',account))
                    assert gate['daily_latched'] is True and 'daily_loss_latched' in result['reasons']
    finally:
        async with pool.acquire() as c:await c.execute('DELETE FROM public.capital_allocation WHERE id=$1',inserted)
