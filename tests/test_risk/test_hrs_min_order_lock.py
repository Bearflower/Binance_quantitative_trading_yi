"""Floor sizing is refined inside the existing real-PG account lock."""
import asyncio,json
from dataclasses import replace, asdict
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from shared.entry_risk_policy import EntryDenied, RiskPolicyStore, SizedEntry
from strategies.hrs.risk_budget import FLOOR, assess_entry
from strategies.hrs.equity_evidence import canonical
from tests.test_risk.test_hrs_scoped_slots import runtime, pytestmark


def plan(allocation='1000'):
    return assess_entry(allocation=D(allocation),entry_price=D(100),stop_price=D(99),direction='long',existing_open_risk=D(0),leverage=D(10),remaining_margin=D(500),qty_step=D('.1'),min_qty=D('.1'),min_notional=D(5),fee_rate=D('.0005'),pressure_slippage=D('.02'),take_profit_prices=(D(105),D(110)),budget_version=FLOOR)


def proposal(decision):
    return SizedEntry(decision.quantity,decision.quantity,D(100),D(10),decision.quantity*10,decision.worst_loss,decision.worst_loss,decision.exit_ratios),dict(hrs_risk_budget_version=FLOOR,hrs_budget_inputs=json.loads(json.dumps(decision.budget_inputs,default=str)),hrs_budget_decision=json.loads(json.dumps(asdict(decision),default=str)),hrs_budget_source='hrs_live_assessor_v1')


async def enable(r):
    await r.admin.execute("""INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason,created_at) SELECT account_id,strategy_id,2,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,$1,created_at FROM trading.strategy_risk_policy WHERE revision=1""",json.dumps({'hrs_risk_budget_version':FLOOR}))
    await r.admin.execute("UPDATE trading.strategy_risk_active SET revision=2")
    # Existing ownership fixture has deliberately minimal local state. Supply real
    # protection for the new risk proof in both the authorized parent and snapshot.
    r.payload['capture']['local']['WLDUSDT'].update(entry_price='10',risk_stop_price='9.9',algo_ids={'sl':123})
    encoded,digest=canonical(r.payload)
    await r.admin.execute('INSERT INTO trading.hrs_equity_evidence VALUES($1,$2,$3,$4,$4,$5::jsonb)','test',digest,'seg',r.at,encoded)
    await r.admin.execute('UPDATE trading.strategy_equity_gate SET evidence_sha256=$1,policy_revision=2',digest)
    async def snapshot(margin=100):
        base=await r.snapshot(margin=margin)
        base.slot_evidence['local'].update(r.payload['capture']['local'])
        return base
    return snapshot


@pytest.mark.asyncio
async def test_locked_allocation_reduces_quantity_and_persists_recomputed_inputs(runtime):
    r=runtime;snapshot=await enable(r);initial=plan();sized,geometry=proposal(initial)
    await r.admin.execute("UPDATE public.capital_allocation SET entries='[{\"strategy_id\":\"hrs\",\"allocated_amount\":120}]'")
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    row=await r.admin.fetchrow('SELECT * FROM trading.entry_risk_reservation WHERE client_order_id=$1',client)
    decision=r.store.reservation_audit['risk_decision']
    assert decision.quantity<initial.quantity and row['quantity']==decision.quantity
    assert row['pressure_risk']==decision.worst_loss
    saved=json.loads(row['geometry'])
    assert D(saved['hrs_budget_inputs']['allocation'])==120
    assert saved['hrs_budget_context']['checked_at']==saved['slot_context']['checked_at']
    assert saved['hrs_budget_context']['local_risk_by_symbol']['WLDUSDT']


@pytest.mark.asyncio
async def test_floor_requires_active_opt_in_and_scoped_snapshot(runtime):
    r=runtime;sized,geometry=proposal(plan())
    with pytest.raises(EntryDenied):await r.store.reserve('hrs','A','a',sized,1,r.snapshot,geometry=geometry)
    snapshot=await enable(r)
    async def legacy():return D(100),D(10000),D(1000)
    with pytest.raises(EntryDenied):await r.store.reserve('hrs','A','b',sized,2,legacy,geometry=geometry)
    assert await r.admin.fetchval('SELECT count(*) FROM trading.entry_risk_reservation')==0


@pytest.mark.asyncio
async def test_pending_pressure_deduplicates_live_symbol_and_concurrency_refines(runtime):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    await r.admin.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state,pressure_risk,updated_at) VALUES('held','test','hrs',1,'WLDUSDT',28,28,10,10,28,'submitting',1,$1)",r.at-timedelta(seconds=1))
    stores=[RiskPolicyStore(SimpleNamespace(pool=r.pool),'test') for _ in range(3)]
    captured=[await snapshot() for _ in stores]
    async def fixed(i):return captured[i]
    results=await asyncio.gather(*(s.reserve('hrs',sym,sym,sized,2,lambda i=i:fixed(i),geometry=geometry) for i,(s,sym) in enumerate(zip(stores,['A','B','C']))),return_exceptions=True)
    assert sum(isinstance(x,str) for x in results)==2, [str(x) for x in results]
    for store,result in zip(stores,results):
        if isinstance(result,Exception):continue
        audit=store.reservation_audit
        context=audit['budget_context']
        assert D(context['open_risk_by_symbol']['WLDUSDT'])==D(context['local_risk_by_symbol']['WLDUSDT'])
        assert audit['risk_decision'].quantity<=sized.quantity
        assert D(context['existing_open_risk'])==sum(map(D,context['open_risk_by_symbol'].values()))


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['quantity','risk','inputs','version','unknown_pressure','margin','audit_extra','audit_nonboolean'])
async def test_tampered_or_unsafe_floor_is_denied_without_success_audit(runtime,change):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    if change=='quantity':sized=replace(sized,quantity=sized.quantity+D('.1'))
    if change=='risk':sized=replace(sized,risk=D('.01'))
    if change=='inputs':geometry['hrs_budget_inputs']=dict(geometry['hrs_budget_inputs'],single_risk_ratio='.02')
    if change=='version':geometry['hrs_risk_budget_version']='unknown'
    if change=='audit_extra':geometry['hrs_budget_decision']['undeclared_limit']='100'
    if change=='audit_nonboolean':geometry['hrs_budget_decision']['allowed']=1
    if change=='unknown_pressure':
        await r.admin.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state) VALUES('unknown','test','hrs',1,'B',1,1,100,10,10,'reserved')")
    if change=='margin':
        original=snapshot
        async def snapshot():return await original(margin=D("999.5"))
    with pytest.raises(EntryDenied):await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    assert r.store.reservation_audit is None
    assert await r.admin.fetchval("SELECT count(*) FROM trading.entry_risk_reservation WHERE symbol='A'")==0


@pytest.mark.asyncio
async def test_floor_final_authorization_binds_current_durable_geometry(runtime,monkeypatch):
    import shared.entry_risk_policy as p
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    obj=SimpleNamespace(db=SimpleNamespace(pool=r.pool))
    assert await p.entry_allowed(obj,'hrs',risk_decision=decision,quantity=decision.quantity)
    assert not await p.entry_allowed(obj,'hrs',risk_decision=replace(decision,quantity=decision.quantity+D('.1')),quantity=decision.quantity+D('.1'))
    await r.admin.execute("UPDATE trading.entry_risk_reservation SET geometry=jsonb_set(geometry,'{hrs_budget_context}','{}'::jsonb) WHERE client_order_id=$1",client)
    assert not await p.entry_allowed(obj,'hrs',risk_decision=decision,quantity=decision.quantity)


@pytest.mark.asyncio
async def test_floor_commit_failure_has_no_success_audit(runtime):
    import asyncpg
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    await r.admin.execute("""CREATE FUNCTION trading.reject_floor_commit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'floor deferred commit failure'; END $$;
        CREATE CONSTRAINT TRIGGER reject_floor_commit AFTER INSERT ON trading.entry_risk_reservation
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trading.reject_floor_commit()""")
    with pytest.raises(asyncpg.RaiseError,match='floor deferred commit failure'):
        await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    assert r.store.reservation_audit is None
    assert await r.admin.fetchval('SELECT count(*) FROM trading.entry_risk_reservation')==0


@pytest.mark.asyncio
async def test_floor_policy_revision_change_rejects_preplan(runtime):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    with pytest.raises(EntryDenied,match='policy changed'):
        await r.store.reserve('hrs','A','a',sized,1,snapshot,geometry=geometry)
    assert r.store.reservation_audit is None


@pytest.mark.asyncio
@pytest.mark.parametrize('unsafe',['zero_pressure','zero_margin','weaker_pressure','no_protection'])
async def test_floor_requires_known_risk_pressure_protection_and_physical_margin(runtime,unsafe):
    r=runtime;snapshot=await enable(r);decision=plan()
    if unsafe=='weaker_pressure':
        from strategies.hrs.risk_budget import assess_budget_inputs
        decision=assess_budget_inputs(dict(decision.budget_inputs,pressure_slippage=D(0),fee_rate=D(0)))
    sized,geometry=proposal(decision)
    if unsafe=='zero_pressure':
        await r.admin.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state,pressure_risk) VALUES('zero','test','hrs',2,'B',1,1,100,10,10,'reserved',0)")
    if unsafe=='zero_margin':
        original=snapshot
        async def snapshot():return await original(margin=1000)
    if unsafe=='no_protection':
        original=snapshot
        async def snapshot():
            base=await original()
            base.slot_evidence['local']['WLDUSDT']['algo_ids']={}
            return base
    with pytest.raises(EntryDenied) as caught:await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    if unsafe=='zero_margin':assert caught.value.reason_code=='minimum_order_exceeds_margin'
    assert r.store.reservation_audit is None


@pytest.mark.asyncio
async def test_locked_pending_risk_shrinks_candidate_to_remaining_portfolio_budget(runtime):
    r=runtime;snapshot=await enable(r);initial=plan();sized,geometry=proposal(initial)
    await r.admin.execute("INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state,pressure_risk) VALUES('pending','test','hrs',2,'B',1,1,100,10,10,'reserved',28)")
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    audit=r.store.reservation_audit
    refined=audit['risk_decision']
    assert refined.quantity<initial.quantity
    assert D(audit['budget_context']['existing_open_risk'])==D(28)+D(audit['budget_context']['local_risk_by_symbol']['WLDUSDT'])
    assert refined.quantity==refined.minimum_quantity
    assert refined.worst_loss<=refined.risk_limit
    assert await r.admin.fetchval('SELECT quantity FROM trading.entry_risk_reservation WHERE client_order_id=$1',client)==refined.quantity


@pytest.mark.asyncio
async def test_allocation_change_after_snapshot_is_rechecked_under_account_lock(runtime):
    r=runtime;snapshot=await enable(r);initial=plan();sized,geometry=proposal(initial)
    earlier=await snapshot()
    tx=r.admin.transaction();await tx.start()
    await r.admin.execute("SELECT pg_advisory_xact_lock(hashtextextended('test',7183))")
    started=asyncio.Event()
    async def fixed():started.set();return earlier
    task=asyncio.create_task(r.store.reserve('hrs','A','a',sized,2,fixed,geometry=geometry))
    await started.wait()
    await r.admin.execute("UPDATE public.capital_allocation SET entries='[{\"strategy_id\":\"hrs\",\"allocated_amount\":120}]'")
    await tx.commit()
    await task
    assert r.store.reservation_audit['risk_decision'].budget_inputs['allocation']==D(120)
    assert r.store.reservation_audit['risk_decision'].quantity<initial.quantity


@pytest.mark.asyncio
@pytest.mark.parametrize('column',['original_quantity','original_pressure_risk'])
async def test_floor_final_authorization_rejects_tampered_original_intent(runtime,monkeypatch,column):
    from shared.entry_risk_policy import entry_allowed
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    await r.admin.execute(f'UPDATE trading.entry_risk_reservation SET {column}={column}+1 WHERE client_order_id=$1',client)
    assert not await entry_allowed(SimpleNamespace(db=SimpleNamespace(pool=r.pool)),'hrs',risk_decision=decision,quantity=decision.quantity)
