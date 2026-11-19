"""Owned quota + real cash through isolated PG reservation and final HTTP hook."""
import asyncio,copy,json
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
import pytest
from shared.entry_risk_policy import EntryDenied, RiskPolicyStore, entry_allowed
from strategies.hrs.equity_evidence import canonical,certify_owned_inventory_paths
from strategies.hrs.margin_budget import HRS_OWNED
from strategies.hrs.risk_budget import FLOOR
from tests.test_risk.test_hrs_scoped_slots import runtime, pytestmark
from tests.test_risk.test_hrs_min_order_lock import enable,attach_financial_receipt,plan,proposal


async def enable_owned(r, **fields):
    await enable(r)
    await r.admin.execute("""INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason)
        SELECT account_id,strategy_id,3,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,26.18,$1 FROM trading.strategy_risk_policy WHERE revision=2""",json.dumps(dict(hrs_risk_budget_version=FLOOR,hrs_margin_scope=HRS_OWNED)))
    await r.admin.execute("UPDATE trading.strategy_risk_active SET revision=3")
    await r.admin.execute("""INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason)
        SELECT account_id,'new_coin',1,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,'fixture foreign' FROM trading.strategy_risk_policy WHERE strategy_id='hrs' AND revision=3""")
    await r.admin.execute("UPDATE public.capital_allocation SET entries='[{\"strategy_id\":\"hrs\",\"allocated_amount\":60}]'")
    r.inventory[0]['positionAmt']='3.69';r.inventory[1]['markPrice']='16147.5'
    r.payload['fills'][0]['qty']='3.69'
    r.payload['capture']['local']['WLDUSDT']['remaining_quantity']='3.69'
    r.payload['inventory_owner_path_proofs']=certify_owned_inventory_paths('test',
        {row['symbol']:D(row['positionAmt']) for row in r.inventory},r.payload['fills'],r.payload['order_owners'],coverage_complete=True)
    encoded,digest=canonical(r.payload)
    await r.admin.execute('INSERT INTO trading.hrs_equity_evidence VALUES($1,$2,$3,$4,$4,$5::jsonb)','test',digest,'seg',r.at,encoded)
    await r.admin.execute('UPDATE trading.strategy_equity_gate SET evidence_sha256=$1,policy_revision=3',digest)
    # This is the actual economic distinction: released entry claim, retained
    # foreign position, and its complete unique signed fill path still exist.
    await r.admin.execute("UPDATE trading.position_claims SET claim_state='RELEASED',expires_at=clock_timestamp()-interval '1 second' WHERE strategy<>'hrs'")
    async def snapshot(symbol='A',signal_id='a'):
        base=replace(await r.snapshot(margin=D('68.55')),equity=D(500),allocation=D('26.18'))
        return await attach_financial_receipt(r,base,symbol=symbol,signal_id=signal_id,
            hrs_margin_scope=HRS_OWNED,account_available_balance=fields.get('cash','448.86'),
            account_available_balance_source=fields.get('source','papi/v1/account.totalAvailableBalance'),
            account_status=fields.get('status','NORMAL'))
    return snapshot


@pytest.mark.asyncio
async def test_foreign_margin_exceeding_quota_allows_minimum_and_nested_decision_reaches_http(runtime,monkeypatch):
    from shared.binance_api import BinanceClient
    from strategies.hrs.executor import TradingExecutor
    r=runtime;snapshot=await enable_owned(r);initial=plan('60');sized,geometry=proposal(initial)
    geometry.update(hrs_budget_decision=initial,hrs_budget_inputs=initial.budget_inputs,hrs_margin_scope=HRS_OWNED)
    client_id=await r.store.reserve('hrs','A','a',sized,3,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client_id)
    context=r.store.reservation_audit['budget_context']
    assert D(context['account_margin'])==D('68.55')>D('26.18')
    assert D(context['hrs_owned_margin'])==D('3.69')
    assert D(context['margin_cap'])==D('26.18')
    assert D(context['remaining_margin'])==D('22.49')
    assert decision.quantity==decision.minimum_quantity
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    assert await entry_allowed(SimpleNamespace(db=SimpleNamespace(pool=r.pool)),'hrs',risk_decision=decision,quantity=decision.quantity)
    client=BinanceClient('test_api_key_12345','test_api_secret_67890')
    client.use_unified_account=False;client._init_session=AsyncMock();client.session=MagicMock()
    client.get_symbol_info=AsyncMock(return_value={'stepSize':'.1','tickSize':D('.01')});client.rate_limiter.acquire=AsyncMock()
    class Boundary(RuntimeError):pass
    client.session.request.side_effect=Boundary('test POST boundary')
    executor=SimpleNamespace(db=SimpleNamespace(pool=r.pool),binance_api=client,order_type_entry='LIMIT')
    with pytest.raises(Boundary):await TradingExecutor._submit_reserved_hrs_entry(executor,'A','BUY',decision.quantity,sized.price,decision)
    assert client.session.request.call_args.args[0]=='POST'


@pytest.mark.asyncio
@pytest.mark.parametrize('fields',[dict(cash=None),dict(cash=False),dict(cash=448.86),dict(cash='NaN'),dict(cash='0'),dict(cash='.99'),dict(status=None),dict(status='MARGIN_CALL'),dict(source='equity')])
async def test_invalid_actual_cash_status_source_is_denied_before_reservation(runtime,fields):
    r=runtime;snapshot=await enable_owned(r,**fields);sized,geometry=proposal(plan('60'))
    geometry['hrs_margin_scope']=HRS_OWNED
    with pytest.raises(EntryDenied):await r.store.reserve('hrs','A','a',sized,3,snapshot,geometry=geometry)
    assert await r.admin.fetchval('SELECT count(*) FROM trading.entry_risk_reservation')==0


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['new_foreign','foreign_margin','foreign_release','cash','status','scope','source','hrs_claim','conflicting_foreign_claim','quota'])
async def test_final_authorization_rechecks_account_pending_provenance_claims_and_quota(runtime,monkeypatch,change):
    r=runtime;snapshot=await enable_owned(r);sized,geometry=proposal(plan('60'));geometry['hrs_margin_scope']=HRS_OWNED
    await foreign_pending(r,'10')
    client=await r.store.reserve('hrs','A','a',sized,3,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test');obj=SimpleNamespace(db=SimpleNamespace(pool=r.pool))
    assert await entry_allowed(obj,'hrs',risk_decision=decision,quantity=decision.quantity)
    if change=='new_foreign':await foreign_pending(r,'438','foreign2')
    elif change=='foreign_margin':await r.admin.execute("UPDATE trading.entry_risk_reservation SET margin=11 WHERE client_order_id='foreign'")
    elif change=='foreign_release':await r.admin.execute("UPDATE trading.entry_risk_reservation SET state='released' WHERE client_order_id='foreign'")
    elif change=='hrs_claim':await r.admin.execute("UPDATE trading.position_claims SET claim_state='RELEASED' WHERE symbol='WLDUSDT'")
    elif change=='conflicting_foreign_claim':await r.admin.execute("INSERT INTO trading.position_claims(symbol,strategy,claim_state,expires_at) VALUES('SOLUSDT','hrs','ACTIVE','infinity')")
    elif change=='quota':await r.admin.execute("UPDATE public.capital_allocation SET entries='[{\"strategy_id\":\"hrs\",\"allocated_amount\":2}]'")
    else:
        field,value={'cash':('account_available_balance','999'),'status':('account_status','NORMAL_CHANGED'),
            'scope':('hrs_margin_scope','all_account_positions_v1'),'source':('account_available_balance_source','equity')}[change]
        await r.admin.execute("UPDATE hrs.hrs_decision_evidence SET payload=jsonb_set(payload,ARRAY[$1],to_jsonb($2::text))",field,value)
    assert not await entry_allowed(obj,'hrs',risk_decision=decision,quantity=decision.quantity)


async def foreign_pending(r,margin,identity='foreign'):
    await r.admin.execute("""INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state)
        VALUES($1,'test','new_coin',1,$1,1,1,100,10,$2,'submitting')""",identity,D(margin))


@pytest.mark.asyncio
async def test_cash_accounts_for_all_strategy_reservations_and_exact_boundary(runtime):
    r=runtime;snapshot=await enable_owned(r,cash='2');sized,geometry=proposal(plan('60'));geometry['hrs_margin_scope']=HRS_OWNED
    await foreign_pending(r,'1.00000001')
    with pytest.raises(EntryDenied):await r.store.reserve('hrs','A','a',sized,3,snapshot,geometry=geometry)
    await r.admin.execute("UPDATE trading.entry_risk_reservation SET margin=1 WHERE client_order_id='foreign'")
    assert await r.store.reserve('hrs','A','a',sized,3,snapshot,geometry=geometry)


@pytest.mark.asyncio
async def test_concurrent_hrs_intents_cannot_spend_same_actual_account_cash(runtime):
    r=runtime;snapshot=await enable_owned(r,cash='1.5');sized,geometry=proposal(plan('60'));geometry['hrs_margin_scope']=HRS_OWNED
    captured=[await snapshot(symbol,identity) for symbol,identity in [('A','a'),('B','b')]]
    stores=[RiskPolicyStore(SimpleNamespace(pool=r.pool),'test') for _ in range(2)]
    async def fixed(index):return captured[index]
    results=await asyncio.gather(*(stores[i].reserve('hrs',symbol,identity,sized,3,
        lambda i=i:fixed(i),geometry=geometry) for i,(symbol,identity) in enumerate([('A','a'),('B','b')])),return_exceptions=True)
    assert sum(isinstance(result,str) for result in results)==1
    denied=next(result for result in results if isinstance(result,EntryDenied))
    assert denied.reason_code=='minimum_order_exceeds_margin'
    assert await r.admin.fetchval("SELECT sum(margin) FROM trading.entry_risk_reservation WHERE state<>'released'")==D(1)


@pytest.mark.asyncio
async def test_hrs_retained_margin_exhausts_own_quota_despite_large_actual_cash(runtime):
    r=runtime;snapshot=await enable_owned(r);sized,geometry=proposal(plan('60'));geometry['hrs_margin_scope']=HRS_OWNED
    await r.admin.execute("""INSERT INTO trading.entry_risk_reservation(client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state,pressure_risk)
        VALUES('held','test','hrs',3,'B',1,1,224.9,10,22.49,'reserved',.1)""")
    with pytest.raises(EntryDenied) as caught:await r.store.reserve('hrs','A','a',sized,3,snapshot,geometry=geometry)
    assert caught.value.reason_code=='minimum_order_exceeds_margin'
    assert D(caught.value.context['hrs_owned_margin'])==D('3.69')
    assert D(caught.value.context['remaining_margin'])==0


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('entry_price','1'),('risk_stop_price','1'),('algo_ids',{'sl':999})])
async def test_new_scope_receipt_cannot_weaken_original_hrs_loss_fields(runtime,field,value):
    from strategies.hrs.entry_occupancy import capture_hrs_slot_inputs
    from datetime import datetime,timezone
    r=runtime;original=await enable_owned(r);sized,geometry=proposal(plan('60'));geometry['hrs_margin_scope']=HRS_OWNED
    async def snapshot():
        base=await original();local=copy.deepcopy(base.slot_evidence['local']);local['WLDUSDT'][field]=value
        inputs=capture_hrs_slot_inputs(inventory=base.slot_evidence['inventory'],independent=base.slot_evidence['independent'],local=local,observed_at=datetime.now(timezone.utc))
        return await attach_financial_receipt(r,replace(base,slot_evidence=inputs),hrs_margin_scope=HRS_OWNED,
            account_available_balance='448.86',account_available_balance_source='papi/v1/account.totalAvailableBalance',account_status='NORMAL')
    with pytest.raises(EntryDenied) as caught:await r.store.reserve('hrs','A','a',sized,3,snapshot,geometry=geometry)
    assert caught.value.reason_code=='hrs_budget_parent_risk_mismatch'
