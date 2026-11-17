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
