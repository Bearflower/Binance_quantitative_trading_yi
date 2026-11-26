from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
import pytest

from strategies.hrs.risk_budget import RiskDecision
from strategies.hrs.strategy import HRSStrategy


def prepared(monkeypatch,denial=None):
    import shared.entry_risk_policy as policy
    import strategies.hrs.strategy as module
    inventory=[dict(symbol=s,positionAmt=q,markPrice='10',entryPrice='10',leverage='2')
               for s,q in [('WLDUSDT','28'),('BNBUSDT','.04'),('SOLUSDT','-.27')]]
    local={'WLDUSDT':dict(position_id='p',direction='long',remaining_quantity='28')}
    obj=object.__new__(HRSStrategy)
    obj.config={'trading':{'max_positions':3}}
    obj.db=SimpleNamespace(fetch_all=AsyncMock(return_value=[]))
    obj.position_manager=SimpleNamespace(get_all_positions=lambda:local)
    obj.binance_client=SimpleNamespace(get_position=AsyncMock(return_value=inventory),get_um_account_positions=AsyncMock(return_value=inventory))
    obj.trading_executor=SimpleNamespace(leverage=2)
    seen=[]
    async def reserve(strategy,symbol,identity,sized,revision,snapshot,**kwargs):
        seen.append(await snapshot())
        if denial:raise denial
        return 'intent'
    store=SimpleNamespace(reconcile=AsyncMock(),reserve=reserve,reservation_audit={'client_order_id':'intent','slot_occupancy_proof':{'equity_evidence_sha256':'a'*64},'slot_context':{'evidence_sha256':'a'*64,'checked_at':'2026-11-18T10:00:00+00:00'}})
    monkeypatch.setattr(policy,'RiskPolicyStore',lambda db:store)
    monkeypatch.setattr(module,'try_claim_symbol',AsyncMock(return_value={'claimed':True,'claim_id':1}))
    monkeypatch.setattr(module,'release_claim',AsyncMock())
    stages=AsyncMock();monkeypatch.setattr(module,'record_stage',stages)
    logger=Mock();monkeypatch.setattr(module,'logger',logger)
    decision=RiskDecision(True,'ok',quantity=D(1),worst_loss=D('.5'),exit_ratios=(D(1),D(0)),policy_revision=6,effective_allocation=D(1000))
    return obj,decision,seen,stages,logger,inventory


@pytest.mark.asyncio
async def test_live_snapshot_separates_hrs_slots_from_whole_account_margin(monkeypatch):
    from shared.entry_risk_policy import ScopedRiskSnapshot
    from strategies.hrs.entry_occupancy import SLOT_SCOPE
    obj,decision,seen,stages,logger,inventory=prepared(monkeypatch)
    result=await obj._reserve_hrs_entry({'symbol':'SUIUSDT','signal_id':1},decision,10,1000)
    assert result is not None and result.client_order_id=='intent'
    assert len(seen)==1 and isinstance(seen[0],ScopedRiskSnapshot)
    snapshot=seen[0]
    assert snapshot.slot_scope==SLOT_SCOPE and snapshot.max_slots==3
    assert snapshot.account_margin==D('141.55')
    assert snapshot.slot_evidence['inventory']==snapshot.slot_evidence['independent']==inventory
    assert list(snapshot.slot_evidence['local'])==['WLDUSDT']
    obj.binance_client.get_um_account_positions.assert_awaited_once()
    captured=[c.args[2] for c in stages.await_args_list if c.args[2].get('stage')=='reservation_inventory']
    assert captured[0]['slot_scope']==SLOT_SCOPE and captured[0]['slot_inputs']==snapshot.slot_evidence
    success=next(c.args[2] for c in stages.await_args_list if c.args[2].get('stage')=='reservation_success')
    assert success['client_order_id']=='intent'
    assert success['slot_context']['evidence_sha256']==success['slot_occupancy_proof']['equity_evidence_sha256']


@pytest.mark.asyncio
async def test_expected_risk_denial_is_a_business_event_not_an_error(monkeypatch):
    from shared.entry_risk_policy import EntryDenied
    obj,decision,seen,stages,logger,_=prepared(monkeypatch,EntryDenied('concurrent position slots include pending reservations',reason_code='entry_position_slots',context={'max_slots':3,'requested_slots':4}))
    signal={'symbol':'SUIUSDT','signal_id':1}
    assert await obj._reserve_hrs_entry(signal,decision,10,1000) is None
    events=[c for c in logger.info.call_args_list if c.args[0]=='HRS 风控拒绝开仓']
    assert events,'expected risk denial must have its own business event'
    event=events[0]
    assert event.kwargs['reason_code']=='entry_position_slots'
    assert event.kwargs['risk_context']['requested_slots']==4 and 'error' not in event.kwargs
    assert signal['_audit_denial']=='entry_position_slots'
    logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_reservation_exception_remains_visible(monkeypatch):
    obj,decision,seen,stages,logger,_=prepared(monkeypatch,RuntimeError('database unavailable'))
    assert await obj._reserve_hrs_entry({'symbol':'SUIUSDT','signal_id':1},decision,10,1000) is None
    assert any(c.kwargs.get('error')=='database unavailable' for c in logger.warning.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('limit',[True,'3',4,0])
async def test_live_wiring_does_not_coerce_invalid_typed_slot_limit(monkeypatch,limit):
    obj,decision,seen,stages,logger,inventory=prepared(monkeypatch)
    obj.config['trading']['max_positions']=limit
    await obj._reserve_hrs_entry({'symbol':'SUIUSDT','signal_id':1},decision,10,1000)
    assert seen[0].max_slots is limit
