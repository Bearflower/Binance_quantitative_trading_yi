"""Synthetic HRS engine contracts, not historical qualification evidence."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect

import pytest

from ai_tuner.replay.broker import SymbolFilters, VirtualBroker
from ai_tuner.replay.models import Bar, DataBlocked, OrderIntent

UTC=timezone.utc
AT=datetime(2026,11,17,tzinfo=UTC)
D=Decimal


def broker():
    return VirtualBroker({'A':SymbolFilters(D('.01'),D('.001'),D('5'))},D(0),D(0),D(0),D(0),D(10000),D(2),D('.3'))


def bar(high='110',low='90'):
    return Bar('A','1h',AT,AT+timedelta(hours=1),D(100),D(high),D(low),D(100),D(10),D(1000),1)


def test_filled_entry_installs_brackets_before_first_bar_exit():
    b=broker()
    assert 'after_entries' in inspect.signature(b.on_bar).parameters, 'entry-fill bracket hook missing'
    b.submit((OrderIntent('hrs','A',1,'LONG','MARKET',D(10),'ENTRY'),),bar(),AT)
    def protect():
        b.submit((OrderIntent('hrs','A',2,'LONG','STOP',D(10),'STOP_LOSS',
                             trigger_price=D(95),reduce_only=True),),bar(),AT)
    b.on_bar('A',bar(),AT+timedelta(hours=1),after_entries=protect,entry_ts=AT)
    assert len(b.closed_trades)==1 and b.closed_trades[0].exit_reason=='STOP_LOSS'
    assert b.closed_trades[0].entry_time==AT


def test_explicit_hundred_percent_tp1_ignores_global_ratio():
    b=broker()
    assert 'close_quantity' in OrderIntent.__dataclass_fields__, 'fixed HRS exit quantity missing'
    b.submit((OrderIntent('hrs','A',1,'LONG','MARKET',D(10),'ENTRY'),),bar(),AT)
    b.on_bar('A',bar(),AT)
    b.submit((OrderIntent('hrs','A',2,'LONG','MARKET',D(10),'TP1',reduce_only=True,
                         close_quantity=D(10)),),bar(),AT)
    assert not b.positions['A'].is_open()


def test_tp2_explicit_quantity_is_original_entry_allocation():
    b=broker()
    assert 'close_quantity' in OrderIntent.__dataclass_fields__, 'fixed HRS exit quantity missing'
    b.submit((OrderIntent('hrs','A',1,'LONG','MARKET',D(10),'ENTRY'),),bar(),AT)
    b.on_bar('A',bar(),AT)
    for seq,reason,qty in ((2,'TP1','3'),(3,'TP2','4')):
        b.submit((OrderIntent('hrs','A',seq,'LONG','MARKET',D(qty),reason,reduce_only=True,
                             close_quantity=D(qty)),),bar(),AT)
    assert b.positions['A'].current_quantity==D(3)


def test_snapshot_scheduled_when_available_not_bucket_start():
    from ai_tuner.replay.clock import collect_snapshot_events
    from ai_tuner.replay.models import ExternalSnapshot
    snap=ExternalSnapshot('hrs','A',AT+timedelta(minutes=8),AT,None,None,None,None,None,
                          'x','x','x','a'*64,True)
    event=collect_snapshot_events([snap])[0]
    assert event.time==snap.observed_at, 'snapshot was exposed before collection'


def test_prospective_funding_evidence_accepts_interval_change_and_blocks_hole():
    import ai_tuner.replay.funding_history as history
    assert hasattr(history,'attested_funding_schedule'), 'prospective schedule evidence not wired'
    import hashlib,json
    start=AT;end=AT+timedelta(minutes=20)
    obs=[]
    for i in range(0,21,5):
        source=AT+timedelta(minutes=i)
        next_at=AT+timedelta(minutes=10 if i<10 else 30)
        payload={'symbol':'A','time':int(source.timestamp()*1000),
                 'nextFundingTime':int(next_at.timestamp()*1000),'fundingIntervalHours':1 if i<10 else 2}
        obs.append(dict(symbol='A',source_time=source.isoformat(),received_at=source.isoformat(),
                        next_funding_time=next_at.isoformat(),funding_interval_hours=payload['fundingIntervalHours'],
                        payload_hash=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()))
    evidence=dict(source='prospective_exchange_forecast',complete=True,observations=obs)
    result=history.attested_funding_schedule(evidence,('A',),start,end)
    assert result=={'A':(AT+timedelta(minutes=10),)}
    evidence['observations']=obs[:1]+obs[3:]
    with pytest.raises(DataBlocked,match='coverage_gap'):
        history.attested_funding_schedule(evidence,('A',),start,end)


def _hrs_fixture(tmp_path, *, missing=None, entry_type='MARKET', subbars=False, subbar_volume='10000', config_patch=None, candidate_drop_after=None, initial_position=False, cash_flow=None, omit_preentry=False, market_patch=None):
    import json
    from .conftest import make_bars,write_snapshot
    from ai_tuner.replay.data_portal import DataPortal
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    start=AT;end=start+timedelta(hours=3)
    config={'kline':{'min_klines_for_analysis':24},'scoring':{'entry_threshold':0,
        'technical':{'min_total_score':0,'min_primary_pattern_score':0}},
        'lv_rm':{'enabled':False},'circuit_breaker':{'enabled':False},
        'trading':{'leverage':2,'order_type':{'entry':entry_type},'time_stop':{'max_holding_hours':72}}}
    if config_patch:
        config.update(config_patch)
    bootstrap={'as_of':start.isoformat(),'complete':True,'config':config,
        'config_hashes':{'base_config_hash':'b'*64,'params_manifest_hash':'c'*64,'overlay_hash':'d'*64},
        'market_circuit_breaker_config':None,'allocation':'10000','account_equity':'10000','entry_policy':{'mode':'reduced','multiplier':'1',
            'ratio_cap':'1','entry_ceiling':'10000','revision':1,'allow_add':False},
        'ledger':{'segment_id':'test','units':'10000','history_complete_from':(AT-timedelta(hours=8)).isoformat(),
                  'history':[{'as_of':(AT-timedelta(hours=8)).isoformat(),'equity':'10000','units':'10000','nav':'1'},
                             {'as_of':AT.isoformat(),'equity':'10000','units':'10000','nav':'1'}],
                  'allow_inception_segment':True,'gate_state':None},
        'runtime':{'signal_timestamps':{},'pending_timeout_entries':[],'pause_until':None,
                   'circuit_breakers':{},'daily_open_count':{'short':0,'long':0},
                   'daily_reset_date':AT.date().isoformat(),'consecutive_losses':0,
                   'drawdown_pause_until':None,'stop_loss_monitor':{},'market_cooldowns':{},'total_pnl':'0'}}
    if subbars:
        bootstrap['execution_model']={'version':'hrs_1m_volume_v1','participation_rate':'.01','timeout_minutes':15}
    inputs=[]
    for i in range(1,4):
        decision=start+timedelta(hours=i)
        row={'symbol':'A','decision_at':decision.isoformat(),'observed_at':decision.isoformat(),'complete':True,
             'candidate_scan':{'as_of':start.isoformat(),'complete':True,'short_candidates':[],
                               'long_candidates':(['A'] if candidate_drop_after is None or i<candidate_drop_after else []),
                               'lv_rm_symbols':[],'dynamic_thresholds':None},
             'market':{'oi_usd':'20000000','funding_rate':'-.001','volume_24h':'100000000',
                       'entry_price':'100','market_cap':'100000000','has_market_cap':True,
                       'market_cap_source':'coingecko','source_observed_at':decision.isoformat(),
                       'expires_at':(decision+timedelta(hours=1)).isoformat(),
                       'pre_entry_oi_usd':'20000000','pre_entry_funding_rate':'-.001'},
             'risk':{'pool_index':None,'pool_index_12h':None,'exchange_position_clear':True,'claims_clear':True,
                     'allocation':'10000','entry_policy':bootstrap['entry_policy'],'account_equity_excluding_hrs':'0'}}
        if market_patch:
            row['market'].update(market_patch)
        if omit_preentry:
            row['market'].pop('pre_entry_oi_usd')
            row['market'].pop('pre_entry_funding_rate')
            row['risk'].pop('exchange_position_clear')
            row['risk'].pop('claims_clear')
        if missing:
            row.pop(missing)
        inputs.append(row)
    prices=[('100','101','99','100')]*33
    bars1=make_bars('A','1h',start-timedelta(hours=30),prices)
    bars4=make_bars('A','4h',start-timedelta(hours=120),[('100','101','99','100')]*30,240)
    evidence=_prospective_evidence(start,end)
    extra={'exchange_info.json':json.dumps({'symbols':[{'symbol':'A','filters':[
        {'filterType':'PRICE_FILTER','tickSize':'.01'},{'filterType':'LOT_SIZE','stepSize':'.001','minQty':'.001'},
        {'filterType':'MIN_NOTIONAL','notional':'5'}]}]}),
           'hrs_cash_flows.jsonl':(json.dumps(cash_flow)+'\n' if cash_flow else ''),
           'funding_schedule_evidence.json':json.dumps(evidence),
           'hrs_replay_state.json':json.dumps(bootstrap),
           'hrs_decision_inputs.jsonl':''.join(json.dumps(row)+'\n' for row in inputs)}
    directory=str(tmp_path/'hrs')
    all_bars={'A|1h':bars1,'A|4h':bars4}
    if subbars:
        rows=make_bars('A','1m',start,[('100','101','99','100')]*180,1)
        for row in rows:
            row['volume']=subbar_volume
        all_bars['A|1m']=rows
    write_snapshot(directory,'hrs',end,start,all_bars,['A'],['1h','4h'],extra_files=extra)
    portal=DataPortal(directory,'hrs',end,start)
    states={name:{'as_of':start.isoformat(),'complete':True,'rows':[]}
            for name in HrsAdapter.REQUIRED_STATE_TABLES}
    if initial_position:
        from strategies.hrs.exit_geometry import mode_exit_parameters
        states['hrs_positions']['rows']=[dict(position_id='initial-1',symbol='A',direction='long',
            entry_price='100',entry_quantity='10',remaining_quantity='10',atr='2',
            opened_at=(start-timedelta(hours=1)).isoformat(),entry_mode='standard',
            exit_ratios=['.3','.4'],exit_parameters=mode_exit_parameters(config,'standard'),
            stop_loss_price='95',tp1_price='103',tp2_price='107',target1_reached=False,target2_reached=False,
            best_price='100',next_review_at=(start+timedelta(hours=71)).isoformat(),
            mark_price='100',mark_observed_at=start.isoformat(),entry_fee_remaining='-.5',
            entry_slippage_remaining='-.2',funding_accrued='.1',mae='0',mfe='0',net_pnl_realized_to_date='0')]
    seen=[]
    def loader(at):
        seen.append(at)
        return states
    return portal,HrsAdapter(loader),seen


def test_real_hrs_adapter_scores_sizes_and_installs_actual_fill_protection(tmp_path):
    portal,adapter,seen=_hrs_fixture(tmp_path)
    ctx=adapter.prepare(portal)
    assert seen==[portal.window_start]
    first=portal.bars('A','1h')[-3]
    ctx.portfolio=broker().view()
    adapter.on_bar(first,ctx)
    intents=adapter.intents(ctx)
    assert len(intents)==1 and intents[0].reason=='ENTRY'
    assert intents[0].quantity>0 and intents[0].exit_ratios is not None
    b=broker(); b.submit(intents,first,first.close_time)
    next_bar=portal.bars('A','1h')[-2]
    def protect():
        ctx.portfolio=b.view()
        exits=adapter.on_entry_fill(b.positions['A'],ctx)
        b.submit(exits,next_bar,next_bar.open_time)
    b.on_bar('A',next_bar,next_bar.close_time,after_entries=protect,entry_ts=next_bar.open_time)
    prices=adapter.protective_prices('A')
    assert prices['stop_loss_price']==D(95)
    assert prices['tp1_price']==D(103)
    assert len(b._working_exits['A'])==3
    adapter.on_bar(replace(next_bar,interval='4h'),ctx)
    assert adapter.intents(ctx)==()


def test_hrs_missing_candidate_proof_blocks_instead_of_zero_trades(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,missing='candidate_scan')
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view()
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    with pytest.raises(DataBlocked,match='candidate_scan'):
        adapter.intents(ctx)


def test_hrs_hourly_limit_execution_is_explicitly_unsupported(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT')
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view()
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    with pytest.raises(DataBlocked,match='execution_resolution_unsupported'):
        adapter.intents(ctx)


def _prospective_evidence(start,end):
    import hashlib,json
    observations=[]
    when=start
    while when<=end:
        next_at=end+timedelta(hours=1)
        payload={'symbol':'A','time':int(when.timestamp()*1000),
                 'nextFundingTime':int(next_at.timestamp()*1000),'fundingIntervalHours':None}
        observations.append(dict(symbol='A',source_time=when.isoformat(),received_at=when.isoformat(),
            next_funding_time=next_at.isoformat(),funding_interval_hours=None,
            payload_hash=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()))
        when+=timedelta(minutes=5)
    return dict(source='prospective_exchange_forecast',complete=True,observations=observations)


def test_unified_engine_runs_real_hrs_and_does_not_emit_duplicate_hourly_decisions(tmp_path):
    from ai_tuner.replay.engine import run_replay
    portal,adapter,_=_hrs_fixture(tmp_path)
    result=run_replay(portal,adapter,broker(),include_snapshots=True)
    assert result.final_position is not None
    assert result.final_position.stop_loss_price==D(95)
    assert result.final_position.atr==D(2)
    scores=[event for event in result.audit_events if event['type']=='hrs_score']
    assert len(scores)==3
    assert len({event['ts'] for event in scores})==3
    fills=[event for event in result.audit_events if event['type']=='fill' and event['payload']['side']=='OPEN_LONG']
    assert len(fills)==1
    assert not result.invariant_violations


def test_hrs_limit_partial_fills_accumulate_then_timeout_closes_owned_quantity():
    assert 'execution_model' in OrderIntent.__dataclass_fields__, 'bounded 1m execution model missing'
    b=broker()
    model={'version':'hrs_1m_volume_v1','participation_rate':'.1','timeout_minutes':15}
    intent=OrderIntent('hrs','A',1,'LONG','LIMIT',D(10),'ENTRY',trigger_price=D(100),limit_price=D(100),
                       execution_model=model,expires_at=AT+timedelta(minutes=15))
    b.submit((intent,),bar(),AT)
    for minute in range(1,16):
        current=replace(bar(),interval='1m',open_time=AT+timedelta(minutes=minute-1),
                        close_time=AT+timedelta(minutes=minute),volume=D(1))
        b.on_bar('A',current,current.close_time,entry_ts=current.close_time)
    assert not b.positions['A'].is_open()
    assert b.closed_trades[0].quantity==D('1.5')
    assert b.closed_trades[0].exit_reason=='ENTRY_TIMEOUT'
    assert any(e['type']=='entry_timeout' for e in b.audit)


def test_hrs_limit_full_fill_activates_brackets_after_completion():
    assert 'execution_model' in OrderIntent.__dataclass_fields__, 'bounded 1m execution model missing'
    b=broker();model={'version':'hrs_1m_volume_v1','participation_rate':'.1','timeout_minutes':15}
    intent=OrderIntent('hrs','A',1,'LONG','LIMIT',D(2),'ENTRY',trigger_price=D(100),limit_price=D(100),
                       execution_model=model,expires_at=AT+timedelta(minutes=15))
    b.submit((intent,),bar(),AT)
    states=[]
    for minute in range(1,3):
        current=replace(bar(high='101',low='99'),interval='1m',open_time=AT+timedelta(minutes=minute-1),
                        close_time=AT+timedelta(minutes=minute),volume=D(10))
        def observe():
            states.append((b.positions['A'].current_quantity,b.positions['A'].entry_complete))
        b.on_bar('A',current,current.close_time,entry_ts=current.close_time,after_entries=observe)
    assert states==[(D(1),False),(D(2),True)]
    assert not b._pending_entries.get('A')


def test_unified_real_hrs_limit_model_opens_from_manifest_one_minute_inputs(tmp_path):
    from ai_tuner.replay.engine import run_replay
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    result=run_replay(portal,adapter,broker(),include_snapshots=True)
    assert result.final_position is not None and result.final_position.entry_complete
    assert result.final_position.entry_time==AT+timedelta(hours=1,minutes=1)
    assert result.final_position.stop_loss_price==D(95)
    assert any(e['type']=='entry_complete' for e in result.audit_events)
    assert not result.invariant_violations


def test_unified_real_hrs_limit_timeout_records_partial_owned_close(tmp_path):
    from ai_tuner.replay.engine import run_replay
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True,subbar_volume='1')
    result=run_replay(portal,adapter,broker(),include_snapshots=True)
    assert len(result.closed_trades)==2
    assert all(trade.exit_reason=='ENTRY_TIMEOUT' for trade in result.closed_trades)
    assert all(trade.quantity==D('.15') for trade in result.closed_trades)
    assert result.final_position is None
    assert not result.invariant_violations


def test_primary_rejection_still_records_independent_emm_shadow(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,config_patch={
        'scoring':{'entry_threshold':99},
        'emm':{'enabled':True,'execution_mode':'shadow','long':{'price_change_24h':0,
               'funding_rate_annual':0,'oi_market_cap_ratio':.1},'scoring':{'entry_threshold':0}}})
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view()
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    assert adapter.intents(ctx)==()
    shadows=[e for e in adapter.audit if e['type']=='hrs_shadow']
    assert shadows and shadows[0]['payload']['result']['entry_mode']=='emm'


def test_loss_gate_rejects_new_risk_after_marked_drawdown(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path)
    ctx=adapter.prepare(portal);ctx.portfolio=replace(broker().view(),realized_equity=D(9600))
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    assert adapter.intents(ctx)==()
    rejects=[e for e in adapter.audit if e['type']=='hrs_reject']
    assert any('daily_loss_latched' in e['payload']['reason'] for e in rejects)


def test_time_stop_reanalysis_uses_actual_standard_score_after_candidate_removal(tmp_path):
    from ai_tuner.replay.engine import run_replay
    portal,adapter,_=_hrs_fixture(tmp_path,candidate_drop_after=2,config_patch={
        'trading':{'leverage':2,'order_type':{'entry':'MARKET'},
                   'time_stop':{'max_holding_hours':1,'reanalyze':{'enabled':True,'hold_threshold':3}}}})
    result=run_replay(portal,adapter,broker())
    assert result.final_position is not None
    assert result.closed_trades==()


def test_time_stop_closes_when_reanalysis_is_below_hold_threshold(tmp_path):
    from ai_tuner.replay.engine import run_replay
    portal,adapter,_=_hrs_fixture(tmp_path,candidate_drop_after=2,config_patch={
        'trading':{'leverage':2,'order_type':{'entry':'MARKET'},
                   'time_stop':{'max_holding_hours':1,'reanalyze':{'enabled':True,'hold_threshold':99}}}})
    result=run_replay(portal,adapter,broker())
    assert len(result.closed_trades)==1 and result.closed_trades[0].exit_reason=='TIME_STOP'
    assert result.final_position is None


def test_unified_hrs_restores_complete_opening_position_instead_of_erasing_inventory(tmp_path):
    from ai_tuner.replay.engine import run_replay
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True)
    b=broker();result=run_replay(portal,adapter,b)
    assert result.final_position.position_id=='initial-1'
    assert result.final_position.current_quantity==D(10)
    assert result.final_position.stop_loss_price==D(95)
    assert b._entry_fee['initial-1']==D('-.5')
    assert not any(e['type']=='fill' and e['payload']['side'].startswith('OPEN') for e in result.audit_events)


def test_hrs_take_profit_uses_live_adverse_limit_offset():
    b=broker();b.tp_offset_pct=D('.0015')
    intent=OrderIntent('hrs','A',1,'LONG','TAKE_PROFIT',D(1),'TP1',trigger_price=D(110),reduce_only=True)
    assert b._exit_fill_price(intent,D(110))==D('109.8350')
    assert b._exit_fill_price(replace(intent,side='SHORT'),D(90))==D('90.1350')


def test_hrs_stop_gap_uses_adverse_open_instead_of_fictitious_trigger_fill():
    b=broker()
    b.submit((OrderIntent('hrs','A',1,'LONG','MARKET',D(10),'ENTRY'),),bar(),AT)
    b.on_bar('A',bar(high='101',low='99'),AT)
    b.submit((OrderIntent('hrs','A',2,'LONG','STOP',D(10),'STOP_LOSS',trigger_price=D(95),reduce_only=True),),bar(),AT)
    gap=replace(bar(),open=D(90),close=D(91),high=D(92),low=D(88))
    b.on_bar('A',gap,AT+timedelta(hours=1))
    assert b.closed_trades[0].exit_price==D(90)


def test_replay_cashflow_uses_shared_unit_ledger_and_does_not_create_fake_return(tmp_path):
    from ai_tuner.replay.engine import run_replay
    flow=dict(event_id='deposit-1',amount='1000',occurred_at=(AT+timedelta(hours=1)).isoformat(),
              observed_at=(AT+timedelta(hours=1)).isoformat(),segment_id='test',verified=True)
    portal,adapter,_=_hrs_fixture(tmp_path,cash_flow=flow)
    result=run_replay(portal,adapter,broker())
    assert result.equity_curve[-1].realized_equity==D(11000)
    assert adapter._ledger_state.units==D(11000)
    assert adapter._ledger_state.nav==D(1)
    assert adapter._gate.daily_latched is False


def test_replay_ledger_reconciles_modeled_entry_exit_fees_and_slippage(tmp_path):
    from ai_tuner.replay.engine import run_replay
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True,subbar_volume='1')
    b=broker();b.fee_bps=D(5);b.slippage_bps=D(2)
    result=run_replay(portal,adapter,b)
    assert result.equity_curve[-1].realized_equity<D(10000)
    assert adapter._ledger_state.equity==result.equity_curve[-1].marked_equity
    assert any(event.kind=='fee' for event in adapter._ledger_state.incomes)


def test_protection_retains_planned_geometry_reference_after_better_fill(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path)
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view()
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    intent=adapter.intents(ctx)[0]
    assert hasattr(intent,'geometry_reference_price'), 'planned live geometry reference missing'
    from ai_tuner.replay.models import Position
    position=Position('fill-improved','A','LONG',D(99),intent.quantity,intent.quantity,
        intent.atr,'',AT+timedelta(hours=1),entry_mode=intent.entry_mode,exit_ratios=intent.exit_ratios,
        exit_parameters=intent.exit_parameters,geometry_reference_price=intent.geometry_reference_price)
    ctx.portfolio=replace(ctx.portfolio,positions=(position,))
    adapter.on_entry_fill(position,ctx)
    assert adapter.protective_prices('A')['stop_loss_price']==D(95)


def test_multiple_pending_hrs_entries_keep_distinct_position_cost_ledgers():
    b=broker();b.filters['B']=b.filters['A'];b.fee_bps=D(5)
    intents=tuple(OrderIntent('hrs',symbol,i,'LONG','MARKET',D(10),'ENTRY')
                  for i,symbol in enumerate(('A','B'),1))
    b.submit(intents,bar(),AT)
    for symbol in ('A','B'):
        b.on_bar(symbol,replace(bar(),symbol=symbol),AT)
    ids=[b.positions[symbol].position_id for symbol in ('A','B')]
    assert len(set(ids))==2
    assert all(b._entry_fee[identity]==D('-.5') for identity in ids)


def test_rejected_score_does_not_require_unexecuted_later_stage_exchange_calls(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,omit_preentry=True,config_patch={'scoring':{'entry_threshold':99}})
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view()
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    assert adapter.intents(ctx)==()


def test_executable_signal_requires_later_stage_proof_when_consumed(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,omit_preentry=True)
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view()
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    with pytest.raises(DataBlocked,match='entry_stage'):
        adapter.intents(ctx)


@pytest.mark.parametrize('field',('oi_usd','market_cap','funding_rate','entry_price'))
def test_nonfinite_raw_market_inputs_are_structured_data_blocks(tmp_path,field):
    portal,adapter,_=_hrs_fixture(tmp_path,market_patch={field:'NaN'})
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view()
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    with pytest.raises(DataBlocked):
        adapter.intents(ctx)
