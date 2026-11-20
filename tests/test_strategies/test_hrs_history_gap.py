from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from strategies.hrs.equity_evidence import EvidenceBlocked
from strategies.hrs.equity_history_gap import reconstruct_gaps, replay_inception_latches
from strategies.hrs.loss_gate import GateHistory, LossGateState, evaluate_loss_gate
from strategies.hrs.risk_ledger import EquityCheckpoint, IncomeEvent, LedgerState

START = datetime(2026, 11, 19, 16, tzinfo=timezone.utc)
END = START + timedelta(minutes=4)
SEGMENT = 'segment'


def cp(at, nav):
    return EquityCheckpoint(SEGMENT, at, D(nav) * 100, D(100), D(nav))


def inputs():
    bars = [[int((START+timedelta(minutes=i)).timestamp()*1000), '10', '13', '8', close, '0',
             int((START+timedelta(minutes=i+1)).timestamp()*1000)-1]
            for i, close in enumerate(('10', '11', '12', '10'))]
    after = END + timedelta(seconds=5)
    manifest = {'account':'default','segment':SEGMENT,'available_at':after.isoformat(),'gaps':[
        {'left':START.isoformat(),'right':END.isoformat(),'bars':{'WLDUSDT':bars}}]}
    ledger = LedgerState(SEGMENT, START, after, D(100), D(1), D(100), D(101), D('1.01'),
        (IncomeEvent('funding:1','funding',D(1),START+timedelta(minutes=1,seconds=1),SEGMENT,True),))
    opening = [dict(symbol='WLDUSDT',strategy='hrs',quantity='2',entry_price='10')]
    return manifest, ledger, (cp(START,'1'),cp(END,'1.01')), opening, after


def test_occupied_gap_restores_market_closes_and_applies_funding_exactly_once():
    manifest, ledger, points, opening, after = inputs()
    restored, marks = reconstruct_gaps(manifest,'default',ledger,points,(),(),opening,after)
    assert [x.nav for x in restored]==[D('1'),D('1.03'),D('1.05')]
    assert [x.as_of for x in restored]==[START+timedelta(minutes=i) for i in (1,2,3)]
    assert len(marks)==3 and all(x['source']=='/fapi/v1/markPriceKlines' for x in marks.values())
    assert ledger.income_total==D(1)


@pytest.mark.parametrize('mutation', ['missing', 'wrong_time', 'wrong_owner', 'cash', 'future','wrong_close'])
def test_gap_proof_rejects_gaps_wrong_id_or_unproved_economics(mutation):
    manifest, ledger, points, opening, after = inputs()
    from dataclasses import replace
    from strategies.hrs.risk_ledger import CashFlow
    bars=manifest['gaps'][0]['bars']['WLDUSDT']
    if mutation=='missing': bars.pop(1)
    if mutation=='wrong_time': bars[0][6]+=1
    if mutation=='wrong_owner': opening[0]['strategy']='new_coin'
    if mutation=='cash': ledger=replace(ledger,cash_flows=(CashFlow('x',D(1),START+timedelta(minutes=1),SEGMENT),))
    if mutation=='future': manifest['available_at']=(after+timedelta(seconds=1)).isoformat()
    if mutation=='wrong_close': bars[0][4]='14'
    with pytest.raises(EvidenceBlocked):
        reconstruct_gaps(manifest,'default',ledger,points,(),(),opening,after)


def test_replayed_breach_latches_even_after_valuation_recovers():
    points=(cp(START,'1'),cp(START+timedelta(minutes=1),'0.93'),cp(END,'1'))
    prior=LossGateState(SEGMENT,START.astimezone(timezone(timedelta(hours=8))).date())
    result=replay_inception_latches(prior,points)
    assert result.rolling_latched and result.daily_latched
    assert result.rolling_latch_scope=='inception_segment'


def test_today_coverage_does_not_qualify_incomplete_older_history():
    points = (cp(START-timedelta(minutes=5),'1'), cp(START,'1'), cp(END,'0.99'))
    history = GateHistory(points, points[0].as_of, END, False, True)
    result = evaluate_loss_gate(None, points[-1], history, allow_inception_segment=True)
    assert result.daily_return == D('-0.01')
    assert result.reasons == ('rolling_30_day_history_incomplete',)
    assert not result.ready and not result.allow_new_risk
