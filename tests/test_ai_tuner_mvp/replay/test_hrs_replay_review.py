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


@pytest.mark.parametrize('mark,offset',[(False,0),(True,1)])
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
