from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D

import pytest

from strategies.hrs.equity_reconciliation import ReconciliationSource,rebuild_history
from strategies.hrs.equity_evidence import EvidenceBlocked
from strategies.hrs.risk_ledger import LedgerObservation,IncomeEvent,CashFlow,initialize_ledger,reconcile_ledger
from strategies.hrs.loss_gate import GateHistory,evaluate_loss_gate
from strategies.hrs.loss_gate_repository import StoredRiskState

T=datetime(2026,11,16,16,tzinfo=timezone.utc)


def observation(at,incomes=(),flows=()):
    return LedgerObservation(at,'seg',incomes,flows,(),at,at,True,True)


def prior():
    start=initialize_ledger('seg',D('1000'),observation(T))
    state=reconcile_ledger(start,observation(T+timedelta(seconds=30)))
    gate=evaluate_loss_gate(None,state.checkpoint,GateHistory((start.checkpoint,state.checkpoint),T,state.as_of,True),allow_inception_segment=True)
    return start,StoredRiskState(2,state,gate.state)


def source(start,old,event,*,as_of=None,flows=None):
    at=as_of or T+timedelta(seconds=60)
    return ReconciliationSource(account='test',segment_id='seg',inception=T,opening_cash=D('1000'),
        current=observation(at,(event,)),cash_flows=flows or old.ledger.cash_flows,
        checkpoints=(start.checkpoint,old.ledger.checkpoint),captures=(),fills=(),orders=(),
        available_at=at+timedelta(seconds=1),source_sha256='a'*64)


def test_late_income_rebuilds_flat_history_with_actual_availability_and_latches():
    start,old=prior()
    event=IncomeEvent('REALIZED_PNL:1','realized_pnl',D('-30'),T+timedelta(seconds=10),'seg',True)
    rebuilt=rebuild_history(old,source(start,old,event),allow_inception_segment=True)
    assert rebuilt.ledger.nav==D('.97')
    assert rebuilt.gate.state.daily_latched
    assert rebuilt.gate.allow_new_risk is False
    corrected={cp.as_of:cp for cp in rebuilt.checkpoints}
    assert corrected[old.ledger.as_of].nav==D('.97')
    assert old.ledger.nav==D('1')
    assert rebuilt.receipt['available_at']==T+timedelta(seconds=61)
    assert rebuilt.receipt['new_income_ids']==['REALIZED_PNL:1']


def test_late_income_reprices_existing_flow_units_without_changing_cash_source():
    start,old=prior()
    flow=CashFlow('allocation:journal:1',D('1000'),T+timedelta(seconds=20),'seg',D('1'),'original-proof')
    old_ledger=reconcile_ledger(start,observation(old.ledger.as_of,flows=(flow,)))
    old=replace(old,ledger=old_ledger)
    event=IncomeEvent('REALIZED_PNL:1','realized_pnl',D('-100'),T+timedelta(seconds=10),'seg',True)
    rebuilt=rebuild_history(old,source(start,old,event),allow_inception_segment=True)
    assert rebuilt.ledger.cash_flows[0].amount==D('1000')
    assert rebuilt.ledger.cash_flows[0].nav_before==D('.9')
    assert rebuilt.ledger.nav==D('.9')
    assert rebuilt.receipt['flow_valuations'][0]['old_nav_before']=='1'


def test_reconciliation_preserves_same_day_latches_but_valid_rollover_can_reset_daily():
    start,old=prior()
    old=replace(old,gate=replace(old.gate,daily_latched=True,rolling_latched=True,rolling_latch_scope='inception_segment'))
    event=IncomeEvent('REALIZED_PNL:1','realized_pnl',D('10'),T+timedelta(seconds=10),'seg',True)
    same=rebuild_history(old,source(start,old,event),allow_inception_segment=True)
    assert same.gate.state.daily_latched and same.gate.state.rolling_latched
    tomorrow=rebuild_history(old,source(start,old,event,as_of=T+timedelta(days=1,seconds=30)),allow_inception_segment=True)
    assert not tomorrow.gate.state.daily_latched
    assert tomorrow.gate.state.rolling_latched


def test_missing_historical_nonflat_mark_blocks_without_using_current_mark():
    start,old=prior()
    event=IncomeEvent('COMMISSION:1','fee',D('-1'),T+timedelta(seconds=10),'seg',True)
    filled=dict(account_id='test',symbol='BTCUSDT',trade_id=1,order_id=1,side='BUY',qty=D('1'),price=D('100'),trade_time_utc=T+timedelta(seconds=5))
    owner=dict(account_id='test',symbol='BTCUSDT',order_id='1',strategy='hrs')
    data=replace(source(start,old,event),fills=(filled,),orders=(owner,))
    with pytest.raises(EvidenceBlocked,match='historical_mark'):
        rebuild_history(old,data,allow_inception_segment=True)


def test_existing_position_anchor_reconciliation_keeps_original_marked_unit_base():
    from strategies.hrs.risk_ledger import PositionMark
    opening_mark=PositionMark('p','seg',D('1'),D('100'),D('99'),T,True)
    base=initialize_ledger('seg',D('1000'),replace(observation(T),positions=(opening_mark,)))
    old_mark=replace(opening_mark,marked_at=T+timedelta(seconds=30))
    old_ledger=reconcile_ledger(base,replace(observation(T+timedelta(seconds=30)),positions=(old_mark,)))
    old_gate=evaluate_loss_gate(None,old_ledger.checkpoint,GateHistory((base.checkpoint,old_ledger.checkpoint),T,old_ledger.as_of,True),allow_inception_segment=True).state
    old=StoredRiskState(2,old_ledger,old_gate)
    event=IncomeEvent('FUNDING_FEE:1','funding',D('-1'),T+timedelta(seconds=10),'seg',True)
    current=replace(observation(T+timedelta(seconds=60),(event,)),positions=(replace(opening_mark,marked_at=T+timedelta(seconds=60)),))
    inventory=[dict(symbol='BTCUSDT',positionAmt='1',entryPrice='100')]
    capture=dict(inventory=inventory,independent=inventory,local={'BTCUSDT':dict(direction='long',remaining_quantity='1',position_id='p')},
                 marks={'BTCUSDT':dict(symbol='BTCUSDT',markPrice='99',time=int(old_ledger.as_of.timestamp()*1000))})
    data=ReconciliationSource('test','seg',T,D('1000'),current,(),(base.checkpoint,old_ledger.checkpoint),
        (dict(as_of=old_ledger.as_of,capture=capture,evidence_sha256='b'*64),),(),(),current.as_of,'a'*64,
        opening_positions=(opening_mark,),opening_inventory=(dict(strategy='hrs',symbol='BTCUSDT',quantity='1',entry_price='100'),))
    rebuilt=rebuild_history(old,data,allow_inception_segment=True)
    assert rebuilt.ledger.units==D('999')
    assert rebuilt.ledger.nav==D('998')/D('999')


def test_opening_credit_revision_preserves_latches_and_rejects_fact_replacement():
    start,old=prior()
    old=replace(old,gate=replace(old.gate,daily_latched=True,rolling_latched=True,rolling_latch_scope='inception_segment'))
    credit=dict(event_id='COMMISSION:old',kind='fee',amount='-2',occurred_at=(T-timedelta(seconds=10)).isoformat())
    revision=dict(old_credits={},new_credits={credit['event_id']:credit},old_opening_cash='1000',new_opening_cash='998')
    data=ReconciliationSource('test','seg',T,D('998'),observation(T+timedelta(seconds=60)),(),
        (start.checkpoint,old.ledger.checkpoint),(),(),(),T+timedelta(seconds=61),'a'*64,opening_revision=revision)
    rebuilt=rebuild_history(old,data,allow_inception_segment=True)
    assert rebuilt.ledger.units==D('998') and rebuilt.ledger.nav==D('1')
    assert rebuilt.gate.state.daily_latched and rebuilt.gate.state.rolling_latched
    assert rebuilt.receipt['opening_anchor_revision']==revision
    conflict=dict(revision,old_credits={credit['event_id']:dict(credit,amount='-1')})
    with pytest.raises(EvidenceBlocked,match='opening_credit_conflict'):
        rebuild_history(old,replace(data,opening_revision=conflict),allow_inception_segment=True)
