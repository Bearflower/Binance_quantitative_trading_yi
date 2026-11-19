"""Real PostgreSQL plus the real final Binance transport authorization hook."""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from shared.binance_api import BinanceClient
from shared.entry_risk_policy import EntryDenied, RiskPolicyStore, entry_allowed
from strategies.hrs.executor import TradingExecutor
from tests.test_risk.test_hrs_scoped_slots import runtime, pytestmark
from tests.test_risk.test_hrs_min_order_lock import enable, plan, proposal


def denied_executor(r):
    executor=object.__new__(TradingExecutor)
    executor.db=SimpleNamespace(pool=r.pool)
    executor._set_leverage=AsyncMock()
    executor.binance_api=SimpleNamespace(place_order=AsyncMock())
    return executor


@pytest.mark.asyncio
@pytest.mark.parametrize('direction',['long','short'])
async def test_executor_precheck_denial_releases_only_own_unsent_intent(runtime,monkeypatch,direction):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client)
    # An unreadable historical repr must stay denied, never parsed as evidence.
    await r.admin.execute("UPDATE trading.entry_risk_reservation SET geometry=jsonb_set(geometry,'{hrs_budget_context,planned_budget_decision}',to_jsonb('RiskDecision(allowed=True)'::text)) WHERE client_order_id=$1",client)
    await r.admin.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state) VALUES('other','test','hrs',2,'B',1,1,100,10,10,'reserved')")
    original_geometry=await r.admin.fetchval('SELECT geometry FROM trading.entry_risk_reservation WHERE client_order_id=$1',client)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    executor=denied_executor(r)
    result=await getattr(executor,'execute_'+direction)('A',100,1,decision.quantity,1,risk_decision=decision)
    assert not result.order_filled
    executor._set_leverage.assert_not_awaited();executor.binance_api.place_order.assert_not_awaited()
    row=await r.admin.fetchrow('SELECT state,geometry FROM trading.entry_risk_reservation WHERE client_order_id=$1',client)
    assert row['state']=='released'
    assert row['geometry']==original_geometry
    assert await r.admin.fetchval("SELECT claim_state FROM trading.position_claims WHERE symbol='A'")=='RELEASED'
    assert await r.admin.fetchval("SELECT state FROM trading.entry_risk_reservation WHERE client_order_id='other'")=='reserved'
    assert await r.admin.fetchval("SELECT claim_state FROM trading.position_claims WHERE symbol='B'")=='PENDING'


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['submitting','unknown','filled','symbol','revision','account','client','other_active'])
async def test_precheck_denial_cleanup_preserves_ambiguous_or_unmatched_risk(runtime,monkeypatch,change):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client)
    if change in ('submitting','unknown','filled'):
        # The existing schema has only reserved/submitting/released; unknown and
        # filled remain represented by submitting until reconciliation proves flat.
        await r.admin.execute("UPDATE trading.entry_risk_reservation SET state='submitting' WHERE client_order_id=$1",client)
    elif change=='symbol':decision=replace(decision,quantity=decision.quantity+D('.1'))
    elif change=='revision':decision=replace(decision,policy_revision=1)
    elif change=='client':decision=replace(decision,client_order_id='risk_not_this_intent')
    elif change=='account':monkeypatch.setenv('TRADE_ACCOUNT_ID','another-account')
    elif change=='other_active':
        await r.admin.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state) VALUES('other','test','hrs',2,'A',1,1,100,10,10,'submitting')")
    # Explicit precheck denial is induced without touching the transport.
    await r.admin.execute("UPDATE trading.strategy_equity_gate SET allowed=false")
    if change!='account':monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    executor=denied_executor(r)
    result=await executor.execute_short('B' if change=='symbol' else 'A',100,1,decision.quantity,1,risk_decision=decision)
    assert not result.order_filled
    executor._set_leverage.assert_not_awaited();executor.binance_api.place_order.assert_not_awaited()
    state=await r.admin.fetchval('SELECT state FROM trading.entry_risk_reservation WHERE client_order_id=$1',client)
    assert state==('released' if change=='other_active' else 'submitting' if change in ('submitting','unknown','filled') else 'reserved')
    assert await r.admin.fetchval("SELECT claim_state FROM trading.position_claims WHERE symbol='A'")=='PENDING'


@pytest.mark.asyncio
async def test_precheck_cleanup_rechecks_reserved_state_after_waiting_for_account_lock(runtime,monkeypatch):
    import asyncio
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client)
    attempted=asyncio.Event();lock=RiskPolicyStore.lock
    async def observed_lock(store,conn,strategy):
        attempted.set()
        await lock(store,conn,strategy)
    monkeypatch.setattr(RiskPolicyStore,'lock',observed_lock)
    transaction=r.admin.transaction()
    await transaction.start()
    task=None
    try:
        await r.admin.execute("SELECT pg_advisory_xact_lock(hashtextextended('test',7183))")
        task=asyncio.create_task(r.store.release_hrs_precheck_denial('A',decision,reason_code='test_denial'))
        await asyncio.wait_for(attempted.wait(),2)
        assert not task.done()
        # A concurrent authorized path won the lock and entered transport state.
        await r.admin.execute("UPDATE trading.entry_risk_reservation SET state='submitting' WHERE client_order_id=$1",client)
    finally:
        await transaction.commit()
    assert await asyncio.wait_for(task,2) is False
    assert await r.admin.fetchval('SELECT state FROM trading.entry_risk_reservation WHERE client_order_id=$1',client)=='submitting'
    assert await r.admin.fetchval("SELECT claim_state FROM trading.position_claims WHERE symbol='A'")=='PENDING'


@pytest.mark.asyncio
async def test_http_rechecks_original_risk_after_preleverage_authorization(runtime,monkeypatch):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    db=SimpleNamespace(pool=r.pool)
    assert await entry_allowed(SimpleNamespace(db=db),'hrs',risk_decision=decision,quantity=decision.quantity)
    await r.admin.execute('UPDATE trading.entry_risk_reservation SET original_pressure_risk=0 WHERE client_order_id=$1',client)
    transport=AsyncMock(return_value={'sent':True})
    executor=SimpleNamespace(db=db,binance_api=SimpleNamespace(place_order=transport),order_type_entry='LIMIT')
    with pytest.raises(EntryDenied):
        await TradingExecutor._submit_reserved_hrs_entry(executor,'A','BUY',decision.quantity,sized.price,decision)
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_http_authorization_rechecks_after_rate_limiter_on_same_locked_connection(runtime,monkeypatch):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client_id=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client_id)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    client=BinanceClient('test_api_key_12345','test_api_secret_67890')
    client.use_unified_account=False;client._init_session=AsyncMock();client.session=MagicMock()
    client.get_symbol_info=AsyncMock(return_value={'stepSize':'.1','tickSize':D('.01')})
    client.session.request.side_effect=AssertionError('transport must not run')
    async def rate_wait():
        await r.admin.execute('UPDATE trading.entry_risk_reservation SET original_pressure_risk=0 WHERE client_order_id=$1',client_id)
    client.rate_limiter.acquire=AsyncMock(side_effect=rate_wait)
    connections=[]
    authorize=RiskPolicyStore.authorize_hrs_floor
    async def observed(store,connection,*args,**kwargs):
        connections.append(id(connection))
        return await authorize(store,connection,*args,**kwargs)
    monkeypatch.setattr(RiskPolicyStore,'authorize_hrs_floor',observed)
    executor=SimpleNamespace(db=SimpleNamespace(pool=r.pool),binance_api=client,order_type_entry='LIMIT')
    with pytest.raises(EntryDenied):
        await TradingExecutor._submit_reserved_hrs_entry(executor,'A','BUY',decision.quantity,sized.price,decision)
    client.session.request.assert_not_called()
    client.rate_limiter.acquire.assert_awaited_once()
    assert len(connections)==2 and len(set(connections))==1
    assert await r.admin.fetchval('SELECT state FROM trading.entry_risk_reservation WHERE client_order_id=$1',client_id)=='released'


@pytest.mark.asyncio
async def test_valid_floor_intent_reaches_real_post_boundary(runtime,monkeypatch):
    r=runtime;snapshot=await enable(r);initial=plan();sized,geometry=proposal(initial)
    geometry.update(hrs_budget_decision=initial,hrs_budget_inputs=initial.budget_inputs)
    client_id=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client_id)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    assert await entry_allowed(SimpleNamespace(db=SimpleNamespace(pool=r.pool)),'hrs',risk_decision=decision,quantity=decision.quantity)
    client=BinanceClient('test_api_key_12345','test_api_secret_67890')
    client.use_unified_account=False;client._init_session=AsyncMock();client.session=MagicMock()
    client.get_symbol_info=AsyncMock(return_value={'stepSize':'.1','tickSize':D('.01')})
    client.rate_limiter.acquire=AsyncMock()
    class ReachedTransport(RuntimeError):pass
    client.session.request.side_effect=ReachedTransport('POST boundary reached')
    executor=SimpleNamespace(db=SimpleNamespace(pool=r.pool),binance_api=client,order_type_entry='LIMIT')
    with pytest.raises(ReachedTransport,match='POST boundary reached'):
        await TradingExecutor._submit_reserved_hrs_entry(executor,'A','BUY',decision.quantity,sized.price,decision)
    assert client.session.request.call_count==1
    args=client.session.request.call_args
    assert args.args[0]=='POST'
    assert D(args.kwargs['params']['quantity'])==decision.quantity
    assert args.kwargs['params']['newClientOrderId']==client_id


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['pressure_risk','quantity','margin','revision','released','new','allocation','receipt_payload','receipt_missing'])
async def test_http_rejects_changed_pending_source_after_rate_wait_and_releases_unsent_intent(runtime,monkeypatch,change):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    await r.admin.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state,pressure_risk) VALUES('pending','test','hrs',2,'B',1,1,100,10,10,'reserved',28)")
    client_id=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client_id)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    client=BinanceClient('test_api_key_12345','test_api_secret_67890')
    client.use_unified_account=False;client._init_session=AsyncMock();client.session=MagicMock()
    client.get_symbol_info=AsyncMock(return_value={'stepSize':'.1','tickSize':D('.01')})
    client.session.request.side_effect=AssertionError('POST must not run')
    async def rate_wait():
        if change=='receipt_payload':
            await r.admin.execute("UPDATE hrs.hrs_decision_evidence SET payload=jsonb_set(payload,'{account_equity}',to_jsonb('99999'::text))")
        elif change=='receipt_missing':await r.admin.execute('DELETE FROM hrs.hrs_decision_evidence')
        elif change=='allocation':await r.admin.execute("UPDATE public.capital_allocation SET entries='[{\"strategy_id\":\"hrs\",\"allocated_amount\":900}]'")
        elif change=='quantity':await r.admin.execute("UPDATE trading.entry_risk_reservation SET quantity=quantity+1,original_quantity=original_quantity+1 WHERE client_order_id='pending'")
        elif change=='revision':await r.admin.execute("UPDATE trading.entry_risk_reservation SET revision=1 WHERE client_order_id='pending'")
        elif change in ('pressure_risk','margin'):
            await r.admin.execute(f"UPDATE trading.entry_risk_reservation SET {change}={change}+1 WHERE client_order_id='pending'")
        elif change=='released':await r.admin.execute("UPDATE trading.entry_risk_reservation SET state='released' WHERE client_order_id='pending'")
        else:await r.admin.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state,pressure_risk) VALUES('new','test','hrs',2,'C',1,1,100,10,10,'reserved',1)")
    client.rate_limiter.acquire=AsyncMock(side_effect=rate_wait)
    executor=SimpleNamespace(db=SimpleNamespace(pool=r.pool),binance_api=client,order_type_entry='LIMIT')
    with pytest.raises(EntryDenied):
        await TradingExecutor._submit_reserved_hrs_entry(executor,'A','BUY',decision.quantity,sized.price,decision)
    client.rate_limiter.acquire.assert_awaited_once();client.session.request.assert_not_called()
    assert await r.admin.fetchval('SELECT state FROM trading.entry_risk_reservation WHERE client_order_id=$1',client_id)=='released'


@pytest.mark.asyncio
@pytest.mark.parametrize('direction',['long','short'])
async def test_precheck_cleanup_preserves_production_captured_intent_and_appends_state_fact(runtime,monkeypatch,direction):
    """Use the exact read-only captured production function, owner and append ACL."""
    import asyncpg,json
    from pathlib import Path
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    # The isolated cluster may lack this production owner. Create only the
    # fixture owner if absent; never modify an existing role or runtime grants.
    await r.admin.execute("""DO $$ BEGIN
        IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_migration_admin') THEN
            CREATE ROLE trading_migration_admin NOLOGIN NOSUPERUSER;
        END IF; END $$""")
    await r.admin.execute("""CREATE TABLE trading.entry_activity_source_fact(
        id bigserial PRIMARY KEY,account_id text NOT NULL,kind text NOT NULL,payload jsonb NOT NULL,
        observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),created_by text NOT NULL DEFAULT CURRENT_USER);
        ALTER TABLE trading.entry_activity_source_fact OWNER TO trading_migration_admin;
        GRANT USAGE ON SCHEMA trading TO trading_migration_admin;
        GRANT SELECT,INSERT ON trading.entry_activity_source_fact TO trading_user;
        GRANT USAGE,SELECT ON SEQUENCE trading.entry_activity_source_fact_id_seq TO trading_user;""")
    await r.admin.execute(Path('tests/fixtures/hrs_entry_activity_intent.sql').read_text())
    await r.admin.execute('ALTER FUNCTION trading.capture_entry_activity_intent() OWNER TO trading_migration_admin')
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    original=await r.admin.fetchrow('SELECT quantity,geometry FROM trading.entry_risk_reservation WHERE client_order_id=$1',client)
    assert await r.admin.fetchval("SELECT count(*) FROM trading.entry_activity_source_fact WHERE kind='intent'")==1
    async with r.pool.acquire() as conn:
        # The actual append profile permits INSERT but never UPDATE/DELETE.
        assert await conn.fetchval("SELECT has_table_privilege(current_user,'trading.entry_activity_source_fact','INSERT')")
        for sql in ("UPDATE trading.entry_activity_source_fact SET kind='fake'", "DELETE FROM trading.entry_activity_source_fact"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):await conn.execute(sql)
        with pytest.raises(asyncpg.RaiseError,match='Captured entry intent identity is immutable'):
            await conn.execute("UPDATE trading.entry_risk_reservation SET geometry=geometry || '{\"fake\":true}'::jsonb WHERE client_order_id=$1",client)
        with pytest.raises(asyncpg.RaiseError,match='Captured entry intent identity is immutable'):
            await conn.execute('UPDATE trading.entry_risk_reservation SET quantity=quantity+1 WHERE client_order_id=$1',client)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client)
    await r.admin.execute('UPDATE trading.strategy_equity_gate SET allowed=false')
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test');executor=denied_executor(r)
    result=await getattr(executor,'execute_'+direction)('A',100,1,decision.quantity,1,risk_decision=decision)
    assert not result.order_filled
    executor._set_leverage.assert_not_awaited();executor.binance_api.place_order.assert_not_awaited()
    row=await r.admin.fetchrow('SELECT state,quantity,geometry FROM trading.entry_risk_reservation WHERE client_order_id=$1',client)
    assert row['state']=='released' and row['quantity']==original['quantity'] and row['geometry']==original['geometry']
    fact=await r.admin.fetchrow("SELECT payload,created_by FROM trading.entry_activity_source_fact WHERE kind='intent_state'")
    assert fact is not None and fact['created_by']=='trading_migration_admin'
    state=json.loads(fact['payload'])
    assert state['state']=='released' and state['client_order_id']==client
    assert state['geometry']==json.loads(original['geometry']) and D(str(state['quantity']))==original['quantity']
    assert await r.admin.fetchval("SELECT claim_state FROM trading.position_claims WHERE symbol='A'")=='RELEASED'
