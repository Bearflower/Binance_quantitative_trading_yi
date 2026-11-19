from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from strategies.hrs.risk_budget import FLOOR, STRICT, assess_entry
from strategies.hrs.strategy import HRSStrategy
from tests.test_strategies.test_hrs_admission_wiring import prepared


def floor_decision(allocation='1000', margin='500'):
    return assess_entry(allocation=D(allocation),entry_price=D(100),stop_price=D(99),direction='long',existing_open_risk=D(0),leverage=D(10),remaining_margin=D(margin),qty_step=D('.1'),min_qty=D('.1'),min_notional=D(5),fee_rate=D('.0005'),pressure_slippage=D('.02'),take_profit_prices=(D(105),D(110)),budget_version=FLOOR)


@pytest.mark.asyncio
async def test_live_policy_json_enables_floor_and_unknown_contract_denies(monkeypatch):
    import shared.entry_risk_policy as p
    obj=object.__new__(HRSStrategy)
    obj.config={}
    obj.db=object()
    obj.capital_mgr=SimpleNamespace(get_effective_margin_limit=AsyncMock(return_value=(D(2),None)))
    obj.binance_client=SimpleNamespace(get_symbol_info=AsyncMock(return_value=dict(stepSize='.1',minQty='.1',minNotional='5')))
    obj.position_manager=SimpleNamespace(get_all_positions=lambda:{})
    obj.trading_executor=SimpleNamespace(leverage=10)
    policy=dict(revision=1,mode='reduced',allow_add=False,multiplier='.5',ratio_cap='.15',entry_ceiling=100,reason='{"hrs_risk_budget_version":"'+FLOOR+'"}')
    monkeypatch.setattr(p,'RiskPolicyStore',lambda db:SimpleNamespace(policy=AsyncMock(return_value=policy)))
    kwargs=dict(symbol='A',direction='long',entry_price=100,stop_price=99,occupied_margin=0,take_profit_prices=(105,110),account_equity=1000)
    decision=await obj._size_entry_with_hrs_budget(**kwargs)
    assert decision.allowed and decision.budget_version==FLOOR
    assert decision.worst_loss>decision.base_single_limit
    policy['reason']='legacy'
    assert not (await obj._size_entry_with_hrs_budget(**kwargs)).allowed
    policy['reason']='{"hrs_risk_budget_version":"unknown"}'
    assert not (await obj._size_entry_with_hrs_budget(**kwargs)).allowed


@pytest.mark.asyncio
async def test_strategy_uses_committed_refined_decision(monkeypatch):
    import shared.entry_risk_policy as p
    obj,_,seen,stages,_,_=prepared(monkeypatch)
    decision=replace(floor_decision(),policy_revision=6,effective_allocation=D(1000))
    refined=replace(floor_decision('10','1'),policy_revision=6,effective_allocation=D(1000))
    geometry={}
    async def reserve(*args,**kwargs):
        geometry.update(kwargs['geometry'])
        return 'intent'
    store=SimpleNamespace(reconcile=AsyncMock(),reserve=reserve,reservation_audit=dict(client_order_id='intent',risk_decision=refined,budget_context={'checked_at':'clock'}))
    monkeypatch.setattr(p,'RiskPolicyStore',lambda db:store)
    result=await obj._reserve_hrs_entry({'symbol':'SUIUSDT','signal_id':1},decision,100,10000)
    assert result.quantity==refined.quantity<decision.quantity
    assert geometry['hrs_budget_inputs']==decision.budget_inputs
    assert geometry['hrs_risk_budget_version']==FLOOR


@pytest.mark.asyncio
async def test_final_authorization_rejects_forged_floor_and_policy_mismatch(monkeypatch):
    import shared.entry_risk_policy as p
    policy=dict(revision=1,mode='reduced',allow_add=False,reason='legacy')
    monkeypatch.setattr(p,'RiskPolicyStore',lambda db:SimpleNamespace(policy=AsyncMock(return_value=policy)))
    obj=SimpleNamespace(db=object())
    decision=replace(floor_decision(),policy_revision=1,client_order_id='risk_valid')
    assert not await p.entry_allowed(obj,'hrs',risk_decision=decision,quantity=decision.quantity)
    policy['reason']='{"hrs_risk_budget_version":"'+FLOOR+'"}'
    forged=replace(decision,worst_loss=D('.01'))
    assert not await p.entry_allowed(obj,'hrs',risk_decision=forged,quantity=forged.quantity)


@pytest.mark.asyncio
async def test_floor_strategy_requires_committed_refinement(monkeypatch):
    import shared.entry_risk_policy as p
    obj,_,_,_,_,_=prepared(monkeypatch)
    decision=replace(floor_decision(),policy_revision=6,effective_allocation=D(1000))
    store=SimpleNamespace(reconcile=AsyncMock(),reserve=AsyncMock(return_value='intent'),reservation_audit={'client_order_id':'intent'})
    monkeypatch.setattr(p,'RiskPolicyStore',lambda db:store)
    assert await obj._reserve_hrs_entry({'symbol':'SUIUSDT','signal_id':1},decision,100,10000) is None
