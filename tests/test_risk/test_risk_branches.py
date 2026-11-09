"""Reachable risk decisions and failures; PostgreSQL tests opt in explicitly."""
import asyncio
import copy
import json
from datetime import datetime
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import asyncpg
import pytest
from shared.entry_risk_policy import (
    EntryDenied,RiskPolicyStore,SizedEntry,activate_policy,activate_policies,
    allocation_frozen,current_allocation,entry_allowed,entry_geometry,
    prepare_aggressive_entry,recover_reserved_protection,size_reduced_entry,
    submit_aggressive_entry,
)
from test_policy_postgres import DSN,setup

PG=pytest.mark.skipif(not DSN,reason='RISK_TEST_DSN must explicitly select local risk_tests')
FILTERS=dict(stepSize='.1',tickSize='.05',minQty='.1',minNotional='5')
PARTIAL=dict(tp1_close_ratio='.3',tp2_close_ratio='.3')
SIZED=SizedEntry(D(10),D(5),D(100),D(10),D(50),D(25),D(50))


def signal():
    return dict(symbol='AAA',quantity=D(10),entry_price=D(100),leverage=10,
        initial_stop_loss=95,direction='LONG',tp1_price=110,tp2_price=120,
        grade='A',timestamp=datetime(2026,11,4))


def strategy(pool):
    return NS(frequency_controller=NS(state_ready=True),db_manager=NS(pool=pool),positions={},risk_config={},
        binance=NS(get_position=AsyncMock(return_value=[]),get_open_orders=AsyncMock(return_value=[]),place_order=AsyncMock(return_value={'orderId':123})),
        _get_symbol_precision=AsyncMock(return_value=copy.deepcopy(FILTERS)),
        _get_grade_risk=lambda grade:{'partial_take_profit':copy.deepcopy(PARTIAL)},
        _owned_exchange_margin_for_allocation=AsyncMock(return_value=0),
        _get_account_equity=AsyncMock(return_value=1000),_owns_exchange_position=AsyncMock(return_value=True))


def candidate(**changes):
    result=dict(account_id='default',strategy_id='btc_eth_aggressive',mode='monitor_only',
        allow_add=False,multiplier='0',ratio_cap='.15',entry_ceiling='90',reason='test revision')
    result.update(changes)
    return result


@pytest.mark.parametrize('change',[
    {'leverage':'1.5'},{'multiplier':'.51'},{'stop_offset':'1'},
    {'ratios':[]},{'ratios':['.5','.5']},{'tp_offset':'1'},
    {'tp1_price':'99'},{'direction':'SHORT','initial_stop_loss':'105','tp1_price':'101'},
    {'direction':'OTHER'},{'direction':'SHORT','initial_stop_loss':'99'},
        {'minNotional':'1001'},{'stepSize':'0'},{'minQty':'NaN'},
    {'quantity':'Infinity'},{'quantity':'-1'},{'leverage':'bad'},
])
def test_sizing_rejects_invalid_risk_geometry(change):
    data=signal();filters=copy.deepcopy(FILTERS)
    multiplier=change.get('multiplier','.5');offset=change.get('stop_offset','.002')
    for key,value in change.items():
        if key in data:data[key]=value
        if key in filters:filters[key]=value
    with pytest.raises((EntryDenied,KeyError)):
        size_reduced_entry(data,filters,multiplier,offset,change.get('ratios',['.3','.3']),tp_offset=change.get('tp_offset','.0015'))


def test_short_geometry_and_zero_offset():
    data=signal();data.update(direction='SHORT',initial_stop_loss=105,tp1_price=90,tp2_price=80)
    result=size_reduced_entry(data,FILTERS,'.5',0,['.3','.3'],tp_offset=0)
    assert result.quantity==D(5) and result.risk==result.original_risk/2


@pytest.mark.asyncio
async def test_allocator_freezes_even_without_database():
    assert await allocation_frozen(None)


@PG
@pytest.mark.asyncio
async def test_policy_modes_missing_database_and_expiry():
    pool=await setup()
    try:
        obj=strategy(pool)
        assert await entry_allowed(obj,'btc_eth_aggressive')
        assert not await entry_allowed(obj,'btc_eth_aggressive',adding=True)
        assert not await entry_allowed(obj,'hrs')
        await activate_policy(pool,candidate(),expected_revision=1,valid_hours=1)
        assert not await entry_allowed(obj,'btc_eth_aggressive')
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason) VALUES('default','hrs',1,now()-interval '2 hours',now()-interval '1 hour','monitor_only',false,0,0,0,'expired')")
            await conn.execute("INSERT INTO trading.strategy_risk_active VALUES('default','hrs',1)")
        with pytest.raises(EntryDenied,match='expired'):await RiskPolicyStore(obj.db_manager).policy('hrs')
        assert not await entry_allowed(obj,'hrs')
    finally:await pool.close()
    assert not await entry_allowed(obj,'btc_eth_aggressive')


@PG
@pytest.mark.asyncio
async def test_reservation_refuses_changed_policy_duplicate_intent_and_missing_claim():
    pool=await setup()
    try:
        store=RiskPolicyStore(NS(pool=pool))
        async def snapshot():return 0,1000,100
        cid=await store.reserve('btc_eth_aggressive','AAA','same',SIZED,1,snapshot)
        with pytest.raises(EntryDenied,match='already reserved'):await store.reserve('btc_eth_aggressive','AAA','same',SIZED,1,snapshot)
        with pytest.raises(EntryDenied,match='exclusive'):await store.reserve('btc_eth_aggressive','NOCLAIM','none',SIZED,1,snapshot)
        await activate_policy(pool,candidate(),expected_revision=1,valid_hours=1)
        with pytest.raises(EntryDenied,match='changed'):await store.reserve('btc_eth_aggressive','BBB','new',SIZED,1,snapshot)
        with pytest.raises(EntryDenied):
            async with store.submitting('btc_eth_aggressive',cid):pytest.fail('writer must never run')
    finally:await pool.close()


@PG
@pytest.mark.asyncio
async def test_final_policy_revision_race_retains_submitting_reservation():
    pool=await setup()
    try:
        store=RiskPolicyStore(NS(pool=pool))
        async def snapshot():return 0,1000,100
        cid=await store.reserve('btc_eth_aggressive','AAA','race',SIZED,1,snapshot)
        class RacingStore(RiskPolicyStore):
            calls=0
            async def lock(self,conn,strategy_id):
                self.calls+=1
                if self.calls==2:await activate_policy(pool,candidate(),expected_revision=1,valid_hours=1)
                await super().lock(conn,strategy_id)
        with pytest.raises(EntryDenied,match='immediately'):
            async with RacingStore(NS(pool=pool)).submitting('btc_eth_aggressive',cid):pytest.fail('writer ran')
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT state FROM trading.entry_risk_reservation')=='submitting'
    finally:await pool.close()


@PG
@pytest.mark.asyncio
async def test_reconcile_malformed_exchange_and_legacy_orders():
    pool=await setup()
    try:
        store=RiskPolicyStore(NS(pool=pool))
        async def snapshot():return 0,1000,100
        await store.reserve('btc_eth_aggressive','AAA','badexchange',SIZED,1,snapshot)
        exchange=NS(get_order_by_client_id=AsyncMock(return_value=None),get_position=AsyncMock(return_value=None),get_open_orders=AsyncMock(return_value=None))
        with pytest.raises(EntryDenied,match='absence'):await store.reconcile('btc_eth_aggressive',exchange)
        exchange.get_open_orders.return_value=[{'reduceOnly':True}]
        await store.reconcile('btc_eth_aggressive',exchange)
        exchange.get_open_orders.return_value=[{'clientOrderId':'untracked'}]
        with pytest.raises(EntryDenied,match='legacy'):await store.reconcile('btc_eth_aggressive',exchange)
    finally:await pool.close()


@pytest.mark.asyncio
async def test_allocation_decoded_json_missing_duplicate_and_invalid():
    conn=NS(fetchrow=AsyncMock())
    for row in (None,{'entries':[]},{'entries':[{'strategy_id':'x'},{'strategy_id':'x'}]}):
        conn.fetchrow.return_value=row
        with pytest.raises(EntryDenied):await current_allocation(conn,'x')
    conn.fetchrow.return_value={'entries':[{'strategy_id':'x','allocated_amount':100}]}
    assert await current_allocation(conn,'x')==100


@PG
@pytest.mark.asyncio
@pytest.mark.parametrize('scenario',['adding','monitor','stale_retry','missing_identity','bad_positions','unknown_owner','owned','other_owner','zero_position','no_slots','db_error'])
async def test_prepare_snapshot_risk_decisions(scenario):
    pool=await setup()
    try:
        obj=strategy(pool);data=signal()
        expected=False
        if scenario=='adding':obj.positions['AAA']=NS(current_quantity=D(1))
        elif scenario=='monitor':await activate_policy(pool,candidate(),expected_revision=1,valid_hours=1)
        elif scenario=='stale_retry':data['_risk_client_id']='unknown'
        elif scenario=='missing_identity':data.pop('timestamp')
        elif scenario=='bad_positions':obj.binance.get_position.return_value={}
        elif scenario in ('unknown_owner','owned','other_owner','zero_position'):
            obj.binance.get_position.return_value=[{'symbol':'BBB','positionAmt':'0' if scenario=='zero_position' else '1'}]
            obj._owns_exchange_position.return_value=None if scenario=='unknown_owner' else scenario=='owned'
            expected=scenario!='unknown_owner'
            if scenario=='owned':obj.positions['BBB']=NS(current_quantity=D(1))
        elif scenario=='no_slots':obj.risk_config={'position_management':{'max_concurrent_positions':0}}
        elif scenario=='db_error':obj._owned_exchange_margin_for_allocation.side_effect=RuntimeError('DB unavailable')
        assert await prepare_aggressive_entry(obj,'AAA',data) is expected
        obj.binance.place_order.assert_not_called()
    finally:await pool.close()


@PG
@pytest.mark.asyncio
@pytest.mark.parametrize('change',['missing','stop','geometry','symbol','short'])
async def test_final_submission_identity_and_geometry(change):
    pool=await setup()
    try:
        obj=strategy(pool);data=signal()
        if change=='short':data.update(direction='SHORT',initial_stop_loss=105,tp1_price=90,tp2_price=80)
        assert await prepare_aggressive_entry(obj,'AAA',data)
        symbol='AAA'
        if change=='missing':data.pop('_risk_client_id')
        if change=='stop':data['initial_stop_loss']=94
        if change=='geometry':data['tp1_price']=111
        if change=='symbol':symbol='BBB'
        if change=='short':
            await submit_aggressive_entry(obj,symbol,data)
            assert obj.binance.place_order.call_args.kwargs['side']=='SELL'
        else:
            with pytest.raises(EntryDenied):await submit_aggressive_entry(obj,symbol,data)
            obj.binance.place_order.assert_not_called()
    finally:await pool.close()


@PG
@pytest.mark.asyncio
@pytest.mark.parametrize('case',['bad_order','zero_fill','wrong_side','bad_positions','missing_symbol','zero_qty','qty_exceeds_fill','fill_exceeds_reservation','wrong_direction','bad_algos','already_covered','stop_failure','transport_error','short'])
async def test_recovery_requires_proof_and_preserves_reservation_on_every_error(case):
    pool=await setup()
    try:
        store=RiskPolicyStore(NS(pool=pool))
        async def snapshot():return 0,1000,100
        direction='SHORT' if case=='short' else 'LONG'
        geometry={'signal':{'direction':direction,'initial_stop_loss':'105' if case=='short' else '95'},'stop_offset':'.002','filters':{'tickSize':'.01'}}
        cid=await store.reserve('btc_eth_aggressive','AAA','recover',SIZED,1,snapshot,geometry=geometry)
        order={'symbol':'AAA','clientOrderId':cid,'executedQty':'2','side':'SELL' if case=='short' else 'BUY'}
        positions=[{'symbol':'AAA','positionAmt':'-2' if case=='short' else '2'}]
        exchange=NS(get_order_by_client_id=AsyncMock(return_value=order),get_position=AsyncMock(return_value=positions),get_open_algo_orders=AsyncMock(return_value=[]))
        obj=NS(db_manager=NS(pool=pool),binance=exchange,strategy_name='aggressive',_hard_stop_covered_qty=lambda *a:0,_place_conditional_order_and_record=AsyncMock(return_value='stop'))
        if case=='bad_order':exchange.get_order_by_client_id.return_value={}
        elif case=='zero_fill':order['executedQty']='0'
        elif case=='wrong_side':order['side']='SELL'
        elif case=='bad_positions':exchange.get_position.return_value={}
        elif case=='missing_symbol':positions[0]['symbol']='WRONG'
        elif case=='zero_qty':positions[0]['positionAmt']='0'
        elif case=='qty_exceeds_fill':positions[0]['positionAmt']='3'
        elif case=='fill_exceeds_reservation':order['executedQty']='6'
        elif case=='wrong_direction':positions[0]['positionAmt']='-2'
        elif case=='bad_algos':exchange.get_open_algo_orders.return_value={}
        elif case=='already_covered':obj._hard_stop_covered_qty=lambda *a:2
        elif case=='stop_failure':obj._place_conditional_order_and_record.return_value=None
        elif case=='transport_error':exchange.get_order_by_client_id.side_effect=TimeoutError()
        await recover_reserved_protection(obj)
        assert obj._place_conditional_order_and_record.await_count == (1 if case in ('short','stop_failure') else 0)
        if case=='short':assert obj._place_conditional_order_and_record.call_args.args[1]=='BUY'
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT state FROM trading.entry_risk_reservation')=='reserved'
    finally:await pool.close()


@pytest.mark.asyncio
async def test_recovery_database_failure_leaves_monitor_alive():
    await recover_reserved_protection(NS(db_manager=None))


@PG
@pytest.mark.asyncio
async def test_decoded_json_geometry_supported_by_submission_and_recovery():
    pool=await setup()
    obj=strategy(pool);data=signal()
    assert await prepare_aggressive_entry(obj,'AAA',data)
    cid=data['_risk_client_id']
    await pool.close()
    async def decoded_role(conn):
        await conn.execute('SET ROLE risk_runtime')
        await conn.set_type_codec('jsonb',schema='pg_catalog',encoder=json.dumps,decoder=json.loads,format='text')
    pool=await asyncpg.create_pool(DSN,min_size=1,max_size=1,setup=decoded_role)
    try:
        obj.db_manager=NS(pool=pool)
        await submit_aggressive_entry(obj,'AAA',data)
        obj.binance.get_order_by_client_id=AsyncMock(return_value={'symbol':'AAA','clientOrderId':cid,'executedQty':'2','side':'BUY'})
        obj.binance.get_position.return_value=[{'symbol':'AAA','positionAmt':'2'}]
        obj.binance.get_open_algo_orders=AsyncMock(return_value=[])
        obj._hard_stop_covered_qty=lambda *a:0
        obj._place_conditional_order_and_record=AsyncMock(return_value='stop')
        obj.strategy_name='aggressive'
        await recover_reserved_protection(obj)
        assert obj._place_conditional_order_and_record.await_count==1
    finally:await pool.close()


@PG
@pytest.mark.asyncio
async def test_activation_input_validation_and_allocation_cas():
    pool=await setup()
    try:
        for candidates,hours in [([],1),([candidate()],0),([candidate()],745),([candidate(),candidate()],1),([candidate(),candidate(account_id='other',strategy_id='hrs')],1)]:
            with pytest.raises(EntryDenied):await activate_policies(pool,candidates,expected_revision=1,valid_hours=hours)
        for changes in ({'reason':' '},{'allow_add':True},{'allocation_amount':'99'}):
            with pytest.raises(EntryDenied):await activate_policy(pool,candidate(**changes),expected_revision=1,valid_hours=1)
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT revision FROM trading.strategy_risk_active')==1
    finally:await pool.close()


@PG
@pytest.mark.asyncio
async def test_cli_dry_run_then_atomic_four_candidate_activation(monkeypatch):
    from scripts.activate_entry_risk_policy import parser,run
    pool=await setup();await pool.close()
    admin=await asyncpg.connect(DSN)
    try:
        await admin.execute('TRUNCATE trading.entry_risk_reservation,trading.strategy_risk_active,trading.strategy_risk_policy,trading.strategy_risk_audit CASCADE')
        entries=[{'strategy_id':s,'allocated_amount':amount} for s,amount in [('btc_eth_aggressive',100),('btc_eth',200),('new_coin',250)]]
        await admin.execute('UPDATE public.capital_allocation SET entries=$1::jsonb',json.dumps(entries))
        original=await admin.fetchval('SELECT entries FROM public.capital_allocation')
        monkeypatch.setenv('RISK_POLICY_ADMIN_DSN',DSN)
        args=parser().parse_args(['--verified-equity','1000','--reason','local test only','--expected-revision','0'])
        args.approved_ceilings=json.dumps({'hrs':'0','btc_eth_aggressive':'50','btc_eth':'100','new_coin':'120'})
        assert not args.activate and not args.enable_reduced
        result=await run(args)
        assert result['dry_run'] and len(result['candidates'])==4
        assert {p['strategy_id']:p['entry_ceiling'] for p in result['candidates']}=={'hrs':'0','btc_eth_aggressive':'50','btc_eth':'100','new_coin':'120'}
        assert next(p for p in result['candidates'] if p['strategy_id']=='btc_eth_aggressive')['mode']=='monitor_only'
        assert await admin.fetchval('SELECT count(*) FROM trading.strategy_risk_policy')==0
        args.activate=True;args.enable_reduced=True
        result=await run(args)
        assert result['active_revision']==1
        assert await admin.fetchval('SELECT count(*) FROM trading.strategy_risk_active')==4
        assert await admin.fetchval('SELECT count(*) FROM trading.strategy_risk_audit')==4
        assert await admin.fetchval('SELECT entries FROM public.capital_allocation')==original
        with pytest.raises(EntryDenied):await run(args)
        assert await admin.fetchval('SELECT count(*) FROM trading.strategy_risk_policy')==4
        # A conflict in the second candidate must roll back the already inserted first one.
        await admin.execute("UPDATE trading.strategy_risk_active SET revision=1 WHERE strategy_id='hrs'")
        args.expected_revision=1
        await admin.execute("INSERT INTO trading.strategy_risk_policy SELECT account_id,strategy_id,2,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,freeze_allocation,reason,created_at FROM trading.strategy_risk_policy WHERE strategy_id='btc_eth_aggressive'")
        await admin.execute("UPDATE trading.strategy_risk_active SET revision=2 WHERE strategy_id='btc_eth_aggressive'")
        with pytest.raises(EntryDenied):await run(args)
        assert await admin.fetchval("SELECT count(*) FROM trading.strategy_risk_policy WHERE strategy_id='hrs'")==1
    finally:await admin.close()
