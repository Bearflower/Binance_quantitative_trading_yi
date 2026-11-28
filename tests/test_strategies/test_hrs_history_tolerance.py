"""History acceptance changes data readiness, never known losses or live evidence."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from strategies.hrs.loss_gate import GateHistory, LossGateState, evaluate_loss_gate
from strategies.hrs.risk_ledger import (EquityCheckpoint, HistoricalIncomeAcceptance,
    LedgerNotReady, LedgerObservation, initialize_ledger, reconcile_ledger)

T = datetime(2026, 11, 24, 16, tzinfo=timezone.utc)  # Beijing midnight
REF = 'a' * 64

def cp(at, nav='1'):
    return EquityCheckpoint('old-segment', at, D(nav)*100, D('100'), D(nav))

def accepted(through=None, current_from=None):
    return HistoricalIncomeAcceptance(REF, T-timedelta(days=2), through or T,
                                     current_from or T)

def test_accepted_history_keeps_incomplete_flag_and_observed_window():
    points=(cp(T-timedelta(days=1), '1.01'), cp(T), cp(T+timedelta(hours=1)))
    history=GateHistory(points, points[0].as_of, points[-1].as_of, False, True,
                        accepted_history=accepted())
    result=evaluate_loss_gate(None, points[-1], history)
    assert result.ready and result.allow_new_risk
    assert result.window_label == 'observed_history_accepted_gaps'
    assert result.qualified_30_day is False and history.complete is False
    assert result.rolling_drawdown == 1-D('1')/D('1.01')

@pytest.mark.parametrize('case',['new_gap','missing_origin','missing_receipt','future_acceptance'])
def test_acceptance_never_hides_current_gaps_or_invents_daily_origin(case):
    points=(cp(T),cp(T+timedelta(hours=1)))
    proof=accepted()
    if case=='new_gap':proof=accepted(current_from=T+timedelta(minutes=1))
    if case=='future_acceptance':proof=accepted(through=T+timedelta(hours=2))
    if case=='missing_receipt':proof=replace(proof,receipt_sha256='')
    if case=='missing_origin':points=points[1:]
    result=evaluate_loss_gate(None,points[-1],GateHistory(points,T,points[-1].as_of,False,True,accepted_history=proof))
    assert not result.allow_new_risk and not result.ready


def test_known_historical_trough_latches_even_after_recovery_and_restart():
    points=(cp(T-timedelta(days=1),'1.1'),cp(T-timedelta(hours=12),'1.02'),cp(T,'1.1'),cp(T+timedelta(hours=1),'1.1'))
    history=GateHistory(points,points[0].as_of,points[-1].as_of,False,True,accepted_history=accepted())
    result=evaluate_loss_gate(None,points[-1],history)
    assert result.ready and not result.allow_new_risk and result.state.rolling_latched
    state=LossGateState.from_dict(result.state.to_dict())
    again=evaluate_loss_gate(state,points[-1],history)
    assert again.state.rolling_latched and not again.allow_new_risk


def test_daily_three_percent_and_prior_latches_stay_effective():
    points=(cp(T),cp(T+timedelta(hours=1),'.97'))
    history=GateHistory(points,T,points[-1].as_of,False,True,accepted_history=accepted())
    result=evaluate_loss_gate(None,points[-1],history)
    assert result.state.daily_latched and not result.allow_new_risk
    prior=LossGateState('old-segment',T.date(),rolling_latched=True,rolling_latch_scope='inception_segment')
    assert not evaluate_loss_gate(prior,points[-1],history).allow_new_risk


def test_observation_acceptance_preserves_cash_units_income_and_original_segment():
    initial=LedgerObservation(T,'old-segment',(),(),(),T,T,True,True)
    old=initialize_ledger('old-segment',D('100'),initial)
    now=T+timedelta(hours=1)
    observation=LedgerObservation(now,old.segment_id,(),(),(),now,now,False,True,
                                  accepted_history=accepted())
    state=reconcile_ledger(old,observation)
    assert (state.segment_id,state.inception,state.cash_allocation,state.units,state.income_total)==(old.segment_id,old.inception,old.cash_allocation,old.units,old.income_total)
    assert observation.income_complete is False
    with pytest.raises(LedgerNotReady):
        reconcile_ledger(old,replace(observation,accepted_history=None))
    with pytest.raises(LedgerNotReady):
        reconcile_ledger(old,replace(observation,accepted_history=accepted(current_from=T+timedelta(minutes=1))))
    with pytest.raises(LedgerNotReady):
        reconcile_ledger(old,replace(observation,positions_complete=False))


def policy_for(stored=None):
    from strategies.hrs.equity_evidence import canonical
    from strategies.hrs.loss_gate_repository import ledger_to_dict
    p=SimpleNamespace(receipt_sha256=REF,revision=1,accepted_from=T-timedelta(days=2),
        accepted_through=T,classic_flat_at=T-timedelta(hours=1),hrs_daily_limit=D('.03'),
        hrs_rolling_limit=D('.06'),payload={})
    p.covers=lambda code,start,end: p.accepted_from<=start<=end<=p.accepted_through
    if stored:
        p.payload=dict(hrs_segment_id=stored.ledger.segment_id,hrs_baseline_state_version=stored.version,
            hrs_baseline_ledger_sha256=canonical(ledger_to_dict(stored.ledger))[1],
            hrs_baseline_gate_sha256=canonical(stored.gate.to_dict())[1])
    return p


def test_resume_policy_pins_exact_original_ledger_gate_and_revision():
    from strategies.hrs.history_tolerance import verify_resume_state
    from strategies.hrs.equity_evidence import EvidenceBlocked
    from strategies.hrs.loss_gate_repository import StoredRiskState
    old=initialize_ledger('old-segment',D('100'),LedgerObservation(T,'old-segment',(),(),(),T,T,True,True))
    stored=StoredRiskState(12,old,LossGateState(old.segment_id,T.date()))
    p=policy_for(stored)
    verify_resume_state(p,stored)
    with pytest.raises(EvidenceBlocked,match='state_changed'):
        verify_resume_state(p,replace(stored,version=13))
    with pytest.raises(EvidenceBlocked,match='state_changed'):
        verify_resume_state(p,replace(stored,gate=replace(stored.gate,rolling_latched=True)))
    continued=replace(stored,version=13,gate=replace(stored.gate,history_acceptance_ref=REF,history_acceptance_revision=1))
    verify_resume_state(p,continued)
    with pytest.raises(EvidenceBlocked,match='revision_mismatch'):
        verify_resume_state(p,replace(continued,gate=replace(continued.gate,history_acceptance_revision=2)))


def test_observed_historical_income_keeps_false_coverage_and_rejects_identity_conflict():
    from strategies.hrs.history_tolerance import observed_historical_incomes
    from strategies.hrs.equity_evidence import EvidenceBlocked
    p=policy_for(); start=p.accepted_from;end=p.classic_flat_at
    event=dict(account_id='default',strategy='hrs',asset='USDT',reason=None,income_type='COMMISSION',tran_id='9',
               amount='-.01',event_time_utc=(start+timedelta(hours=1)).isoformat())
    report=dict(start_ms=int(start.timestamp()*1000),end_ms=int(end.timestamp()*1000),
                account_coverage_complete=False,id_match_and_coverage_complete=False,
                coverage_gaps=['income:COMMISSION'],events=[event],attribution_conflicts=[],unattributed=[],missing_income_fills=[])
    result=observed_historical_incomes(report,'default','old-segment',p)
    assert result[0].amount==D('-.01') and report['account_coverage_complete'] is False
    with pytest.raises(EvidenceBlocked):
        observed_historical_incomes(dict(report,attribution_conflicts=['bad']),'default','old-segment',p)
    with pytest.raises(EvidenceBlocked):
        observed_historical_incomes(dict(report,events=[dict(event,account_id='grid')]),'default','old-segment',p)


def test_late_old_loss_requires_reconciliation_instead_of_charging_today():
    from strategies.hrs.history_tolerance import retained_income_events
    from strategies.hrs.equity_evidence import EvidenceBlocked
    from strategies.hrs.risk_ledger import IncomeEvent
    old=initialize_ledger('old-segment',D('100'),LedgerObservation(T,'old-segment',(),(),(),T,T,True,True))
    old=replace(old,as_of=T+timedelta(hours=2))
    late=IncomeEvent('REALIZED_PNL:99','realized_pnl',D('-50'),T+timedelta(hours=1),old.segment_id,True)
    with pytest.raises(EvidenceBlocked,match='late_income_requires_reconciliation'):
        retained_income_events(old,(),(late,),opening_credits=())
    assert old.cash_allocation==D('100') and old.income_total==0 and old.units==D('100')
