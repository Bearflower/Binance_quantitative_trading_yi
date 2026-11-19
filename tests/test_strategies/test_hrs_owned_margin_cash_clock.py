"""Cash freshness starts with its own actual GET, independently of positions."""
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import pytest
from shared.entry_risk_policy import EntryDenied,_validate_hrs_financial_receipt
from strategies.hrs.decision_capture import canonical_payload
from strategies.hrs.margin_budget import HRS_OWNED
from strategies.hrs.risk_budget import FLOOR

T=datetime(2026,11,19,9,tzinfo=timezone.utc)


def receipt(cash_clock):
    observed=T-timedelta(seconds=1)
    payload=dict(stage='reservation_inventory',account_id='test',signal_id='1',
        slot_scope='hrs_owned_and_unreleased_reservations_v2',slot_inputs=dict(observed_at=observed.isoformat(),inventory=[]),
        exchange_positions=[],observed_at=observed.isoformat(),account_equity='500',effective_allocation='26.18',
        hrs_margin_scope=HRS_OWNED,account_available_balance='448.86',
        account_available_balance_source='papi/v1/account.totalAvailableBalance',account_status='NORMAL')
    if cash_clock is not None:payload['account_cash_observed_at']=cash_clock
    identity=dict(evidence_id='evidence',run_id='run',payload_sha256=canonical_payload(payload)[1],
        event_time=observed.isoformat(),available_at=observed.isoformat())
    row=dict(evidence_id='evidence',run_id='run',payload_sha256=identity['payload_sha256'],
        event_time=observed,available_at=observed,recorded_at=T,kind='entry_stage_inputs',symbol='A',payload=payload)
    return identity,row


def validate(identity,row,checked_at=T,scope=HRS_OWNED):
    return _validate_hrs_financial_receipt(identity,row,account='test',symbol='A',signal_id='1',checked_at=checked_at,margin_scope=scope)


@pytest.mark.parametrize('clock',[None,(T-timedelta(seconds=122)).isoformat(),
    (T-timedelta(seconds=.5)).isoformat(),T.replace(tzinfo=None).isoformat(),False,'invalid'])
def test_fresh_position_receipt_cannot_refresh_missing_stale_future_or_invalid_cash_clock(clock):
    identity,row=receipt(clock)
    with pytest.raises(EntryDenied,match='financial receipt'):validate(identity,row)


def test_final_receipt_authorization_rechecks_cash_age_while_positions_stay_fresh():
    identity,row=receipt((T-timedelta(seconds=119)).isoformat())
    assert validate(identity,row)
    with pytest.raises(EntryDenied):validate(identity,row,checked_at=T+timedelta(seconds=2))


def test_exact_120_second_cash_boundary_is_allowed():
    assert validate(*receipt((T-timedelta(seconds=120)).isoformat()))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['slow_get','transport'])
async def test_strategy_captures_start_of_account_get_and_denies_delayed_or_failed_get(monkeypatch,failure):
    import shared.entry_risk_policy as p
    import strategies.hrs.strategy as module
    from tests.test_strategies.test_hrs_admission_wiring import prepared
    from tests.test_strategies.test_hrs_min_order_live import floor_decision
    obj,_,seen,_,_,inventory=prepared(monkeypatch)
    now=[T-timedelta(seconds=122)]
    class Clock:
        @staticmethod
        def now(tz):return now[0]
    monkeypatch.setattr(module,'datetime',Clock)
    async def account():
        if failure=='transport':raise RuntimeError('actual account GET failed')
        return dict(totalMarginBalance='500',availableBalance='448.86',accountStatus='NORMAL')
    async def positions():now[0]=T;return inventory
    obj.binance_client.get_account_info=AsyncMock(side_effect=account)
    obj.binance_client.get_position=AsyncMock(side_effect=positions)
    obj.binance_client.use_unified_account=True
    writer=AsyncMock(return_value='receipt')
    obj.decision_recorder=SimpleNamespace(failed=False,run_id='r',record=writer)
    decision=replace(floor_decision(),policy_revision=6,effective_allocation=D('26.18'))
    async def reserve(*args,**kwargs):seen.append(await args[5]());return 'intent'
    store=SimpleNamespace(account='test',policy=AsyncMock(return_value=dict(reason=json.dumps(dict(hrs_risk_budget_version=FLOOR,hrs_margin_scope=HRS_OWNED)))),
        reconcile=AsyncMock(),reserve=reserve,reservation_audit=dict(client_order_id='intent',risk_decision=decision))
    monkeypatch.setattr(p,'RiskPolicyStore',lambda db:store)
    assert await obj._reserve_hrs_entry({'symbol':'A','signal_id':1},decision,100,10000) is None
    writer.assert_not_awaited()
    assert not seen


def test_legacy_financial_receipt_retains_no_cash_clock_requirement():
    from strategies.hrs.margin_budget import ALL_ACCOUNT
    identity,row=receipt(None)
    row['payload'].pop('hrs_margin_scope')
    row['payload_sha256']=identity['payload_sha256']=canonical_payload(row['payload'])[1]
    assert validate(identity,row,scope=ALL_ACCOUNT)


@pytest.mark.asyncio
async def test_delayed_prospective_capture_cannot_refresh_cash_start_clock(monkeypatch):
    import strategies.hrs.decision_context as module
    now=[T-timedelta(seconds=122)]
    class Clock:
        @staticmethod
        def now(tz):return now[0]
    async def account():return dict(totalMarginBalance='500',availableBalance='448.86',accountStatus='NORMAL')
    async def positions():now[0]=T;return []
    async def database(db,account_id):
        return [[dict(reason=json.dumps(dict(hrs_risk_budget_version=FLOOR,hrs_margin_scope=HRS_OWNED)))],[],[],[],[],[],[],[],[]],True
    monkeypatch.setattr(module,'datetime',Clock);monkeypatch.setattr(module,'_database_context',database)
    obj=SimpleNamespace(binance_client=SimpleNamespace(use_unified_account=True,get_account_info=account,
        get_position=positions,get_um_account_positions=positions),position_manager=SimpleNamespace(get_all_positions=lambda:{}),
        capital_mgr=SimpleNamespace(get_effective_margin_limit=AsyncMock(return_value=(52,'db'))),
        trading_executor=SimpleNamespace(timeout_recovery=SimpleNamespace(pending=AsyncMock(return_value=[]))),db=object())
    with pytest.raises(ValueError,match='cash_stale'):await module.capture_financial_context(obj)
