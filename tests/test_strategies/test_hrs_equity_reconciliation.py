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


def held_gap_source():
    from strategies.hrs.risk_ledger import PositionMark
    from strategies.hrs.equity_evidence import canonical
    inception=T-timedelta(seconds=30)
    mark=PositionMark('wld','seg',D('28'),D('.2666'),D('.2645'),inception,True)
    initial=replace(observation(inception),positions=(mark,))
    base=initialize_ledger('seg',D('32.10214008'),initial)
    gate=evaluate_loss_gate(None,base.checkpoint,GateHistory((base.checkpoint,),inception,inception,True),allow_inception_segment=True)
    previous=StoredRiskState(1,base,gate.state)
    stop=T+timedelta(minutes=7,seconds=10)
    fill=dict(account_id='test',symbol='WLDUSDT',trade_id=10,order_id=10,side='SELL',qty=D('28'),price=D('.26'),trade_time_utc=stop)
    owner=dict(account_id='test',symbol='WLDUSDT',order_id='10',strategy='hrs')
    current=T+timedelta(minutes=10)
    incomes=(IncomeEvent('REALIZED_PNL:10','realized_pnl',D('-.1848'),stop,'seg',True),)
    start_ms=int((T-timedelta(minutes=1)).timestamp()*1000)
    bars=[[ms,'.2645','.2645','.2645','.2645','0',ms+59999] for ms in range(start_ms,start_ms+9*60000,60000)]
    request=dict(symbol='WLDUSDT',interval='1m',startTime=start_ms,endTime=bars[-1][6],limit=1500)
    page=dict(source='/fapi/v1/markPriceKlines',symbol='WLDUSDT',request=request,available_at=current+timedelta(seconds=1),bars=bars,response_sha256=canonical(bars)[1])
    data=ReconciliationSource('test','seg',inception,D('32.10214008'),observation(current,incomes),(),(base.checkpoint,),(),(fill,),(owner,),current+timedelta(seconds=1),'a'*64,
        opening_positions=(mark,),opening_inventory=(dict(strategy='hrs',symbol='WLDUSDT',quantity='28',entry_price='.2666'),),historical_mark_bars=(page,))
    return previous,data


def test_real_closed_mark_gap_recovery_crosses_midnight_and_stops_at_authenticated_close():
    previous,data=held_gap_source()
    rebuilt=rebuild_history(previous,data,allow_inception_segment=True)
    assert rebuilt.gate.ready and rebuilt.gate.allow_new_risk
    assert rebuilt.gate.window_label=='inception_segment' and not rebuilt.gate.qualified_30_day
    assert rebuilt.ledger.units==previous.ledger.units
    assert rebuilt.ledger.inception==previous.ledger.inception
    assert rebuilt.receipt['kind']=='historical_mark_gap_reconciliation_v1'
    assert rebuilt.receipt['available_at']==data.available_at
    assert T in {cp.as_of for cp in rebuilt.checkpoints}
    assert data.fills[0]['trade_time_utc'] in {cp.as_of for cp in rebuilt.checkpoints}
    assert previous.ledger.as_of==T-timedelta(seconds=30)
    assert any(p['source_kind']=='closed_mark_gap_bar' and p['evidence_sha256'] for p in rebuilt.receipt['checkpoints'])


def test_recovered_intermediate_loss_breach_remains_latched_after_stop_closes():
    from strategies.hrs.equity_evidence import canonical
    previous,data=held_gap_source()
    page=dict(data.historical_mark_bars[0]);page['bars']=[list(b) for b in page['bars']]
    page['bars'][3][4]='.18';page['response_sha256']=canonical(page['bars'])[1]
    rebuilt=rebuild_history(previous,replace(data,historical_mark_bars=(page,)),allow_inception_segment=True)
    assert rebuilt.gate.state.daily_latched and rebuilt.gate.state.rolling_latched
    assert not rebuilt.gate.allow_new_risk


@pytest.mark.parametrize('damage',['missing','duplicate','endpoint','future','open','hash','symbol','request'])
def test_mark_gap_sources_fail_closed(damage):
    from strategies.hrs.equity_evidence import canonical
    previous,data=held_gap_source()
    page=dict(data.historical_mark_bars[0]);page['bars']=[list(b) for b in page['bars']];page['request']=dict(page['request'])
    if damage=='missing':page['bars']=page['bars'][:2]+page['bars'][5:]
    elif damage=='duplicate':page['bars'].insert(1,list(page['bars'][0]))
    elif damage=='endpoint':page['source']='/fapi/v1/klines'
    elif damage=='future':page['available_at']=data.available_at+timedelta(seconds=1)
    elif damage=='open':page['bars'][2][6]+=1
    elif damage=='symbol':page['symbol']='BTCUSDT'
    elif damage=='request':page['request']['interval']='5m'
    page['response_sha256']='0'*64 if damage=='hash' else canonical(page['bars'])[1]
    with pytest.raises(EvidenceBlocked):
        rebuild_history(previous,replace(data,historical_mark_bars=(page,)),allow_inception_segment=True)


def test_later_reconciliation_reuses_retained_gap_pages_with_their_original_availability():
    previous,data=held_gap_source()
    repaired=rebuild_history(previous,data,allow_inception_segment=True)
    old=StoredRiskState(2,repaired.ledger,repaired.gate.state)
    later=data.current.as_of+timedelta(minutes=1)
    retained=dict(as_of=data.current.as_of,capture=dict(historical_mark_gap_pages=data.historical_mark_bars),evidence_sha256='b'*64)
    source=replace(data,current=replace(data.current,as_of=later,income_complete_through=later,inventory_verified_at=later),available_at=later+timedelta(seconds=1),historical_mark_bars=(),captures=(retained,),checkpoints=repaired.checkpoints)
    rebuilt=rebuild_history(old,source,allow_inception_segment=True)
    assert rebuilt.gate.ready and rebuilt.ledger.units==old.ledger.units


def test_gap_request_planner_only_requests_actual_hrs_episodes_and_is_bounded():
    from strategies.hrs.equity_reconciliation import historical_mark_gap_requests
    previous,data=held_gap_source()
    windows=historical_mark_gap_requests(data.account,data.checkpoints,data.fills,data.orders,data.inception,data.current.as_of,opening_inventory=data.opening_inventory)
    assert {row['symbol'] for row in windows}=={'WLDUSDT'}
    assert windows[-1]['end_ms']<int(data.fills[0]['trade_time_utc'].timestamp()*1000)
    assert historical_mark_gap_requests(data.account,(data.checkpoints[0],),(),(),data.inception,data.current.as_of,opening_inventory=())==()
    with pytest.raises(EvidenceBlocked,match='mark_gap_limit'):
        historical_mark_gap_requests(data.account,data.checkpoints,(),(),data.inception,data.inception+timedelta(days=8),opening_inventory=data.opening_inventory)


def test_original_saved_capture_precedes_reconstruction_bar_and_old_latches_survive():
    from strategies.hrs.equity_evidence import canonical
    previous,data=held_gap_source()
    previous=replace(previous,gate=replace(previous.gate,daily_latched=True,rolling_latched=True,rolling_latch_scope='inception_segment'))
    at=T+timedelta(minutes=2)
    inventory=[dict(symbol='WLDUSDT',positionAmt='28',entryPrice='.2666')]
    capture=dict(inventory=inventory,independent=inventory,local={'WLDUSDT':dict(direction='long',remaining_quantity='28',position_id='wld')},marks={'WLDUSDT':dict(symbol='WLDUSDT',markPrice='.27',time=int(at.timestamp()*1000))})
    parent=dict(as_of=at,capture=capture,evidence_sha256='c'*64)
    rebuilt=rebuild_history(previous,replace(data,captures=(parent,)),allow_inception_segment=True)
    proof=next(p for p in rebuilt.receipt['checkpoints'] if p['as_of']==at)
    assert proof['source_kind']=='saved_capture' and proof['evidence_sha256']=='c'*64
    assert rebuilt.gate.state.rolling_latched and not rebuilt.gate.allow_new_risk
    assert rebuilt.ledger.units==previous.ledger.units
