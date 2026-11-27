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


def event_inputs(*, exit_at=None, right=None):
    from dataclasses import replace
    manifest,ledger,points,opening,after=inputs()
    right=right or END+timedelta(seconds=15)
    after=right+timedelta(minutes=1)
    manifest.update(profile='automatic_event_mark_gap_v2',available_at=after.isoformat())
    manifest['gaps'][0]['right']=right.isoformat()
    rows=manifest['gaps'][0]['bars']['WLDUSDT']
    rows.append([int(END.timestamp()*1000),'10','15','9','10','0',int((END+timedelta(minutes=1)).timestamp()*1000)-1])
    ledger=replace(ledger,as_of=after)
    points=(points[0],cp(right,'1.01'))
    fills=();orders=()
    if exit_at:
        fills=(dict(account_id='default',symbol='WLDUSDT',trade_id='9',order_id='9',trade_time_utc=exit_at,side='SELL',qty='2',price='12'),)
        orders=(dict(account_id='default',symbol='WLDUSDT',order_id='9',strategy='hrs'),)
        ledger=replace(ledger,incomes=(*ledger.incomes,
            IncomeEvent('r9','realized_pnl',D(4),exit_at,SEGMENT,True),
            IncomeEvent('f9','fee',D('-0.2'),exit_at,SEGMENT,True)))
    return manifest,ledger,points,opening,after,fills,orders


def test_event_v2_plans_funding_and_complete_partial_tail_without_changing_v1():
    from strategies.hrs.equity_history_gap import plan_event_gaps,plan_constant_gaps,gap_nav_envelopes
    manifest,ledger,points,opening,after,fills,orders=event_inputs()
    assert len(plan_event_gaps('default',ledger,points,fills,orders,opening))==1
    with pytest.raises(EvidenceBlocked,match='automatic_economic_change'):
        plan_constant_gaps('default',ledger,points,fills,orders,opening)
    restored,_=reconstruct_gaps(manifest,'default',ledger,points,fills,orders,opening,after)
    bounds=gap_nav_envelopes(manifest,ledger,points,fills,orders,opening)
    assert len(restored)==4 and len(bounds)==5
    # Funding at01:01 has unknown ordering with extrema in01..02.
    assert D(bounds[1]['low_nav'])==D('.96') and D(bounds[1]['high_nav'])==D('1.07')
    assert D(bounds[-1]['high_nav'])==D('1.11')
    assert datetime.fromisoformat(bounds[-1]['as_of'])==points[-1].as_of-timedelta(microseconds=1)


def test_event_v2_full_exit_includes_independent_income_and_pre_post_inventory():
    from strategies.hrs.equity_history_gap import plan_event_gaps,gap_nav_envelopes
    manifest,ledger,points,opening,after,fills,orders=event_inputs(exit_at=START+timedelta(minutes=1,seconds=20))
    assert len(plan_event_gaps('default',ledger,points,fills,orders,opening))==1
    restored,_=reconstruct_gaps(manifest,'default',ledger,points,fills,orders,opening,after)
    assert restored[1].nav==D('1.048')  # Exact close uses post-exit inventory and exact incomes.
    bounds=gap_nav_envelopes(manifest,ledger,points,fills,orders,opening)
    assert D(bounds[1]['low_nav'])==D('.958') and D(bounds[1]['high_nav'])==D('1.11')
    assert D(bounds[2]['low_nav'])==D('1.048')==D(bounds[2]['high_nav'])
    # Possible peak survives the exit and a subsequent genuine point.
    prior=LossGateState(SEGMENT,START.astimezone(timezone(timedelta(hours=8))).date())
    from strategies.hrs.equity_history_gap import apply_conservative_bounds
    assert apply_conservative_bounds(prior,ledger,(*points,cp(after,'1.04')),bounds).rolling_latched


@pytest.mark.parametrize('damage',['partial','open','multiple','ambiguous','unverified_income','positive_fee','capital'])
def test_event_v2_unsupported_paths_fail_closed(damage):
    from strategies.hrs.equity_history_gap import plan_event_gaps
    from strategies.hrs.risk_ledger import CashFlow
    from dataclasses import replace
    manifest,ledger,points,opening,after,fills,orders=event_inputs(exit_at=START+timedelta(seconds=30))
    if damage=='partial':fills=(dict(fills[0],qty='1'),)
    elif damage=='open':fills=(dict(fills[0],side='BUY'),)
    elif damage=='multiple':fills=(*fills,dict(fills[0],trade_id='10'))
    elif damage=='ambiguous':orders=(*orders,dict(orders[0],strategy='grid'))
    elif damage=='unverified_income':ledger=replace(ledger,incomes=(replace(ledger.incomes[0],verified=False),))
    elif damage=='positive_fee':ledger=replace(ledger,incomes=(replace(ledger.incomes[0],kind='fee'),))
    else:ledger=replace(ledger,cash_flows=(CashFlow('c',D(1),START+timedelta(seconds=30),SEGMENT),))
    with pytest.raises(EvidenceBlocked):plan_event_gaps('default',ledger,points,fills,orders,opening)


def test_event_v2_midnight_tail_stays_on_economic_day_and_never_future():
    from strategies.hrs.equity_history_gap import gap_nav_envelopes
    from dataclasses import replace
    manifest,ledger,points,opening,after,fills,orders=event_inputs()
    delta=timedelta(minutes=-3)
    left=START+delta;right=START
    bars=[]
    for i in range(3):
        at=left+timedelta(minutes=i)
        bars.append([int(at.timestamp()*1000),'10','10','8','10','0',int((at+timedelta(minutes=1)).timestamp()*1000)-1])
    manifest['gaps']=[dict(left=left.isoformat(),right=right.isoformat(),bars={'WLDUSDT':bars})]
    ledger=replace(ledger,inception=left,as_of=right,incomes=())
    points=(cp(left,'1'),cp(right,'1'))
    bounds=gap_nav_envelopes(manifest,ledger,points,fills,orders,opening)
    assert all(datetime.fromisoformat(row['as_of']).astimezone(timezone(timedelta(hours=8))).date()==left.astimezone(timezone(timedelta(hours=8))).date() for row in bounds)
    assert max(datetime.fromisoformat(row['as_of']) for row in bounds)<ledger.as_of


@pytest.mark.parametrize('damage',[None,'hash','clock','overlap','missing','extra','unclosed','raw_response'])
def test_event_v2_receipts_validate_original_contiguous_responses_without_merging_hashes(damage):
    from strategies.hrs.equity_history_gap import verify_mark_receipts,BAR_SOURCE,MINUTE
    from strategies.hrs.equity_evidence import canonical
    manifest,ledger,points,opening,after,_,_=event_inputs()
    rows=manifest['gaps'][0]['bars']['WLDUSDT'];receipts=[]
    for bars in (rows[:4],rows[4:]):
        receipts.append(dict(source=BAR_SOURCE,params=dict(symbol='WLDUSDT',interval='1m',startTime=bars[0][0],endTime=bars[-1][6],limit=128),
            requested_at=after.isoformat(),received_at=after.isoformat(),response_sha256=canonical(bars)[1],response=bars))
    manifest['http_receipts']=receipts
    if damage=='hash':receipts[-1]['response_sha256']='0'*64
    elif damage=='clock':receipts[-1]['received_at']=(after+MINUTE).isoformat()
    elif damage=='overlap':receipts[-1]['params']['startTime']=rows[3][0]
    elif damage=='missing':receipts.pop()
    elif damage=='extra':receipts.append(dict(receipts[-1]))
    elif damage=='unclosed':receipts[-1]['requested_at']=END.isoformat()
    elif damage=='raw_response':receipts[-1]['response']=[]
    if damage:
        with pytest.raises(EvidenceBlocked):verify_mark_receipts(manifest,after)
    else:verify_mark_receipts(manifest,after)


@pytest.mark.parametrize('late_income',[False,True])
def test_event_v2_revalues_original_inventory_after_future_cash_unitization(late_income):
    from dataclasses import replace
    from strategies.hrs.equity_history_gap import revalue_gap_bounds,gap_nav_envelopes
    from strategies.hrs.risk_ledger import CashFlow
    manifest,ledger,points,opening,after,fills,orders=event_inputs()
    receipt=dict(kind=manifest['profile'],account_id='default',mark_gaps=manifest['gaps'])
    original=gap_nav_envelopes(manifest,ledger,points,fills,orders,opening)
    flow=CashFlow('later-cash',D(50),after+timedelta(hours=1),SEGMENT,D(1),'genuine-source')
    current=replace(ledger,as_of=after+timedelta(hours=2),cash_allocation=D(150),units=D(150),cash_flows=(flow,))
    if late_income:
        current=replace(current,incomes=(*current.incomes,IncomeEvent('late','funding',D('.5'),START+timedelta(minutes=2),SEGMENT,True)))
    valued=revalue_gap_bounds(receipt,current,points,fills,orders,opening)
    assert all(D(bound['source_units'])==100 for bound in valued)
    if late_income:
        assert valued[0]==original[0]
        assert D(valued[-1]['high_nav'])==D(original[-1]['high_nav'])+D('.005')
    else:assert valued==original
    current=replace(current,cash_flows=(replace(flow,nav_before=None),))
    with pytest.raises(EvidenceBlocked,match='unitization'):
        revalue_gap_bounds(receipt,current,points,fills,orders,opening)


def test_event_v2_undominated_exit_peak_ages_only_after_full_30_days():
    from dataclasses import replace
    from strategies.hrs.equity_history_gap import apply_conservative_bounds
    _,ledger,_,_,_,_,_=event_inputs()
    at=START+timedelta(minutes=2)-timedelta(microseconds=1)
    bound=dict(as_of=at.isoformat(),low_nav='1.048',high_nav='1.11',source_units='100')
    for age,expected in ((timedelta(days=30),True),(timedelta(days=30,microseconds=1),False)):
        current=replace(ledger,as_of=at+age)
        samples=(cp(current.as_of-timedelta(seconds=1),'1.048'),cp(current.as_of,'1.04'))
        gate=LossGateState(SEGMENT,current.as_of.astimezone(timezone(timedelta(hours=8))).date())
        assert apply_conservative_bounds(gate,current,samples,[bound]).rolling_latched is expected
        latched=replace(gate,rolling_latched=True,rolling_latch_scope='inception_segment')
        assert apply_conservative_bounds(latched,current,samples,[bound]).rolling_latched


def test_event_v2_income_at_right_midnight_preserves_independent_endpoint_peak_on_new_day():
    from dataclasses import replace
    from strategies.hrs.equity_history_gap import gap_nav_envelopes,apply_conservative_bounds
    # The entire gap is on the old day. Two independent exact-time incomes at
    # midnight can pass through1.08 before settling back to1.00.
    right=START;left=right-timedelta(minutes=3)
    ledger=LedgerState(SEGMENT,left,right,D(100),D(0),D(100),D(100),D(1),(
        IncomeEvent('r','realized_pnl',D(8),right,SEGMENT,True),
        IncomeEvent('f','fee',D(-8),right,SEGMENT,True)))
    rows=[[int((left+timedelta(minutes=i)).timestamp()*1000),'10','10','10','10','0',int((left+timedelta(minutes=i+1)).timestamp()*1000)-1] for i in range(3)]
    manifest=dict(profile='automatic_event_mark_gap_v2',account='default',gaps=[dict(left=left.isoformat(),right=right.isoformat(),bars={'WLDUSDT':rows})])
    opening=[dict(symbol='WLDUSDT',strategy='hrs',quantity='2',entry_price='10')]
    points=(cp(left,'1'),cp(right,'1'))
    bounds=gap_nav_envelopes(manifest,ledger,points,(),(),opening)
    assert len(bounds)==4 and bounds[-1]['as_of']==right.isoformat()
    assert D(bounds[-1]['low_nav'])==D('.92') and D(bounds[-1]['high_nav'])==D('1.08')
    assert all(D(bound['low_nav'])==1 for bound in bounds[:-1])
    gate=LossGateState(SEGMENT,right.astimezone(timezone(timedelta(hours=8))).date(),day_origin_nav=D(1))
    assert apply_conservative_bounds(gate,ledger,points,bounds).daily_latched


def test_event_v2_exit_at_right_includes_pre_exit_value_without_previous_minute_extrema():
    from dataclasses import replace
    from strategies.hrs.equity_history_gap import gap_nav_envelopes
    manifest,ledger,points,opening,after,fills,orders=event_inputs(exit_at=END,right=END)
    manifest['gaps'][0]['bars']['WLDUSDT'].pop() # Exactright has no partial tail.
    for bar in manifest['gaps'][0]['bars']['WLDUSDT']:bar[1:5]=['12','100','1','12']
    # Right actual is flat:100cash+1funding+4realized−.2fee.
    points=(points[0],cp(END,'1.048'))
    bounds=gap_nav_envelopes(manifest,ledger,points,fills,orders,opening)
    assert bounds[-1]['as_of']==END.isoformat()
    assert D(bounds[-1]['low_nav'])==D('1.008')
    assert D(bounds[-1]['high_nav'])==D('1.09')


def test_event_v2_late_right_income_keeps_receipt_until_new_endpoint_bound_ages_out():
    import asyncio
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from strategies.hrs.equity_service import HrsEquityService
    from strategies.hrs.equity_history_gap import gap_nav_envelopes,BAR_SOURCE
    from strategies.hrs.equity_evidence import canonical
    manifest,ledger,points,opening,after,fills,orders=event_inputs()
    rows=manifest['gaps'][0]['bars']['WLDUSDT']
    receipt=dict(kind=manifest['profile'],account_id='default',segment_id=SEGMENT,as_of=after.isoformat(),available_at=after.isoformat(),mark_gaps=manifest['gaps'],
        conservative_bounds=gap_nav_envelopes(manifest,ledger,points,fills,orders,opening),http_receipts=[dict(source=BAR_SOURCE,
            params=dict(symbol='WLDUSDT',interval='1m',startTime=rows[0][0],endTime=rows[-1][6],limit=128),
            requested_at=after.isoformat(),received_at=after.isoformat(),response_sha256=canonical(rows)[1])])
    right=points[-1].as_of
    current=replace(ledger,as_of=right+timedelta(days=30),incomes=(*ledger.incomes,IncomeEvent('late-end','funding',D('.5'),right,SEGMENT,True)))
    points=(points[0],cp(right,'1.015'))
    connection=type('Connection',(),{})();connection.fetch=AsyncMock(return_value=[dict(receipt=receipt,receipt_sha256=canonical(receipt)[1],available_at=after)])
    service=HrsEquityService(None,None,None,None);service.account='default';service._anchor_inventory=opening;service.clock=lambda:current.as_of
    evidence=dict(fills=fills,order_owners=orders)
    prior=LossGateState(SEGMENT,current.as_of.astimezone(timezone(timedelta(hours=8))).date())
    asyncio.run(service._saved_gap_latches(connection,prior,current,points,evidence))
    assert [row['as_of'] for row in evidence['history_gap_bounds']['bounds']]==[right.isoformat()]


@pytest.mark.parametrize('midnight',[False,True])
def test_event_v2_internal_exact_minute_income_ordering_retains_transient_states(midnight):
    from dataclasses import replace
    from strategies.hrs.equity_history_gap import gap_nav_envelopes
    at=START if midnight else START+timedelta(minutes=2)
    left=at-timedelta(minutes=2);right=at+timedelta(minutes=2)
    ledger=LedgerState(SEGMENT,left,right,D(100),D(0),D(100),D(100),D(1),(
        IncomeEvent('positive','funding',D(8),at,SEGMENT,True),
        IncomeEvent('negative','funding',D(-8),at,SEGMENT,True)))
    rows=[[int((left+timedelta(minutes=i)).timestamp()*1000),'10','10','10','10','0',int((left+timedelta(minutes=i+1)).timestamp()*1000)-1] for i in range(4)]
    manifest=dict(profile='automatic_event_mark_gap_v2',account='default',gaps=[dict(left=left.isoformat(),right=right.isoformat(),bars={'WLDUSDT':rows})])
    opening=[dict(symbol='WLDUSDT',strategy='hrs',quantity='2',entry_price='10')]
    bounds=gap_nav_envelopes(manifest,ledger,(cp(left,'1'),cp(right,'1')),(),(),opening)
    transient=next((bound for bound in bounds if bound['as_of']==at.isoformat()),None)
    assert transient is not None
    assert D(transient['low_nav'])==D('.92') and D(transient['high_nav'])==D('1.08')
    assert datetime.fromisoformat(transient['as_of']).astimezone(timezone(timedelta(hours=8))).date()==at.astimezone(timezone(timedelta(hours=8))).date()


def test_event_v2_internal_exact_minute_exit_unions_pre_post_holdings_and_income():
    from strategies.hrs.equity_history_gap import gap_nav_envelopes
    at=START+timedelta(minutes=2)
    manifest,ledger,points,opening,after,fills,orders=event_inputs(exit_at=at)
    manifest['gaps'][0]['bars']['WLDUSDT'][1][4]='12'
    bounds=gap_nav_envelopes(manifest,ledger,points,fills,orders,opening)
    transient=next((bound for bound in bounds if bound['as_of']==at.isoformat()),None)
    assert transient is not None
    assert D(transient['low_nav'])==D('1.008') and D(transient['high_nav'])==D('1.09')


def test_accepted_live_gap_reconstructs_new_entry_and_conservative_intrabar_bounds():
    from strategies.hrs.accepted_live_gap import plan_live_gap, reconstruct_live_gap, live_gap_envelopes
    left = START + timedelta(seconds=35)
    right = START + timedelta(minutes=4, seconds=15)
    entry = START + timedelta(minutes=1, seconds=30)
    opening = [dict(symbol='BLESSUSDT', strategy='hrs', quantity='-2', entry_price='10')]
    fills = (dict(account_id='default', symbol='AVAXUSDT', trade_id='2', order_id='2',
                  trade_time_utc=entry, side='BUY', qty='1', price='20'),)
    orders = (dict(account_id='default', symbol='AVAXUSDT', order_id='2', strategy='hrs'),)
    income = IncomeEvent('f2', 'fee', D('-.01'), entry, SEGMENT, True)
    ledger = LedgerState(SEGMENT, START, right, D(100), D('-.01'), D(100), D('99.99'),
                         D('.9999'), (income,))
    points = (cp(left, '1'), cp(right, '.9999'))
    plan = plan_live_gap('default', ledger, *points, fills, orders, opening)
    assert plan['symbols'] == ['AVAXUSDT', 'BLESSUSDT']
    bars = {}
    for symbol, price in (('AVAXUSDT', '20'), ('BLESSUSDT', '10')):
        bars[symbol] = [[int((START+timedelta(minutes=i)).timestamp()*1000),
                         price, price, price, price, '0',
                         int((START+timedelta(minutes=i+1)).timestamp()*1000)-1]
                        for i in range(4)]
    item = dict(plan, bars=bars)
    samples, _ = reconstruct_live_gap('default', ledger, item, fills, orders, opening,
                                      D(100), right+timedelta(seconds=1))
    assert [point.nav for point in samples] == [D(1), D('.9999'), D('.9999'), D('.9999')]
    bounds = live_gap_envelopes('default', ledger, item, fills, orders, opening, D(100))
    assert len(bounds) == 4
    assert all(D(bound['low_nav']) <= D('.9999') <= D(bound['high_nav'])
               for bound in bounds[1:])


def test_accepted_live_gap_refuses_unknown_entry_owner():
    from strategies.hrs.accepted_live_gap import plan_live_gap
    from dataclasses import replace
    manifest, ledger, points, opening, after = inputs()
    ledger = replace(ledger, as_of=END)
    fill = dict(account_id='default', symbol='AVAXUSDT', trade_id='2', order_id='2',
                trade_time_utc=START+timedelta(minutes=2), side='BUY', qty='1', price='20')
    with pytest.raises(EvidenceBlocked):
        plan_live_gap('default', ledger, *points, (fill,), (), opening)


@pytest.mark.parametrize('ambiguous',[False,True])
def test_accepted_live_gap_rejects_unproven_round_trip_on_other_symbol(ambiguous):
    from strategies.hrs.accepted_live_gap import plan_live_gap
    manifest,ledger,points,opening,after=inputs()
    entry=dict(account_id='default',symbol='AVAXUSDT',trade_id='2',order_id='2',
               trade_time_utc=START+timedelta(minutes=1),side='BUY',qty='1',price='20')
    exit_fill=dict(entry,trade_id='3',order_id='3',side='SELL',
                   trade_time_utc=START+timedelta(minutes=2))
    owners=[]
    if ambiguous:
        owners=[dict(account_id='default',symbol='AVAXUSDT',order_id=order,strategy=strategy)
                for order in ('2','3') for strategy in ('hrs','grid')]
    # 首尾净仓都为零仍须逐笔证明归属，不能把未知往返成交当作其他策略噪音。
    reason='history_gap_live_fill_owner' if ambiguous else 'fill_order_owner_unproven'
    with pytest.raises(EvidenceBlocked,match=reason):
        plan_live_gap('default',ledger,*points,(entry,exit_fill),owners,opening)


@pytest.mark.parametrize('ambiguous',[False,True])
def test_accepted_live_gap_keeps_same_symbol_owner_conflicts_blocked(ambiguous):
    from strategies.hrs.accepted_live_gap import plan_live_gap
    manifest,ledger,points,opening,after=inputs()
    fill=dict(account_id='default',symbol='WLDUSDT',trade_id='2',order_id='2',
              trade_time_utc=START+timedelta(minutes=2),side='BUY',qty='1',price='10')
    owners=[dict(account_id='default',symbol='WLDUSDT',order_id='2',strategy='grid')]
    if ambiguous:
        owners.append(dict(owners[0],strategy='hrs'))
    with pytest.raises(EvidenceBlocked):
        plan_live_gap('default',ledger,*points,(fill,),owners,opening)


def test_accepted_live_gap_receipt_revalues_after_later_income():
    from dataclasses import replace
    from strategies.hrs.accepted_live_gap import KIND, plan_live_gap, live_gap_envelopes
    from strategies.hrs.equity_history_gap import verify_mark_receipts, revalue_gap_bounds, BAR_SOURCE
    from strategies.hrs.equity_evidence import canonical
    manifest, ledger, points, opening, after = inputs()
    ledger = replace(ledger, as_of=END)
    plan = plan_live_gap('default', ledger, *points, (), (), opening)
    item = dict(plan, bars=manifest['gaps'][0]['bars'])
    receipt = dict(profile=KIND, account='default', available_at=after.isoformat(),
                   gaps=[item], http_receipts=[dict(source=BAR_SOURCE,
                       params=dict(symbol='WLDUSDT', interval='1m',
                                   startTime=item['bars']['WLDUSDT'][0][0],
                                   endTime=item['bars']['WLDUSDT'][-1][6], limit=4),
                       requested_at=after.isoformat(), received_at=after.isoformat(),
                       response_sha256=canonical(item['bars']['WLDUSDT'])[1])])
    verify_mark_receipts(receipt, after)
    prior = live_gap_envelopes('default', ledger, item, (), (), opening, D(100))
    changed = replace(ledger, as_of=after,
                      incomes=(*ledger.incomes, IncomeEvent('later','funding',D('.5'),after,SEGMENT,True)))
    revised = revalue_gap_bounds(dict(kind=KIND, account_id='default', mark_gaps=[item]),
                                 changed, points, (), (), opening)
    assert revised == prior
