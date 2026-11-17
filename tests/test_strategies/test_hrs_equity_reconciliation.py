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
