"""Isolated real PostgreSQL; app leases execute SET ROLE trading_user NOSUPERUSER."""
import asyncio,json,os,uuid
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import pytest
import pytest_asyncio
from shared.entry_risk_policy import RiskPolicyStore,SizedEntry,EntryDenied
from strategies.hrs.equity_evidence import canonical,certify_owned_inventory_paths

DSN=os.getenv('HRS_TEST_POSTGRES_DSN')
pytestmark=pytest.mark.skipif(not DSN,reason='isolated local administrator DSN required')


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    import asyncpg
    control=await asyncpg.connect(DSN)
    name='hrs_slots_'+uuid.uuid4().hex
    await control.execute(f"CREATE DATABASE {name} TEMPLATE template0 ENCODING 'UTF8'")
    admin=pool=None
    try:
        admin=await asyncpg.connect(DSN,database=name)
        await admin.execute('''CREATE SCHEMA trading;
            CREATE TABLE trading.position_claims(id bigserial primary key,symbol text,strategy text,claim_state text,expires_at timestamptz,released_at timestamptz,reason text);
            CREATE TABLE public.capital_allocation(month date,status text,entries jsonb,created_at timestamptz default now());
            CREATE TABLE trading.income_ingestion_attempt(account_id text,state text);
            GRANT USAGE ON SCHEMA trading TO trading_user;
            GRANT SELECT,UPDATE ON trading.position_claims TO trading_user;
            GRANT SELECT ON public.capital_allocation,trading.income_ingestion_attempt TO trading_user;''')
        await admin.execute(Path('database/postgres/migrations/2026-11-04-entry-risk-policy.sql').read_text())
        await admin.execute(Path('database/postgres/migrations/2026-11-17-hrs-equity-service.sql').read_text())
        await admin.execute("INSERT INTO public.capital_allocation(month,status,entries) VALUES(date_trunc('month',now() AT TIME ZONE 'Asia/Shanghai'),'active','[{\"strategy_id\":\"hrs\",\"allocated_amount\":1000}]')")
        await admin.execute("INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason) VALUES('test','hrs',1,now()-interval '1 hour',now()+interval '1 day','reduced',false,.5,.15,1000,'fixture')")
        await admin.execute("INSERT INTO trading.strategy_risk_active VALUES('test','hrs',1)")
        at=await admin.fetchval("SELECT clock_timestamp()-interval '1 second'")
        inventory=[dict(symbol=s,positionAmt=q,entryPrice='10',positionSide='BOTH',markPrice='10',leverage='10') for s,q in (('WLDUSDT','28'),('BNBUSDT','.04'),('SOLUSDT','-.27'))]
        local={'WLDUSDT':dict(position_id='p',direction='long',remaining_quantity='28')}
        fills=[];owners=[]
        for i,p in enumerate(inventory):
            owner='hrs' if i==0 else 'btc_eth_aggressive'
            fills.append(dict(account_id='test',symbol=p['symbol'],trade_id=i+1,order_id=i+1,side='BUY' if D(p['positionAmt'])>0 else 'SELL',qty=str(abs(D(p['positionAmt']))),price='10',trade_time_utc=at-timedelta(seconds=1)))
            owners.append(dict(account_id='test',symbol=p['symbol'],order_id=str(i+1),strategy=owner))
            await admin.execute("INSERT INTO trading.position_claims(symbol,strategy,claim_state,expires_at) VALUES($1,$2,'ACTIVE','infinity')",p['symbol'],owner)
        for symbol in ('SUIUSDT','A','B','C'):
            await admin.execute("INSERT INTO trading.position_claims(symbol,strategy,claim_state,expires_at) VALUES($1,'hrs','PENDING','infinity')",symbol)
        paths=certify_owned_inventory_paths('test',{r['symbol']:D(r['positionAmt']) for r in inventory},fills,owners,coverage_complete=True)
        payload=dict(account_id='test',segment_id='seg',as_of=at,available_at=at,capture=dict(inventory=inventory,independent=inventory,local=local),fills=fills,order_owners=owners,source_collection=dict(complete=True,gaps=[],position_symbols_covered=[r['symbol'] for r in inventory]),inventory_owner_path_proofs=paths,result=dict(ledger_version=1))
        encoded,digest=canonical(payload)
        await admin.execute('INSERT INTO trading.hrs_equity_evidence VALUES($1,$2,$3,$4,$4,$5::jsonb)','test',digest,'seg',at,encoded)
        await admin.execute("INSERT INTO trading.strategy_equity_gate(account_id,strategy_id,policy_revision,ready,allowed,as_of,valid_until,reasons,evidence_sha256) VALUES('test','hrs',1,true,true,$1::timestamptz,$1::timestamptz+interval '120 seconds','[]',$2)",at,digest)
        async def role(c):
            await c.execute('SET ROLE trading_user')
            assert not await c.fetchval('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
        pool=await asyncpg.create_pool(DSN,database=name,min_size=2,max_size=4,setup=role)
        monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
        store=RiskPolicyStore(SimpleNamespace(pool=pool),'test')
        sized=SizedEntry(D(1),D(1),D(100),D(10),D(10),D(1),D(1))
        async def snapshot(margin=100,max_slots=3):
            from shared.entry_risk_policy import ScopedRiskSnapshot
            from strategies.hrs.entry_occupancy import capture_hrs_slot_inputs,SLOT_SCOPE
            return ScopedRiskSnapshot(D(margin),D(10000),D(1000),max_slots,SLOT_SCOPE,
                capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local=local,observed_at=await admin.fetchval('SELECT clock_timestamp()')))
        yield SimpleNamespace(admin=admin,pool=pool,store=store,sized=sized,snapshot=snapshot,at=at,payload=payload,inventory=inventory)
    finally:
        if pool is not None:await pool.close()
        if admin is not None:await admin.close()
        await control.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')
        await control.close()


@pytest.mark.asyncio
async def test_foreign_positions_do_not_consume_hrs_slots_but_all_account_margin_remains(runtime):
    r=runtime
    assert r.store.reservation_audit is None
    client=await r.store.reserve('hrs','SUIUSDT','sui',r.sized,1,r.snapshot)
    async with r.pool.acquire() as c:
        geometry=json.loads(await c.fetchval('SELECT geometry FROM trading.entry_risk_reservation WHERE client_order_id=$1',client))
    assert geometry['slot_occupancy_proof']['hrs_symbols']==['WLDUSDT']
    assert geometry['slot_occupancy_proof']['account_symbols']==['BNBUSDT','SOLUSDT','WLDUSDT']
    from datetime import datetime,timezone
    context=geometry['slot_context']
    assert context['strategy_id']=='hrs' and context['policy_revision']==1
    assert {'BNBUSDT','SOLUSDT','WLDUSDT','SUIUSDT'}<=set(row['symbol'] for row in context['ownership_claims'])
    assert all(set(row)=={'id','symbol','strategy','claim_state'} for row in context['ownership_claims'])
    assert datetime.fromisoformat(context['as_of'])==r.at
    assert r.at<=datetime.fromisoformat(context['checked_at'])<=datetime.now(timezone.utc)
    assert r.store.reservation_audit==dict(client_order_id=client,slot_occupancy_proof=geometry['slot_occupancy_proof'],slot_context=context)
    with pytest.raises(EntryDenied) as caught:
        await r.store.reserve('hrs','B','expensive',r.sized,1,lambda:r.snapshot(margin=991))
    assert caught.value.reason_code=='entry_margin_ceiling'
    assert caught.value.context['account_margin']=='991'
    assert r.store.reservation_audit is None


@pytest.mark.asyncio
async def test_pending_union_is_durable_deduplicates_held_symbol_and_enforces_max_three(runtime):
    r=runtime
    await r.admin.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state,updated_at) VALUES('held','test','hrs',1,'WLDUSDT',28,28,10,10,28,'submitting',$1)",r.at-timedelta(seconds=1))
    await r.store.reserve('hrs','A','a',r.sized,1,r.snapshot)
    await r.store.reserve('hrs','SUIUSDT','sui',r.sized,1,r.snapshot)
    with pytest.raises(EntryDenied) as caught:await r.store.reserve('hrs','B','b',r.sized,1,r.snapshot)
    assert caught.value.reason_code=='entry_position_slots'
    context=caught.value.context
    assert context['hrs_symbols']==['WLDUSDT'] and context['reservation_symbols']==['A','SUIUSDT','WLDUSDT']
    assert context['requested_slots']==4 and context['max_slots']==3
    assert await r.admin.fetchval("SELECT state FROM trading.entry_risk_reservation WHERE client_order_id='held'")=='submitting'


@pytest.mark.asyncio
async def test_concurrent_scoped_reservations_observe_locked_current_pending_rows(runtime):
    r=runtime
    await r.store.reserve('hrs','A','a',r.sized,1,r.snapshot)
    # Both GET snapshots complete before either contender receives the lock.
    snapshots=[await r.snapshot(),await r.snapshot()]
    async def fixed(i):return snapshots[i]
    outcomes=await asyncio.gather(*(r.store.reserve('hrs',symbol,symbol,r.sized,1,lambda i=i:fixed(i)) for i,symbol in enumerate(('B','C'))),return_exceptions=True)
    assert sum(isinstance(v,str) for v in outcomes)==1
    denied=next(v for v in outcomes if isinstance(v,EntryDenied))
    assert denied.reason_code=='entry_position_slots' and denied.context['requested_slots']==4


@pytest.mark.asyncio
async def test_unknown_owner_is_denied_under_lock_without_reservation_or_claim_write(runtime):
    r=runtime
    r.payload['order_owners'].pop()
    encoded,digest=canonical(r.payload)
    await r.admin.execute('UPDATE trading.hrs_equity_evidence SET payload=$1::jsonb,evidence_sha256=$2',encoded,digest)
    await r.admin.execute('UPDATE trading.strategy_equity_gate SET evidence_sha256=$1',digest)
    with pytest.raises(EntryDenied) as caught:await r.store.reserve('hrs','SUIUSDT','unknown',r.sized,1,r.snapshot)
    assert caught.value.reason_code=='hrs_slot_ownership_unproven'
    assert caught.value.context['strategy_id']=='hrs'
    assert caught.value.context['policy_revision']==1
    assert caught.value.context['as_of']==r.at.isoformat()
    assert caught.value.context['checked_at']
    assert {'BNBUSDT','SOLUSDT','WLDUSDT'}<=set(row['symbol'] for row in caught.value.context['ownership_claims'])
    assert await r.admin.fetchval('SELECT count(*) FROM trading.entry_risk_reservation')==0


@pytest.mark.asyncio
async def test_legacy_tuple_preserves_previous_slot_semantics_and_new_scope_cannot_raise_limit(runtime):
    r=runtime
    async def legacy():return 100,10000,1000,{'WLDUSDT','BNBUSDT','SOLUSDT'},3
    with pytest.raises(EntryDenied,match='slots'):await r.store.reserve('hrs','SUIUSDT','legacy',r.sized,1,legacy)
    with pytest.raises(EntryDenied) as caught:await r.store.reserve('hrs','SUIUSDT','raise',r.sized,1,lambda:r.snapshot(max_slots=4))
    assert caught.value.reason_code=='hrs_slot_limit_invalid'


@pytest.mark.asyncio
async def test_scoped_margin_cannot_omit_foreign_inventory_and_existing_holdings_cannot_be_added(runtime):
    r=runtime
    with pytest.raises(EntryDenied) as caught:await r.store.reserve('hrs','SUIUSDT','omit',r.sized,1,lambda:r.snapshot(margin=0))
    assert caught.value.reason_code=='hrs_slot_account_margin_understated'
    with pytest.raises(EntryDenied) as caught:await r.store.reserve('hrs','BNBUSDT','add',r.sized,1,r.snapshot)
    assert caught.value.reason_code=='entry_no_additions'


@pytest.mark.asyncio
async def test_claim_change_while_waiting_for_policy_lock_is_not_hidden_by_earlier_snapshot(runtime):
    r=runtime
    earlier=await r.snapshot()
    transaction=r.admin.transaction()
    await transaction.start()
    await r.admin.execute("SELECT pg_advisory_xact_lock(hashtextextended('test',7183))")
    started=asyncio.Event()
    async def fixed():started.set();return earlier
    task=asyncio.create_task(r.store.reserve('hrs','SUIUSDT','waiting',r.sized,1,fixed))
    await started.wait()
    await r.admin.execute("DELETE FROM trading.position_claims WHERE symbol='SOLUSDT'")
    await transaction.commit()
    with pytest.raises(EntryDenied) as caught:await task
    assert caught.value.reason_code=='hrs_slot_claim_owner_conflict'
    assert caught.value.context['hrs_symbols'] is None
    assert caught.value.context['reservation_symbols']==[]
    assert await r.admin.fetchval('SELECT count(*) FROM trading.entry_risk_reservation')==0


@pytest.mark.asyncio
async def test_reservation_audit_is_not_published_when_transaction_commit_fails(runtime):
    import asyncpg
    r=runtime
    await r.admin.execute("""CREATE FUNCTION trading.reject_test_commit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'test deferred commit failure'; END $$;
        CREATE CONSTRAINT TRIGGER reject_test_commit AFTER INSERT ON trading.entry_risk_reservation
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trading.reject_test_commit()""")
    with pytest.raises(asyncpg.RaiseError,match='test deferred commit failure'):
        await r.store.reserve('hrs','SUIUSDT','commit-failed',r.sized,1,r.snapshot)
    assert r.store.reservation_audit is None
    assert await r.admin.fetchval('SELECT count(*) FROM trading.entry_risk_reservation')==0


@pytest.mark.asyncio
async def test_newer_authorized_parent_clock_denial_keeps_exact_locked_gate_metadata(runtime):
    from datetime import datetime
    r=runtime
    earlier=await r.snapshot()
    new_at=await r.admin.fetchval('SELECT clock_timestamp()')
    payload=dict(r.payload,as_of=new_at,available_at=new_at)
    encoded,digest=canonical(payload)
    await r.admin.execute('INSERT INTO trading.hrs_equity_evidence VALUES($1,$2,$3,$4,$4,$5::jsonb)','test',digest,'seg',new_at,encoded)
    await r.admin.execute("UPDATE trading.strategy_equity_gate SET as_of=$1,valid_until=$1::timestamptz+interval '120 seconds',evidence_sha256=$2",new_at,digest)
    async def fixed():return earlier
    with pytest.raises(EntryDenied) as caught:await r.store.reserve('hrs','SUIUSDT','new-parent',r.sized,1,fixed)
    assert caught.value.reason_code=='hrs_slot_source_stale_or_future'
    context=caught.value.context
    assert context['evidence_sha256']==digest and context['as_of']==new_at.isoformat()
    assert context['strategy_id']=='hrs' and context['policy_revision']==1
    assert datetime.fromisoformat(context['checked_at'])>=new_at
    assert context['hrs_symbols'] is None
    assert r.store.reservation_audit is None
