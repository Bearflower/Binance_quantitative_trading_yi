"""Review regressions: causal fills, real funding marks and source-time valuation."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import inspect
import json

import pytest

from ai_tuner.replay.broker import PortfolioView
from ai_tuner.replay.data_portal import DataPortal, compute_manifest_sha256, sha256_file
from ai_tuner.replay.engine import run_replay
from ai_tuner.replay.models import DataBlocked, OrderIntent
from .test_hrs_wiring import AT, bar, broker, _hrs_fixture

D=Decimal


def refreeze(portal,updates):
    """Build another sealed synthetic input variant, never bypass portal hashes."""
    root=portal.snapshot_dir
    manifest=json.loads((root/'manifest.json').read_text())
    for name,payload in updates.items():
        (root/name).write_text(payload)
        manifest['files'][name]={'path':name,'sha256':sha256_file(str(root/name))}
    manifest['sha256']=compute_manifest_sha256(manifest['files'])
    (root/'manifest.json').write_text(json.dumps(manifest))
    return DataPortal(root,'hrs',portal.as_of,portal.window_start)


def jsonl(rows):
    return ''.join(json.dumps(row)+'\n' for row in rows)


def test_standard_candidate_overlap_preserves_live_lv_rm_exclusion(tmp_path,monkeypatch):
    from strategies.hrs import core_decision
    portal,adapter,_=_hrs_fixture(tmp_path,config_patch={'scoring':{'entry_threshold':99},'lv_rm':{'enabled':True}})
    rows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for row in rows:
        row['candidate_scan']['lv_rm_symbols']=['A']
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(rows)})
    def forbidden(**kwargs):
        raise AssertionError('Live policy skips LV-RM for either standard candidate membership')
    monkeypatch.setattr(core_decision,'analyze_lv_rm',forbidden)
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view()
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    assert adapter.intents(ctx)==()


def test_close_time_limit_fill_cannot_earn_a_tp_from_earlier_minute_high():
    b=broker()
    minute=replace(bar(),interval='1m',open_time=AT,close_time=AT+timedelta(minutes=1),
                   open=D(110),high=D(120),low=D(99),close=D(100),volume=D(100))
    model={'version':'hrs_1m_volume_v1','participation_rate':'.1','timeout_minutes':15}
    b.submit((OrderIntent('hrs','A',1,'LONG','LIMIT',D(1),'ENTRY',trigger_price=D(100),limit_price=D(100),
                        execution_model=model,expires_at=AT+timedelta(minutes=15)),),minute,AT)
    def brackets():
        b.submit((OrderIntent('hrs','A',2,'LONG','TAKE_PROFIT',D(1),'TP1',trigger_price=D(105),
                             reduce_only=True,close_quantity=D(1)),),minute,minute.close_time)
    b.on_bar('A',minute,minute.close_time,after_entries=brackets,entry_ts=minute.close_time)
    assert b.closed_trades==[]
    assert b.positions['A'].current_quantity==D(1)
    later=replace(minute,open_time=minute.close_time,close_time=AT+timedelta(minutes=2),
                  open=D(100),high=D(106),low=D(99),close=D(100))
    b.on_bar('A',later,later.close_time)
    assert b.closed_trades[0].exit_time==later.close_time


def funding_rows(portal,*,mark=True,available_offset=0):
    settlement=AT+timedelta(hours=1,minutes=30)
    rows=[dict(symbol='A',funding_time=settlement.isoformat(),funding_rate='.01')]
    if mark:
        selected=dict(symbol='A',funding_time=settlement.isoformat(),mark_price='150',
                      mark_time=settlement.isoformat(),mark_source='binance_um_funding_rate')
        rows[0].update(selected,mark_available_at=(settlement+timedelta(seconds=available_offset)).isoformat(),
            mark_payload_hash=hashlib.sha256(json.dumps(selected,sort_keys=True,separators=(',',':')).encode()).hexdigest())
    evidence=json.loads((portal.snapshot_dir/'funding_schedule_evidence.json').read_text())
    for observation in evidence['observations']:
        source=__import__('datetime').datetime.fromisoformat(observation['source_time'])
        next_at=settlement if source<settlement else portal.as_of+timedelta(hours=1)
        observation['next_funding_time']=next_at.isoformat()
        payload=dict(symbol='A',time=int(source.timestamp()*1000),nextFundingTime=int(next_at.timestamp()*1000),
                     fundingIntervalHours=observation['funding_interval_hours'])
        observation['payload_hash']=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return refreeze(portal,{'funding_rates.jsonl':jsonl(rows),'funding_schedule_evidence.json':json.dumps(evidence)})


def test_hrs_funding_uses_proven_settlement_mark_instead_of_entry_price(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    portal=funding_rows(portal)
    result=run_replay(portal,adapter,broker())
    event=next(event for event in result.audit_events if event['type']=='funding_settlement')
    quantity=result.final_position.entry_quantity
    assert D(event['payload']['contribution'])==-quantity*D(150)*D('.01')
    assert event['payload']['mark_price']=='150'


@pytest.mark.parametrize('mark,offset',[(False,0),(True,-1)])
def test_hrs_funding_missing_or_unavailable_mark_blocks(tmp_path,mark,offset):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    portal=funding_rows(portal,mark=mark,available_offset=offset)
    with pytest.raises(DataBlocked,match='funding_mark'):
        run_replay(portal,adapter,broker())


def test_broker_exposes_original_price_time_without_refreshing_it():
    b=broker();b.on_bar('A',bar(),AT+timedelta(hours=1))
    view=b.view()
    assert hasattr(view,'last_price_times'), 'source price time missing from portfolio view'
    assert view.last_price_times['A']==bar().close_time
    assert b.view().last_price_times['A']==view.last_price_times['A']


def test_intrahour_cashflow_cannot_relabel_old_market_prices_as_fresh(tmp_path):
    flow=dict(event_id='intra-flow',amount='1000',occurred_at=(AT+timedelta(minutes=30)).isoformat(),
              observed_at=(AT+timedelta(minutes=30)).isoformat(),segment_id='test',verified=True)
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True,cash_flow=flow)
    with pytest.raises(DataBlocked,match='stale_position_mark'):
        run_replay(portal,adapter,broker())


def test_trailing_protection_updates_on_declared_one_minute_monitor_schedule(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True,initial_position=True,
        config_patch={'cycle':{'check_interval_seconds':60}})
    state=adapter._state_loader(portal.window_start)
    row=state['hrs_positions']['rows'][0]
    row.update(remaining_quantity='3',target1_reached=True,target2_reached=True,best_price='100')
    bars=[json.loads(line) for line in (portal.snapshot_dir/'klines/A_1m.jsonl').read_text().splitlines()]
    bars[0].update(open='100',high='105',low='99',close='105')
    bars[1].update(open='105',high='106',low='100',close='101')
    portal=refreeze(portal,{'klines/A_1m.jsonl':jsonl(bars)})
    result=run_replay(portal,adapter,broker())
    initial=next((trade for trade in result.closed_trades if trade.position_id=='initial-1'),None)
    assert initial is not None and initial.exit_time==AT+timedelta(minutes=2)
    assert initial.exit_price==D(102)


def test_allocation_cut_requiring_liquidation_is_explicitly_blocked(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True)
    inputs=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    inputs[0]['risk']['allocation']='1'
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(inputs)})
    with pytest.raises(DataBlocked,match='allocation_cut_liquidation'):
        run_replay(portal,adapter,broker())


def test_unrepresented_configuration_change_requires_window_partition(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path)
    inputs=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    inputs[0]['config_hashes']={'base_config_hash':'e'*64,'params_manifest_hash':'c'*64,'overlay_hash':'d'*64}
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(inputs)})
    with pytest.raises(DataBlocked,match='config_change_requires_partition'):
        run_replay(portal,adapter,broker())


def test_off_hour_decision_uses_inputs_when_available_then_fills_next_minute(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    rows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()][:2]
    calendar=[]
    for index,row in enumerate(rows,1):
        decided=AT+timedelta(hours=index,minutes=21)
        available=decided+timedelta(seconds=5)
        row.update(decision_at=decided.isoformat(),observed_at=available.isoformat())
        row['risk']['account_occupancy']['observed_at']=available.isoformat()
        row['market'].update(source_observed_at=available.isoformat(),expires_at=(available+timedelta(hours=1)).isoformat())
        calendar.append(dict(symbol='A',decision_at=decided.isoformat(),available_at=available.isoformat(),
                             batch_available_at=available.isoformat(),sequence=index))
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(rows),'hrs_decision_calendar.jsonl':jsonl(calendar)})
    result=run_replay(portal,adapter,broker())
    first_score=next(event for event in result.audit_events if event['type']=='hrs_score')
    assert first_score['ts']=='2026-11-17T01:21:05Z'
    # The 01:21-01:22 bar began before submission, so its unknown partial path
    # cannot prove a fill. Use the next complete minute instead.
    assert result.final_position.entry_time==AT+timedelta(hours=1,minutes=23)
    assert not result.invariant_violations


def test_real_hrs_engine_requires_explicit_decision_calendar(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path)
    manifest=json.loads((portal.snapshot_dir/'manifest.json').read_text())
    manifest['files'].pop('hrs_decision_calendar.jsonl',None)
    manifest['sha256']=compute_manifest_sha256(manifest['files'])
    (portal.snapshot_dir/'manifest.json').write_text(json.dumps(manifest))
    portal=DataPortal(portal.snapshot_dir,'hrs',portal.as_of,portal.window_start)
    with pytest.raises(DataBlocked,match='decision_calendar'):
        run_replay(portal,adapter,broker())


def test_calendar_cannot_expose_raw_inputs_before_they_are_available(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path)
    inputs=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    calendar=[dict(symbol='A',decision_at=row['decision_at'],available_at=row['observed_at'],
                   batch_available_at=row['observed_at'],sequence=index) for index,row in enumerate(inputs)]
    inputs[0]['observed_at']=(AT+timedelta(hours=1,seconds=1)).isoformat()
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(inputs),'hrs_decision_calendar.jsonl':jsonl(calendar)})
    with pytest.raises(DataBlocked,match='decision_calendar'):
        run_replay(portal,adapter,broker())


def test_calendar_cannot_omit_a_captured_decision_to_hide_its_trade(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path)
    inputs=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    calendar=[dict(symbol='A',decision_at=row['decision_at'],available_at=row['observed_at'],
                   batch_available_at=row['observed_at'],sequence=index) for index,row in enumerate(inputs)][1:]
    portal=refreeze(portal,{'hrs_decision_calendar.jsonl':jsonl(calendar)})
    with pytest.raises(DataBlocked,match='decision_calendar'):
        run_replay(portal,adapter,broker())


def test_empty_calendar_cannot_turn_missing_scans_into_successful_zero_trades(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path)
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':'','hrs_decision_calendar.jsonl':''})
    with pytest.raises(DataBlocked,match='decision_calendar_empty'):
        run_replay(portal,adapter,broker())


def test_monitor_phase_may_begin_after_window_start_without_backdating_it(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    bootstrap=json.loads((portal.snapshot_dir/'hrs_replay_state.json').read_text())
    bootstrap['monitoring_model']['anchor_at']=(AT+timedelta(minutes=21)).isoformat()
    portal=refreeze(portal,{'hrs_replay_state.json':json.dumps(bootstrap)})
    ctx=adapter.prepare(portal)
    adapter.configure_execution(ctx)
    assert adapter._monitor_anchor==AT+timedelta(minutes=21)


def test_calendar_keeps_explicit_source_sequence_instead_of_symbol_sort(tmp_path):
    portal,_,_=_hrs_fixture(tmp_path)
    inputs=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()][:1]
    other=json.loads(json.dumps(inputs[0]));other['symbol']='Z'
    inputs.append(other)
    calendar=[dict(symbol='Z',decision_at=other['decision_at'],available_at=other['observed_at'],batch_available_at=other['observed_at'],sequence=0),
              dict(symbol='A',decision_at=inputs[0]['decision_at'],available_at=inputs[0]['observed_at'],batch_available_at=inputs[0]['observed_at'],sequence=1)]
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(inputs),'hrs_decision_calendar.jsonl':jsonl(calendar)})
    assert [row['symbol'] for row in portal.hrs_decision_calendar(('A','Z'))]==['Z','A']


def test_experiment_exit_overrides_keep_source_config_and_initial_positions_frozen(tmp_path):
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    assert 'exit_parameter_overrides' in inspect.signature(HrsAdapter).parameters
    portal,original,_=_hrs_fixture(tmp_path,initial_position=True)
    source=json.loads((portal.snapshot_dir/'hrs_replay_state.json').read_text())
    adapter=HrsAdapter(original._state_loader,exit_parameter_overrides={'standard':{'target1_atr_multiplier':3}},experiment_id='registered-test')
    ctx=adapter.prepare(portal);adapter.configure_execution(ctx)
    assert adapter.config==source['config']
    restored=adapter.initial_positions(ctx)
    assert restored[0]['position'].exit_parameters['target1_atr_multiplier']==1.5
    assert adapter.exit_parameters('standard')['target1_atr_multiplier']==D(3)
    assert isinstance(adapter.equity_checkpoints(),tuple)


def test_experiment_cost_pressure_covers_actual_scenario_fee(tmp_path):
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    assert 'execution_costs' in inspect.signature(HrsAdapter).parameters
    portal,original,_=_hrs_fixture(tmp_path)
    adapter=HrsAdapter(original._state_loader,experiment_id='cost-2x',execution_costs={'fee_rate':D('.001'),'slippage':D('.0004')})
    ctx=adapter.prepare(portal);adapter.configure_execution(ctx)
    fee,slippage=adapter._pressure()
    assert fee==D('.001') and slippage>=D('.02')
    with pytest.raises(ValueError):
        HrsAdapter(original._state_loader,experiment_id='bad',exit_parameter_overrides={'standard':{'entry_threshold':0}})


def pending_funding_fixture(tmp_path,*,delay=90,crash=False,flow=False,missing_mark=False):
    economic=AT+timedelta(hours=1,minutes=30)
    cash_flow=(dict(event_id='funding-period-flow',amount='1000',occurred_at=(economic+timedelta(seconds=30)).isoformat(),
                    observed_at=(economic+timedelta(seconds=30)).isoformat(),segment_id='test',verified=True) if flow else None)
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True,cash_flow=cash_flow)
    portal=funding_rows(portal,mark=not missing_mark,available_offset=delay)
    updates={}
    if crash:
        bars=[json.loads(line) for line in (portal.snapshot_dir/'klines/A_1m.jsonl').read_text().splitlines()]
        bars[90]['low']='94'
        updates['klines/A_1m.jsonl']=jsonl(bars)
        hours=[json.loads(line) for line in (portal.snapshot_dir/'klines/A_1h.jsonl').read_text().splitlines()]
        hours[31]['low']='94'
        updates['klines/A_1h.jsonl']=jsonl(hours)
    inputs=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    decision=economic+timedelta(seconds=70)
    inputs[1].update(decision_at=decision.isoformat(),observed_at=decision.isoformat())
    inputs[1]['market'].update(source_observed_at=decision.isoformat(),expires_at=(decision+timedelta(hours=1)).isoformat())
    updates['hrs_decision_inputs.jsonl']=jsonl(inputs)
    updates['hrs_decision_calendar.jsonl']=jsonl([dict(symbol=row['symbol'],decision_at=row['decision_at'],
        available_at=row['observed_at'],batch_available_at=row['observed_at'],sequence=index)
        for index,row in enumerate(inputs)])
    return refreeze(portal,updates),adapter,economic


def test_delayed_funding_receipt_keeps_economic_quantity_and_blocks_entries_while_exits_continue(tmp_path):
    portal,adapter,economic=pending_funding_fixture(tmp_path,delay=90,crash=True)
    b=broker();result=run_replay(portal,adapter,b)
    settlement=next(event for event in result.audit_events if event['type']=='funding_settlement')
    assert settlement['ts']=='2026-11-17T01:31:30Z'
    assert settlement['payload']['economic_time']=='2026-11-17T01:30:00Z'
    trade=result.closed_trades[0]
    assert trade.exit_time==economic+timedelta(minutes=1)
    assert trade.funding_fee==-trade.quantity*D(150)*D('.01')
    assert any(event['type']=='hrs_reject' and event['payload']['reason']=='funding_pending' for event in result.audit_events)
    assert adapter._ledger_state.equity==result.equity_curve[-1].marked_equity


def test_cashflow_during_pending_funding_is_unitized_after_truthful_reconciliation(tmp_path):
    portal,adapter,economic=pending_funding_fixture(tmp_path,delay=90,flow=True)
    result=run_replay(portal,adapter,broker())
    contribution=next(event for event in result.audit_events if event['type']=='funding_settlement')['payload']['contribution']
    expected=(D(10000)+D(contribution))/D(10000)
    assert abs(adapter._ledger_state.nav-expected)<D('1e-24')
    assert any(point.ts==economic for point in result.equity_curve)
    assert any(event['type']=='hrs_funding_valuation_reconciled' for event in result.audit_events)


def test_unproven_funding_blocks_final_qualification_but_native_stop_still_executes(tmp_path):
    portal,adapter,_=pending_funding_fixture(tmp_path,crash=True,missing_mark=True)
    b=broker()
    with pytest.raises(DataBlocked,match='funding'):
        run_replay(portal,adapter,b)
    assert b.closed_trades and b.closed_trades[0].exit_reason=='STOP_LOSS'


def test_explicit_full_tp1_override_uses_shared_risk_budget(tmp_path):
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    portal,original,_=_hrs_fixture(tmp_path)
    adapter=HrsAdapter(original._state_loader,experiment_id='full-tp1',
        exit_parameter_overrides={'standard':{'target1_close_percent':1,'target2_close_percent':0}})
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view()
    adapter.on_bar(portal.bars('A','1h')[-3],ctx)
    intents=adapter.intents(ctx)
    assert intents and intents[0].exit_ratios==(D(1),D(0))


def test_funding_mark_cannot_be_read_before_publication(tmp_path):
    portal,_,economic=pending_funding_fixture(tmp_path,delay=90)
    with pytest.raises(DataBlocked,match='funding_mark'):
        portal.funding_mark_at('A',economic,as_of=economic+timedelta(seconds=89))
    assert portal.funding_mark_at('A',economic,as_of=economic+timedelta(seconds=90))['price']==D(150)


def test_funding_receipt_after_window_never_qualifies_a_window(tmp_path):
    portal,adapter,_=pending_funding_fixture(tmp_path,delay=4*3600,crash=True)
    b=broker()
    with pytest.raises(DataBlocked,match='funding_pending'):
        run_replay(portal,adapter,b)
    assert b.closed_trades[0].exit_reason=='STOP_LOSS'


def test_cashflow_at_funding_receipt_uses_reconciled_units(tmp_path):
    portal,adapter,economic=pending_funding_fixture(tmp_path,delay=90,flow=True)
    flows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_cash_flows.jsonl').read_text().splitlines()]
    received_at=economic+timedelta(seconds=90)
    flows.append(dict(event_id='receipt-flow',amount='1000',occurred_at=received_at.isoformat(),
        observed_at=received_at.isoformat(),segment_id='test',verified=True))
    portal=refreeze(portal,{'hrs_cash_flows.jsonl':jsonl(flows)})
    result=run_replay(portal,adapter,broker())
    contribution=D(next(e for e in result.audit_events if e['type']=='funding_settlement')['payload']['contribution'])
    assert abs(adapter._ledger_state.nav-(D(10000)+contribution)/D(10000))<D('1e-24')
    assert len(adapter._ledger_state.cash_flows)==2


def test_delayed_funding_does_not_charge_partial_entry_fills_after_settlement():
    b=broker();model={'version':'hrs_1m_volume_v1','participation_rate':'.1','timeout_minutes':15}
    first=replace(bar(),interval='1m',open_time=AT,close_time=AT+timedelta(minutes=1),volume=D(10))
    b.submit((OrderIntent('hrs','A',1,'LONG','LIMIT',D(3),'ENTRY',trigger_price=D(100),limit_price=D(100),
        execution_model=model,expires_at=AT+timedelta(minutes=15)),),first,AT)
    b.on_bar('A',first,first.close_time,entry_ts=first.close_time)
    economic=AT+timedelta(seconds=90)
    key=b.begin_funding('A',economic)
    for minute in (2,3):
        later=replace(first,open_time=AT+timedelta(minutes=minute-1),close_time=AT+timedelta(minutes=minute))
        b.on_bar('A',later,later.close_time,entry_ts=later.close_time)
    assert b.positions['A'].current_quantity==3
    b.submit((OrderIntent('hrs','A',2,'LONG','MARKET',D(3),'TIME_STOP',reduce_only=True,
        reference_price=D(100),close_quantity=D(3)),),later,later.close_time)
    mark=dict(price=D(150),marked_at=economic,available_at=later.close_time,source='fixture')
    b.receive_funding(key,D('.01'),later.close_time,mark)
    assert b.closed_trades[0].funding_fee==D('-1.5')
    assert b.realized_equity==D('9998.5')


def test_replay_declares_software_limit_close_approximation(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    result=run_replay(portal,adapter,broker())
    assumptions=next(e['payload'] for e in result.audit_events if e['type']=='hrs_execution_assumptions')
    assert assumptions['configured_close_type']=='LIMIT'
    assert assumptions['software_close_model']=='observed_quote_market_proxy_v1'
    assert assumptions['exact_exchange_parity'] is False


def test_score_bar_boundary_is_frozen_before_delayed_batch_release(tmp_path,monkeypatch):
    from strategies.hrs import core_decision
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    row=json.loads((portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()[0])
    scored=AT+timedelta(hours=2,seconds=-5);released=AT+timedelta(hours=2,seconds=5)
    row.update(decision_at=scored.isoformat(),observed_at=scored.isoformat(),available_at=released.isoformat(),score_available_at=scored.isoformat())
    row['risk']['account_occupancy']['observed_at']=scored.isoformat()
    row['market'].update(source_observed_at=scored.isoformat(),expires_at=(released+timedelta(hours=1)).isoformat())
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl([row]),'hrs_decision_calendar.jsonl':jsonl([dict(symbol='A',decision_at=scored.isoformat(),available_at=released.isoformat(),batch_available_at=released.isoformat(),sequence=0)])})
    original=core_decision.analyze_standard;seen=[]
    def score(**kwargs):
        seen.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(core_decision,'analyze_standard',score)
    run_replay(portal,adapter,broker())
    assert seen[0]['decision_at']==scored
    assert max(k['open_time']+3600000 for k in seen[0]['klines_1h'])<=int(scored.timestamp()*1000)


def test_frozen_software_trailing_multiplier_survives_global_config_change(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True,entry_type='LIMIT',subbars=True,config_patch={'cycle':{'check_interval_seconds':60}})
    states=adapter._state_loader(AT)
    row=states['hrs_positions']['rows'][0]
    row.update(remaining_quantity='3',target1_reached=True,target2_reached=True)
    row['exit_parameters']['trailing_atr_multiplier']=3.5
    minutes=[json.loads(line) for line in (portal.snapshot_dir/'klines/A_1m.jsonl').read_text().splitlines()]
    minutes[0].update(open='100',high='110',low='99',close='110')
    minutes[1].update(open='110',high='110',low='106',close='106')
    # Continue above the legitimate 103 trailing stop; no retracement of 7.
    for minute in minutes[2:]:minute.update(open='106',high='106',low='106',close='106')
    portal=refreeze(portal,{'klines/A_1m.jsonl':jsonl(minutes)})
    result=run_replay(portal,adapter,broker())
    assert not any(t.position_id=='initial-1' and t.exit_reason=='TRAILING_STOP' and t.exit_time==AT+timedelta(minutes=2) for t in result.closed_trades)


def test_opening_equity_point_uses_restored_marks_and_actual_exposure(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True,entry_type='LIMIT',subbars=True)
    adapter._state_loader(AT)['hrs_positions']['rows'][0]['mark_price']='110'
    bootstrap=json.loads((portal.snapshot_dir/'hrs_replay_state.json').read_text())
    bootstrap['ledger']['history'][-1].update(equity='10100',nav='1.01')
    portal=refreeze(portal,{'hrs_replay_state.json':json.dumps(bootstrap)})
    opening=run_replay(portal,adapter,broker()).equity_curve[0]
    assert opening.marked_equity==D('10100')
    assert opening.realized_equity==D('10000') and opening.has_open_position is True


def test_hrs_market_matching_never_increases_the_risk_sized_quantity():
    b=broker()
    b.submit((OrderIntent('hrs','A',1,'LONG','MARKET',D('.05'),'ENTRY',reference_price=D(100)),),bar(),AT)
    minute=replace(bar(),open=D(50),high=D(51),low=D(49),close=D(50))
    b.on_bar('A',minute,minute.close_time)
    assert not b.positions or b.positions['A'].entry_quantity<=D('.05')
    assert any(e['type']=='reject' and e['payload']['reason']=='MIN_NOTIONAL' for e in b.audit)


def test_initial_full_tp1_frozen_plan_is_a_supported_position(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True)
    row=adapter._state_loader(AT)['hrs_positions']['rows'][0]
    row['exit_ratios']=['1','0'];row['exit_parameters'].update(target1_close_percent=1,target2_close_percent=0)
    ctx=adapter.prepare(portal);adapter.configure_execution(ctx)
    restored=adapter.initial_positions(ctx)
    assert restored[0]['position'].exit_ratios==(D(1),D(0))


def test_captured_score_history_is_used_when_a_new_bar_closes_before_capture_finishes(tmp_path,monkeypatch):
    from strategies.hrs import core_decision
    from ai_tuner.replay.adapters.hrs import _kline
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    raw=json.loads((portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()[0])
    at=AT+timedelta(hours=2,seconds=5)
    raw.update(decision_at=at.isoformat(),observed_at=at.isoformat(),score_available_at=at.isoformat())
    raw['risk']['account_occupancy']['observed_at']=at.isoformat()
    raw['market'].update(source_observed_at=at.isoformat(),expires_at=(at+timedelta(hours=1)).isoformat())
    raw['analysis_klines_1h']=[_kline(b) for b in portal.bars('A','1h') if b.close_time<=AT+timedelta(hours=1)]
    raw['analysis_klines_4h']=[_kline(b) for b in portal.bars('A','4h') if b.close_time<=at]
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl([raw]),'hrs_decision_calendar.jsonl':jsonl([dict(symbol='A',decision_at=at.isoformat(),available_at=at.isoformat(),batch_available_at=at.isoformat(),sequence=0)])})
    original=core_decision.analyze_standard;seen=[]
    def score(**kwargs):
        seen.append(kwargs);return original(**kwargs)
    monkeypatch.setattr(core_decision,'analyze_standard',score)
    run_replay(portal,adapter,broker())
    assert seen[0]['klines_1h']==raw['analysis_klines_1h']


def occupancy_rows(symbols,at):
    return dict(version='hrs_account_occupancy_v1',observed_at=at.isoformat(),other_positions=[
        dict(symbol=s,positionAmt='1',markPrice='100',leverage='2',strategy_id='btc_eth',ownership_evidence_sha256='a'*64) for s in symbols])


def test_account_slots_include_observed_other_strategies(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    rows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for row in rows:row['risk']['account_occupancy']=occupancy_rows(['BTCUSDT','BNBUSDT','ETHUSDT'],AT+timedelta(hours=rows.index(row)+1))
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(rows)})
    result=run_replay(portal,adapter,broker())
    assert result.final_position is None
    assert any(e['type']=='hrs_reject' and e['payload']['reason']=='position_slots_include_account_and_reservations' for e in result.audit_events)


def test_final_margin_guard_rejects_without_resizing_for_other_strategy_holdings(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    rows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for row in rows:
        row['risk']['entry_policy']['entry_ceiling']='2000'
        row['risk']['account_occupancy']=occupancy_rows(['BTCUSDT'],AT+timedelta(hours=rows.index(row)+1))
        row['risk']['account_occupancy']['other_positions'][0]['positionAmt']='30'
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(rows)})
    result=run_replay(portal,adapter,broker())
    assert result.final_position is None
    assert any(e['type']=='hrs_reject' and e['payload']['reason']=='entry_ceiling_includes_account_and_durable_reservations' for e in result.audit_events)


def test_full_fill_releases_pending_risk_but_retains_original_margin(tmp_path):
    from types import SimpleNamespace
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    b=broker();run_replay(portal,adapter,b)
    assert not adapter._reservations
    retained=adapter._durable_margin_reservations['A']
    assert retained['margin']==b.positions['A'].entry_quantity*D(100)/D(2)
    adapter._policy['entry_ceiling']='1500'
    reason=adapter._reservation_denial('B',b.positions['A'].entry_quantity,D(100),SimpleNamespace(portfolio=b.view()),
        portal.as_of,dict(account_occupancy=occupancy_rows([],portal.as_of)),D(1500))
    assert reason=='entry_ceiling_includes_account_and_durable_reservations'


def test_proven_full_close_releases_durable_margin_after_native_orders_clear(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    minutes=[json.loads(line) for line in (portal.snapshot_dir/'klines/A_1m.jsonl').read_text().splitlines()]
    minutes[61]['low']='94'
    portal=refreeze(portal,{'klines/A_1m.jsonl':jsonl(minutes)})
    result=run_replay(portal,adapter,broker())
    releases=[e for e in result.audit_events if e['type']=='hrs_margin_reservation_released']
    assert releases and releases[0]['ts']=='2026-11-17T01:02:00Z'
    assert releases[0]['payload']['reason']=='proven_virtual_flat_without_orders'


def test_missing_or_stale_account_occupancy_proof_blocks_new_risk(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    rows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for row in rows:row['risk']['account_occupancy']=occupancy_rows([],AT)
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(rows)})
    with pytest.raises(DataBlocked,match='account_occupancy'):
        run_replay(portal,adapter,broker())


def test_initial_filled_reservation_requires_terminal_proof_and_retains_original_margin(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True,entry_type='LIMIT',subbars=True)
    state=json.loads((portal.snapshot_dir/'hrs_replay_state.json').read_text())
    state['margin_reservations']=[dict(state='submitting',symbol='A',quantity='10',margin='500',
        terminal_order=dict(symbol='A',status='FILLED',executedQty='10',origQty='10'))]
    portal=refreeze(portal,{'hrs_replay_state.json':json.dumps(state)})
    run_replay(portal,adapter,broker())
    assert adapter._durable_margin_reservations['A']['margin']==D(500)


def test_current_hrs_position_and_two_other_symbols_fill_all_three_slots(tmp_path):
    from types import SimpleNamespace
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True,entry_type='LIMIT',subbars=True)
    b=broker();run_replay(portal,adapter,b)
    reason=adapter._reservation_denial('NEW',D(1),D(100),SimpleNamespace(portfolio=b.view()),portal.as_of,
        dict(account_occupancy=occupancy_rows(['BTCUSDT','BNBUSDT'],portal.as_of)),D(10000))
    assert reason=='position_slots_include_account_and_reservations'


def test_expired_captured_policy_denies_entries_without_renewing_validity(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    rows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for row in rows:
        row['risk']['entry_policy'].update(effective_at=AT.isoformat(),expires_at=(AT+timedelta(hours=1)).isoformat())
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(rows)})
    result=run_replay(portal,adapter,broker())
    assert result.final_position is None
    assert any(e['type']=='hrs_reject' and e['payload']['reason']=='risk_policy_expired' for e in result.audit_events)


def test_captured_policy_not_effective_yet_is_a_normal_denial(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    rows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for row in rows:row['risk']['entry_policy'].update(effective_at=(AT+timedelta(hours=2)).isoformat(),expires_at=(AT+timedelta(hours=4)).isoformat())
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(rows)})
    result=run_replay(portal,adapter,broker())
    assert result.final_position.entry_time==AT+timedelta(hours=2,minutes=1)
    assert any(e['type']=='hrs_reject' and e['payload']['reason']=='risk_policy_not_effective' for e in result.audit_events)


def test_missing_policy_timebounds_cannot_be_window_attested_implicitly(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    rows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for row in rows:
        row['risk']['entry_policy'].pop('effective_at',None);row['risk']['entry_policy'].pop('expires_at',None)
    portal=refreeze(portal,{'hrs_decision_inputs.jsonl':jsonl(rows)})
    with pytest.raises(DataBlocked,match='policy_timebounds'):
        run_replay(portal,adapter,broker())
