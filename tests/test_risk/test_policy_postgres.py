"""Real local PostgreSQL only. Never uses production or ledger_tests."""
import asyncio
import os
from urllib.parse import urlsplit
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal as D
import asyncpg
import pytest
from shared.entry_risk_policy import RiskPolicyStore, SizedEntry, EntryDenied

DSN=os.environ.get('RISK_TEST_DSN','')
pytestmark=pytest.mark.skipif(not DSN,reason='RISK_TEST_DSN is not set; real PostgreSQL risk tests require an explicit isolated local risk_tests database')
if DSN:
    parsed=urlsplit(DSN)
    if parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.path!='/risk_tests':
        raise ValueError('RISK_TEST_DSN must name local isolated database risk_tests; production/ledger databases forbidden')
MIGRATION=Path(__file__).parents[2]/'database/postgres/migrations/2026-11-04-entry-risk-policy.sql'


def test_migration_exists():
    assert MIGRATION.exists()


async def setup():
    admin=await asyncpg.connect(DSN)
    await admin.execute('CREATE SCHEMA IF NOT EXISTS trading')
    await admin.execute('''CREATE TABLE IF NOT EXISTS trading.position_claims(id bigserial primary key,
        symbol text,strategy text,claim_state text,expires_at timestamptz,released_at timestamptz,reason text)''')
    await admin.execute('''DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='risk_runtime') THEN CREATE ROLE risk_runtime LOGIN NOSUPERUSER; END IF; END $$''')
    # Rebuild only dedicated risk test tables: exercise the current migration, not
    # stale constraints left by an earlier uncommitted migration draft.
    await admin.execute('DROP TABLE IF EXISTS trading.entry_risk_reservation,trading.strategy_risk_active,trading.strategy_risk_policy,trading.strategy_risk_audit CASCADE')
    await admin.execute(MIGRATION.read_text())
    await admin.execute('CREATE TABLE IF NOT EXISTS public.capital_allocation(month date,status text,entries jsonb,created_at timestamptz default now())')
    await admin.execute('TRUNCATE public.capital_allocation')
    await admin.execute("INSERT INTO public.capital_allocation(month,status,entries) VALUES(date_trunc('month',now() AT TIME ZONE 'Asia/Shanghai'),'active','[{\"strategy_id\":\"btc_eth_aggressive\",\"allocated_amount\":100}]')")
    await admin.execute('GRANT SELECT ON public.capital_allocation TO risk_runtime')
    await admin.execute('TRUNCATE trading.entry_risk_reservation,trading.strategy_risk_active,trading.strategy_risk_policy,trading.strategy_risk_audit,trading.position_claims CASCADE')
    await admin.execute('''GRANT USAGE ON SCHEMA trading TO risk_runtime;
        GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA trading TO risk_runtime;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA trading TO risk_runtime''')
    await admin.execute('''INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason)
        VALUES('default','btc_eth_aggressive',1,now()-interval '1 hour',now()+interval '1 day','reduced',false,.5,.15,100,'test')''')
    await admin.execute("INSERT INTO trading.strategy_risk_active VALUES('default','btc_eth_aggressive',1)")
    await admin.execute("INSERT INTO trading.position_claims(symbol,strategy,claim_state,expires_at) VALUES('AAA','btc_eth_aggressive','PENDING',now()+interval '30 minutes'),('BBB','btc_eth_aggressive','PENDING',now()+interval '30 minutes')")
    await admin.close()
    async def runtime_role(conn):await conn.execute('SET ROLE risk_runtime')
    pool=await asyncpg.create_pool(DSN,min_size=2,max_size=3,setup=runtime_role)
    return pool


@pytest.mark.asyncio
async def test_two_connections_one_budget_and_durable_unknown():
    pool=await setup()
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        sized=SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50))
        async def snapshot():
            await asyncio.sleep(.05)
            return D(20),D(1000),D(100)
        outcomes=await asyncio.gather(store.reserve('btc_eth_aggressive','AAA','a',sized,1,snapshot),store.reserve('btc_eth_aggressive','BBB','b',sized,1,snapshot),return_exceptions=True)
        assert sum(isinstance(x,str) for x in outcomes)==1
        assert sum(isinstance(x,EntryDenied) for x in outcomes)==1
        cid=next(x for x in outcomes if isinstance(x,str))
        with pytest.raises(TimeoutError):
            async with store.submitting('btc_eth_aggressive',cid):
                raise TimeoutError('exchange accepted then response lost')
        # A fresh service/restart sees persisted submitting occupancy.
        fresh=RiskPolicyStore(SimpleNamespace(pool=pool))
        class UnknownExchange:
            async def get_order_by_client_id(self,*args): raise TimeoutError()
            async def get_open_orders(self): return []
        await fresh.reconcile('btc_eth_aggressive',UnknownExchange())
        async with pool.acquire() as c:
            row=await c.fetchrow('SELECT * FROM trading.entry_risk_reservation WHERE client_order_id=$1',cid)
            assert row['state']=='submitting'
            # Existing cleanup/release paths are prevented at database level.
            await c.execute("UPDATE trading.position_claims SET claim_state='RELEASED',expires_at=now()-interval '1 day' WHERE symbol=$1",row['symbol'])
            claim=await c.fetchrow('SELECT * FROM trading.position_claims WHERE symbol=$1',row['symbol'])
            assert claim['claim_state']=='PENDING'
            assert claim['expires_at'].year==9999
        with pytest.raises(EntryDenied):
            await fresh.reserve('btc_eth_aggressive','BBB','retry',sized,1,snapshot)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_partial_fill_unknown_keeps_claim_until_terminal_flat():
    pool=await setup()
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        async def snapshot():return 0,1000,100
        cid=await store.reserve('btc_eth_aggressive','AAA','partial',SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50)),1,snapshot)
        class Exchange:
            status='PARTIALLY_FILLED'; qty='2'
            async def get_order_by_client_id(self,*args):return {'symbol':'AAA','clientOrderId':cid,'status':self.status,'executedQty':'2'}
            async def get_position(self,*args):return [{'symbol':'AAA','positionAmt':self.qty}]
            async def get_um_account_positions(self):return [{'symbol':'AAA','positionAmt':self.qty}]
            async def get_open_orders(self,symbol=None):return [] if self.status=='CANCELED' else [{'clientOrderId':cid}]
            async def get_open_algo_orders(self,symbol=None):return []
        exchange=Exchange()
        await store.reconcile('btc_eth_aggressive',exchange)
        async with pool.acquire() as c:
            assert await c.fetchval('SELECT state FROM trading.entry_risk_reservation')=='reserved'
        exchange.status='CANCELED'
        await store.reconcile('btc_eth_aggressive',exchange)
        async with pool.acquire() as c:
            assert await c.fetchval('SELECT state FROM trading.entry_risk_reservation')=='reserved'
        exchange.qty='0'
        await store.reconcile('btc_eth_aggressive',exchange)
        async with pool.acquire() as c:
            assert await c.fetchval('SELECT state FROM trading.entry_risk_reservation')=='released'
            assert await c.fetchval("SELECT claim_state FROM trading.position_claims WHERE symbol='AAA'")=='RELEASED'
    finally:await pool.close()


@pytest.mark.asyncio
async def test_zero_symbol_row_cannot_override_nonzero_account_inventory():
    pool=await setup()
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        async def snapshot():return 0,1000,100
        cid=await store.reserve('btc_eth_aggressive','AAA','zero-row-disagrees',SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50)),1,snapshot)
        class Exchange:
            async def get_order_by_client_id(self,*args):
                return {'symbol':'AAA','clientOrderId':cid,'status':'FILLED','executedQty':'5'}
            async def get_position(self,symbol=None):
                return [{'symbol':'AAA','positionAmt':'0'}]
            async def get_um_account_positions(self):
                return [{'symbol':'AAA','positionAmt':'1'}]
            async def get_open_orders(self,symbol=None):return []
            async def get_open_algo_orders(self,symbol=None):return []
        await store.reconcile('btc_eth_aggressive',Exchange())
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT state FROM trading.entry_risk_reservation')=='reserved'
    finally:await pool.close()

@pytest.mark.asyncio
async def test_pm_empty_symbol_response_needs_independent_flat_evidence():
    pool=await setup()
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        async def snapshot():return 0,1000,100
        cid=await store.reserve('btc_eth_aggressive','AAA','empty',SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50)),1,snapshot)
        class Exchange:
            positions=[]
            order_symbol='AAA'; order_client_id=cid
            async def get_order_by_client_id(self,*args):return {'symbol':self.order_symbol,'clientOrderId':self.order_client_id,'status':'CANCELED','executedQty':'0'}
            async def get_position(self,*args):return self.positions
            async def get_um_account_positions(self):return []
            async def get_open_orders(self,*args):return []
            async def get_open_algo_orders(self,*args):return []
        exchange=Exchange()
        for positions in ([{'symbol':'WRONG','positionAmt':'0'}],[{'symbol':'AAA','positionAmt':'NaN'}]):
            exchange.positions=positions
            await store.reconcile('btc_eth_aggressive',exchange)
            async with pool.acquire() as c:
                assert await c.fetchval('SELECT state FROM trading.entry_risk_reservation')=='reserved'
        exchange.positions=[]
        for order_symbol,order_client_id in [('WRONG',cid),('AAA','WRONG')]:
            exchange.order_symbol,exchange.order_client_id=order_symbol,order_client_id
            await store.reconcile('btc_eth_aggressive',exchange)
            async with pool.acquire() as c:
                assert await c.fetchval('SELECT state FROM trading.entry_risk_reservation')=='reserved'
        exchange.order_symbol,exchange.order_client_id='AAA',cid
        await store.reconcile('btc_eth_aggressive',exchange)
        async with pool.acquire() as c:
            assert await c.fetchval('SELECT state FROM trading.entry_risk_reservation')=='released'
    finally:await pool.close()


@pytest.mark.asyncio
async def test_empty_symbol_response_releases_only_with_independent_account_proof():
    pool=await setup()
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        async def snapshot():return 0,1000,100
        cid=await store.reserve('btc_eth_aggressive','AAA','flat-confirmed',SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50)),1,snapshot)
        class Exchange:
            account_positions=[{'symbol':'AAA','positionAmt':'1'}]
            symbol_orders=[]
            async def get_order_by_client_id(self,*args):
                return {'symbol':'AAA','clientOrderId':cid,'status':'FILLED','executedQty':'5'}
            async def get_position(self,symbol=None):return []
            async def get_um_account_positions(self):return self.account_positions
            async def get_open_orders(self,symbol=None):
                return self.symbol_orders if symbol else []
            async def get_open_algo_orders(self,symbol=None):return []
        exchange=Exchange()
        await store.reconcile('btc_eth_aggressive',exchange)
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT state FROM trading.entry_risk_reservation')=='reserved'
        exchange.account_positions=[]
        exchange.symbol_orders=[{'orderId':123}]
        await store.reconcile('btc_eth_aggressive',exchange)
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT state FROM trading.entry_risk_reservation')=='reserved'
        exchange.symbol_orders=[]
        await store.reconcile('btc_eth_aggressive',exchange)
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT state FROM trading.entry_risk_reservation')=='released'
            assert await conn.fetchval("SELECT claim_state FROM trading.position_claims WHERE symbol='AAA'")=='RELEASED'
    finally:await pool.close()

@pytest.mark.asyncio
async def test_atomic_activation_cas_and_audit():
    pool=await setup()
    try:
        from shared.entry_risk_policy import activate_policy
        candidate=dict(account_id='default',strategy_id='btc_eth_aggressive',mode='monitor_only',allow_add=False,multiplier='0',ratio_cap='.15',entry_ceiling='90',reason='reviewed remediation')
        assert await activate_policy(pool,candidate,expected_revision=1,valid_hours=24)==2
        with pytest.raises(EntryDenied):
            await activate_policy(pool,candidate,expected_revision=1,valid_hours=24)
        async with pool.acquire() as c:
            assert await c.fetchval("SELECT revision FROM trading.strategy_risk_active")==2
            assert await c.fetchval("SELECT count(*) FROM trading.strategy_risk_audit")==1
    finally:await pool.close()


@pytest.mark.asyncio
async def test_pending_reservations_count_for_slot_limit():
    pool=await setup()
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        async def snapshot():return 0,10000,10000,set(),1
        sized=SizedEntry(D(10),D(5),D(10),D(10),D(5),D(2),D(4))
        await store.reserve('btc_eth_aggressive','AAA','slot1',sized,1,snapshot)
        with pytest.raises(EntryDenied,match='slots'):
            await store.reserve('btc_eth_aggressive','BBB','slot2',sized,1,snapshot)
    finally:await pool.close()

@pytest.mark.asyncio
async def test_ambiguous_reserved_fill_rebuilds_only_missing_original_hard_stop():
    from unittest.mock import AsyncMock
    from shared.entry_risk_policy import recover_reserved_protection
    pool=await setup()
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        async def snapshot():return 0,1000,100
        geometry={'signal':{'direction':'LONG','initial_stop_loss':'95'},'stop_offset':'.002','filters':{'tickSize':'.01'}}
        cid=await store.reserve('btc_eth_aggressive','AAA','protection',SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50)),1,snapshot,geometry=geometry)
        exchange=SimpleNamespace(get_order_by_client_id=AsyncMock(return_value={'clientOrderId':cid,'symbol':'AAA','executedQty':'2','side':'BUY'}),get_position=AsyncMock(return_value=[{'symbol':'AAA','positionAmt':'2'}]),get_open_algo_orders=AsyncMock(return_value=[]))
        strategy=SimpleNamespace(db_manager=SimpleNamespace(pool=pool),binance=exchange,my_record_name='btc_eth_aggressive',strategy_name='aggressive',_hard_stop_covered_qty=lambda *a:D(0),_place_conditional_order_and_record=AsyncMock(return_value='123'))
        await recover_reserved_protection(strategy)
        args=strategy._place_conditional_order_and_record.call_args.args
        assert args[:7]==('AAA','SELL','STOP',D('95'),D('94.81'),D('2'),'STOP_LOSS')
        exchange.get_order_by_client_id.return_value['executedQty']='5'
        exchange.get_position.return_value=[{'symbol':'AAA','positionAmt':'5'}]
        strategy._hard_stop_covered_qty=lambda *a:D(2)
        await recover_reserved_protection(strategy)
        assert strategy._place_conditional_order_and_record.call_args.args[5]==D(3)
        async with pool.acquire() as c:
            assert await c.fetchval('SELECT state FROM trading.entry_risk_reservation')=='reserved'
    finally:await pool.close()

@pytest.mark.asyncio
async def test_single_connection_snapshot_cannot_deadlock():
    initial=await setup();await initial.close()
    async def runtime_role(conn):await conn.execute('SET ROLE risk_runtime')
    pool=await asyncpg.create_pool(DSN,min_size=1,max_size=1,setup=runtime_role)
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        async def snapshot():
            async with pool.acquire() as conn:await conn.fetchval('SELECT 1')
            return 0,1000,100
        cid=await asyncio.wait_for(store.reserve('btc_eth_aggressive','AAA','single',SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50)),1,snapshot),timeout=2)
        assert cid.startswith('risk_')
    finally:await pool.close()


@pytest.mark.asyncio
async def test_production_runtime_grants_nosuperuser():
    initial=await setup();await initial.close()
    admin=await asyncpg.connect(DSN.replace('/postgres','/risk_tests'))
    await admin.execute("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user') THEN CREATE ROLE trading_user LOGIN NOSUPERUSER; END IF; END $$")
    assert not await admin.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname='trading_user'")
    await admin.execute('REVOKE ALL ON trading.strategy_risk_policy,trading.strategy_risk_active,trading.strategy_risk_audit,trading.entry_risk_reservation FROM trading_user')
    # Existing production claim permissions are independent of this migration.
    await admin.execute('GRANT SELECT,UPDATE ON trading.position_claims TO trading_user')
    await admin.execute('GRANT SELECT ON public.capital_allocation TO trading_user')
    await admin.execute(MIGRATION.read_text())
    await admin.close()
    async def runtime_role(connection):
        await connection.execute('SET ROLE trading_user')
    pool=await asyncpg.create_pool(DSN.replace('/postgres','/risk_tests'),min_size=1,max_size=2,setup=runtime_role)
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        assert (await store.policy('btc_eth_aggressive'))['revision']==1
        async def snapshot():return 0,1000,100
        cid=await store.reserve('btc_eth_aggressive','AAA','production-grants',SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50)),1,snapshot)
        async with store.submitting('btc_eth_aggressive',cid):pass
        async with pool.acquire() as conn:
            with pytest.raises(Exception) as denied:
                await conn.execute("UPDATE trading.strategy_risk_active SET revision=revision")
            assert getattr(denied.value,"sqlstate",None)=="42501"
            with pytest.raises(Exception) as denied:
                await conn.execute("CREATE TABLE trading.should_not_be_allowed(x int)")
            assert getattr(denied.value,"sqlstate",None)=="42501"
    finally:await pool.close()

@pytest.mark.asyncio
async def test_reduced_prepare_retry_and_final_submit_uses_exact_reserved_quantity():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from unittest.mock import AsyncMock
    from shared.entry_risk_policy import prepare_aggressive_entry,submit_aggressive_entry
    pool=await setup()
    admin=await asyncpg.connect(DSN.replace('/postgres','/risk_tests'))
    await admin.execute('CREATE TABLE IF NOT EXISTS public.capital_allocation(month date,status text,entries jsonb,created_at timestamptz default now())')
    await admin.execute('TRUNCATE public.capital_allocation')
    month=datetime.now(ZoneInfo('Asia/Shanghai')).date().replace(day=1)
    await admin.execute('INSERT INTO public.capital_allocation(month,status,entries) VALUES($1,\'active\',\'[{"strategy_id":"btc_eth_aggressive","allocated_amount":100}]\')',month)
    await admin.execute('GRANT SELECT ON public.capital_allocation TO risk_runtime')
    await admin.close()
    try:
        filters=dict(stepSize='.1',tickSize='.05',minQty='.1',minNotional='5')
        partial=dict(tp1_close_ratio='.3',tp2_close_ratio='.3')
        exchange=SimpleNamespace(get_position=AsyncMock(return_value=[]),get_open_orders=AsyncMock(return_value=[]),place_order=AsyncMock(return_value={'orderId':123}))
        strategy=SimpleNamespace(frequency_controller=SimpleNamespace(state_ready=True),db_manager=SimpleNamespace(pool=pool),positions={},binance=exchange,
            _get_symbol_precision=AsyncMock(return_value=filters),_get_grade_risk=lambda grade:{'partial_take_profit':partial},risk_config={},
            _owned_exchange_margin_for_allocation=AsyncMock(return_value=0),_get_account_equity=AsyncMock(return_value=1000))
        signal=dict(symbol='AAA',quantity=D('10.03'),entry_price=D('100.034'),leverage=10,initial_stop_loss=95,direction='LONG',tp1_price=110,tp2_price=120,grade='A',timestamp=datetime(2026,11,4))
        assert await prepare_aggressive_entry(strategy,'AAA',signal)
        assert signal['quantity']==D(5)
        cid=signal['_risk_client_id']
        assert await prepare_aggressive_entry(strategy,'AAA',signal)
        assert signal['quantity']==D(5) and signal['_risk_original_quantity']==D('10.03')
        await submit_aggressive_entry(strategy,'AAA',signal)
        assert exchange.place_order.call_args.kwargs['quantity']==D(5)
        assert exchange.place_order.call_args.kwargs['price']==D('100.05')
        assert exchange.place_order.call_args.kwargs['client_order_id']==cid
        with pytest.raises(EntryDenied):await submit_aggressive_entry(strategy,'AAA',signal)
        assert exchange.place_order.await_count==1
    finally:await pool.close()

@pytest.mark.asyncio
async def test_same_symbol_concurrent_intents_cannot_add():
    pool=await setup()
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        async def snapshot():return 0,1000,100
        sized=SizedEntry(D(10),D(5),D(10),D(10),D(5),D(2),D(4))
        results=await asyncio.gather(*(store.reserve('btc_eth_aggressive','AAA',i,sized,1,snapshot) for i in ('first','second')),return_exceptions=True)
        assert sum(isinstance(x,str) for x in results)==1
    finally:await pool.close()


@pytest.mark.asyncio
async def test_versioned_policy_and_audit_are_immutable_even_for_administrator():
    pool=await setup();await pool.close()
    admin=await asyncpg.connect(DSN.replace('/postgres','/risk_tests'))
    try:
        with pytest.raises(Exception) as immutable:
            await admin.execute("UPDATE trading.strategy_risk_policy SET entry_ceiling=999")
        assert getattr(immutable.value,"sqlstate",None)=="P0001"
        with pytest.raises(Exception) as immutable:
            await admin.execute("DELETE FROM trading.strategy_risk_policy")
        assert getattr(immutable.value,"sqlstate",None)=="P0001"
        await admin.execute("INSERT INTO trading.strategy_risk_audit(account_id,strategy_id,previous_revision,revision,reason) VALUES('default','btc_eth_aggressive',0,1,'test')")
        for sql in ("UPDATE trading.strategy_risk_audit SET reason='changed'","DELETE FROM trading.strategy_risk_audit"):
            with pytest.raises(Exception) as immutable:
                await admin.execute(sql)
            assert getattr(immutable.value,"sqlstate",None)=="P0001"
    finally:await admin.close()

@pytest.mark.asyncio
async def test_policy_expiry_while_waiting_account_lock_rejects_submission():
    pool=await setup()
    admin=await asyncpg.connect(DSN)
    try:
        store=RiskPolicyStore(SimpleNamespace(pool=pool))
        async def snapshot():return 0,1000,100
        cid=await store.reserve('btc_eth_aggressive','AAA','expire-lock',SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50)),1,snapshot)
        # Activation uses the same revision payload only as test setup; expiry is short.
        await admin.execute("INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason) VALUES('default','btc_eth_aggressive',2,clock_timestamp(),clock_timestamp()+interval '.15 seconds','reduced',false,.5,.15,100,'expiry test')")
        await admin.execute("UPDATE trading.strategy_risk_active SET revision=2")
        await admin.execute("UPDATE trading.entry_risk_reservation SET revision=2")
        waiting,go=asyncio.Event(),asyncio.Event()
        class SecondStageStore(RiskPolicyStore):
            calls=0
            async def lock(self,conn,strategy):
                self.calls+=1
                if self.calls==2:
                    waiting.set()
                    await go.wait()
                await super().lock(conn,strategy)
        final_store=SecondStageStore(SimpleNamespace(pool=pool))
        async def submit():
            async with final_store.submitting('btc_eth_aggressive',cid):return 'writer-reached'
        task=asyncio.create_task(submit())
        await waiting.wait()
        await admin.execute('BEGIN')
        await store.lock(admin,'btc_eth_aggressive')
        go.set()
        await asyncio.sleep(.25)
        await admin.execute('COMMIT')
        with pytest.raises(EntryDenied,match='expired'):
            await task
    finally:await admin.close();await pool.close()
