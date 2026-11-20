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


def test_automatic_plan_selects_only_bounded_constant_economic_owned_gaps():
    from strategies.hrs.equity_history_gap import plan_constant_gaps
    manifest,ledger,points,opening,after=inputs()
    from dataclasses import replace
    ledger=replace(ledger,incomes=())
    plan=plan_constant_gaps('default',ledger,points,(),(),opening)
    assert plan==[dict(left=START.isoformat(),right=END.isoformat(),symbols=['WLDUSDT'])]


@pytest.mark.parametrize('damage',['cash','income','fill','units','large','midnight'])
def test_automatic_plan_refuses_unsupported_economic_or_clock_paths(damage):
    from strategies.hrs.equity_history_gap import plan_constant_gaps
    from strategies.hrs.risk_ledger import CashFlow
    from dataclasses import replace
    manifest,ledger,points,opening,after=inputs();ledger=replace(ledger,incomes=())
    fills=();orders=()
    if damage=='cash':ledger=replace(ledger,cash_flows=(CashFlow('c',D(1),START+timedelta(seconds=30),SEGMENT),))
    elif damage=='income':ledger=replace(ledger,incomes=(IncomeEvent('i','funding',D(1),START+timedelta(seconds=30),SEGMENT,True),))
    elif damage=='fill':
        fills=(dict(account_id='default',symbol='WLDUSDT',trade_id='1',order_id='1',trade_time_utc=START+timedelta(seconds=30),side='SELL',qty='1',price='10'),)
        orders=(dict(account_id='default',symbol='WLDUSDT',order_id='1',strategy='hrs'),)
    elif damage=='units':points=(points[0],replace(points[1],units=D(101)))
    elif damage=='large':points=(points[0],cp(START+timedelta(minutes=129),'1'))
    else:
        points=(cp(START-timedelta(seconds=30),'1'),points[1])
    with pytest.raises(EvidenceBlocked,match='history_gap_automatic'):
        plan_constant_gaps('default',ledger,points,fills,orders,opening)


@pytest.mark.parametrize('side',[1,-1])
def test_automatic_envelope_latches_possible_intrabar_breach_without_nav_samples(side):
    from strategies.hrs.equity_history_gap import conservative_gap_latches
    from dataclasses import replace
    manifest,ledger,points,opening,after=inputs();ledger=replace(ledger,incomes=(),income_total=D(0))
    opening[0]['quantity']=str(2*side)
    # Every close recovers; genuine OHLC extremes could still breach protection.
    for bar in manifest['gaps'][0]['bars']['WLDUSDT']:bar[1:5]=['10','15','5','10']
    state=LossGateState(SEGMENT,START.astimezone(timezone(timedelta(hours=8))).date(),day_origin_nav=D(1))
    actual_points=tuple(points)
    changed=conservative_gap_latches(state,manifest,ledger,points,(),(),opening)
    assert changed.daily_latched and changed.rolling_latched
    assert changed.rolling_latch_scope=='inception_segment'
    assert points==actual_points  # Bounds are risk evidence, not invented NAV.


def test_automatic_envelope_keeps_prior_bar_high_through_later_actual_samples():
    from strategies.hrs.equity_history_gap import conservative_gap_latches
    from dataclasses import replace
    manifest,ledger,points,opening,after=inputs();ledger=replace(ledger,incomes=(),income_total=D(0))
    bars=manifest['gaps'][0]['bars']['WLDUSDT']
    bars[0][1:5]=['12.5','15','12.5','12.5']  # NAV range1.05..1.10.
    for bar in bars[1:]:bar[1:5]=['12','12','11.5','11.5']  # NAV1.03..1.04.
    state=LossGateState(SEGMENT,START.astimezone(timezone(timedelta(hours=8))).date(),day_origin_nav=D(1))
    assert conservative_gap_latches(state,manifest,ledger,points,(),(),opening).rolling_latched
    # Retained risk bounds remain useful on a later ordinary refresh; they are
    # never inserted into the original checkpoint series.
    from strategies.hrs.equity_history_gap import apply_conservative_bounds
    future=replace(ledger,as_of=after+timedelta(minutes=1),nav=D('1.03'),equity=D(103))
    later=(*points,cp(future.as_of,'1.03'))
    retained=[dict(as_of=(START+timedelta(minutes=1)).isoformat(),low_nav='1.05',high_nav='1.10')]
    assert apply_conservative_bounds(state,future,later,retained).rolling_latched


def test_retained_bound_after_inception_month_uses_rolling_scope_without_coverage_claim():
    from strategies.hrs.equity_history_gap import apply_conservative_bounds
    from dataclasses import replace
    _,ledger,points,_,_=inputs()
    ledger=replace(ledger,as_of=START+timedelta(days=31),nav=D('1.03'),equity=D(103))
    samples=(cp(ledger.as_of-timedelta(days=1),'1.05'),cp(ledger.as_of,'1.03'))
    bound=dict(as_of=(ledger.as_of-timedelta(days=1)).isoformat(),low_nav='1.05',high_nav='1.10')
    gate=LossGateState(SEGMENT,ledger.as_of.astimezone(timezone(timedelta(hours=8))).date())
    assert apply_conservative_bounds(gate,ledger,samples,[bound]).rolling_latch_scope=='rolling_30_day'
