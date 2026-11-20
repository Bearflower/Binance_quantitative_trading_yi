"""Exit experiments must execute the real replay and retain missing-data barriers."""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest


def module():
    from ai_tuner import hrs_exit_experiment
    return hrs_exit_experiment


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def registration():
    return {
        'schema_version': 1, 'experiment_id': 'hrs-exits-v1',
        'baseline': {'config': {}, 'effective_config_sha256': digest({}),
            'config_hashes': dict(base_config_hash='b'*64, params_manifest_hash='c'*64, overlay_hash='d'*64),
            'code_commit': 'a'*40, 'source': 'production_effective'},
        'candidates': [dict(candidate_id='baseline', mode='standard', exit_parameters={}),
                       dict(candidate_id='floor-4', mode='standard', exit_parameters={'min_absolute_percent': '.04'})],
        'windows': [
            dict(window_id='research', role='research', start='2026-09-01T00:00:00+08:00', end='2026-10-01T00:00:00+08:00'),
            dict(window_id='month1', role='holdout', start='2026-10-01T00:00:00+08:00', end='2026-11-01T00:00:00+08:00'),
            dict(window_id='month2', role='holdout', start='2026-11-01T00:00:00+08:00', end='2026-12-01T00:00:00+08:00')],
        'cost_model': dict(fee_bps='5', slippage_bps='2', stop_offset_pct='.001', tp_offset_pct='.0015', max_leverage='2'),
        'minimum_closed_lifecycles': 20,
    }


def test_missing_real_data_writes_blocked_bundle_without_candidate_scores(tmp_path):
    m = module()
    out = tmp_path/'blocked.json'
    result = m.run_experiment(registration(), snapshot_roots=[tmp_path/'absent'], output=out)
    assert result['status'] == 'BLOCKED_DATA'
    assert result['evaluations'] == [] and result['selected_candidates'] == {}
    assert 'historical_hrs_snapshots_missing' in result['blocked_reasons']
    assert json.loads(out.read_text())['registration_sha256']


@pytest.mark.parametrize('scoped',[False,True])
def test_registered_v2_admission_requires_complete_matching_snapshot_inputs(tmp_path,scoped):
    from .test_hrs_evidence_export import captured_sources
    from .replay.test_hrs_slot_scope import scoped_sources
    from .replay.test_hrs_wiring import AT
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    m=module();sources,bootstrap=(scoped_sources(tmp_path) if scoped else captured_sources(tmp_path))
    exported=export_evidence_bundle(sources,tmp_path/'out',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert exported.get('status')=='SEALED',exported
    r=registration();r['runtime_admission_scope']='hrs_owned_and_unreleased_reservations_v2'
    r['baseline']['config']=bootstrap['config'];r['baseline']['effective_config_sha256']=digest(bootstrap['config'])
    r['windows'][0].update(start=AT.isoformat(),end=(AT+timedelta(hours=3)).isoformat(),snapshot_dir=exported['snapshot_dir'])
    r['windows'][1].update(start='2026-12-01T00:00:00+08:00',end='2027-01-01T00:00:00+08:00')
    r['windows'][2].update(start='2027-01-01T00:00:00+08:00',end='2027-02-01T00:00:00+08:00')
    report=m.run_experiment(r,snapshot_roots=[exported['snapshot_dir']])
    assert report['runtime_admission_scope']==r['runtime_admission_scope']
    if scoped:
        assert report['engine_runs']==6,report['blocked_reasons']
        validation=report['evaluations'][0]['tiers']['baseline']['runtime_admission_scope_validation']
        assert validation==dict(status='VERIFIED',scope=r['runtime_admission_scope'],input_rows_checked=2)
    else:
        assert report['engine_runs']==0
        assert any('registered_runtime_admission_scope_mismatch' in reason for reason in report['blocked_reasons'])
    assert report['performance_accepted'] is False


@pytest.mark.parametrize('bad',['old','missing','null_proof'])
def test_registered_admission_checks_every_input_before_engine(tmp_path,bad):
    scope='hrs_owned_and_unreleased_reservations_v2'
    v2=dict(risk=dict(account_occupancy=dict(version='hrs_account_occupancy_v2',slot_scope=scope,
        slot_ownership_proof=dict(slot_scope=scope))))
    v1=dict(risk=dict(account_occupancy=dict(version='hrs_account_occupancy_v1')))
    if bad=='missing':v1=None
    elif bad=='null_proof':
        v1=copy.deepcopy(v2);v1['risk']['account_occupancy']['slot_ownership_proof']=None
    (tmp_path/'hrs_decision_inputs.jsonl').write_text(json.dumps(v2)+'\n'+json.dumps(v1)+'\n')
    from ai_tuner.replay.models import DataBlocked
    with pytest.raises(DataBlocked,match='registered_runtime_admission_scope_mismatch:input'):
        module()._registered_admission_validation(dict(runtime_admission_scope=scope),
            SimpleNamespace(snapshot_dir=tmp_path),dict(runtime_slot_scope=scope))
    r=registration();r['runtime_admission_scope']='unknown'
    with pytest.raises(ValueError,match='registered_runtime_admission_scope_unknown'):
        module().validate_registration(r)


def test_registration_bounds_one_factor_candidates_and_immutable_baseline():
    m = module()
    r = registration(); original = copy.deepcopy(r)
    m.validate_registration(r)
    assert r == original
    r['candidates'] += [dict(candidate_id=f'n{i}', mode='standard', exit_parameters={'atr_multiplier': str(i+1)}) for i in range(11)]
    with pytest.raises(ValueError, match='candidate_limit'):
        m.validate_registration(r)
    r = registration(); r['baseline']['config']['scoring'] = {'entry_threshold': 0}
    with pytest.raises(ValueError, match='baseline_config_hash'):
        m.validate_registration(r)
    r = registration(); r['candidates'][1]['exit_parameters'] = {'scoring.entry_threshold': 0}
    with pytest.raises(ValueError, match='unsupported_exit_parameter'):
        m.validate_registration(r)
    r = registration(); r['candidates'][1]['exit_parameters']['atr_multiplier'] = '2'
    with pytest.raises(ValueError, match='one_factor'):
        m.validate_registration(r)


def test_paired_tp_allocation_is_one_registered_factor_and_must_be_legal():
    m = module(); r = registration()
    r['candidates'][1].update(factor='tp_allocation', exit_parameters={'target1_close_percent': '1', 'target2_close_percent': '0'})
    m.validate_registration(r)
    r['candidates'][1]['exit_parameters']['target2_close_percent'] = '.4'
    with pytest.raises(ValueError, match='exit_allocation'):
        m.validate_registration(r)


def test_two_holdout_comparisons_require_confident_benefit_in_both_months():
    m = module(); rows = []
    for window in ('month1', 'month2'):
        for candidate, risk_return, dd, is_baseline in [('baseline', '.1', '.02', True), ('floor-4', '.2', '.01', False)]:
            tier = dict(nav={'nav_return': risk_return, 'max_drawdown': dd},
                lifecycles=dict(net_pnl_per_entry_risk=risk_return, worst_lifecycle_r=risk_return,
                    without_best_lifecycle_net_pnl='10', lifecycle_r=[risk_return]*20))
            rows.append(dict(mode='standard', candidate_id=candidate, window_id=window,
                             is_baseline=is_baseline, tiers={key: copy.deepcopy(tier) for key in ('baseline','1.5x','2.0x')}))
    assert m.compare_holdouts(rows, {'standard': 'floor-4'})[0]['stable_improvement']
    assert not m.compare_holdouts(rows[:2], {'standard': 'floor-4'})[0]['stable_improvement']
    for tier in rows[-1]['tiers'].values():
        tier['lifecycles']['lifecycle_r'] = ['-.5', '.7']*10
    assert not m.compare_holdouts(rows, {'standard': 'floor-4'})[0]['stable_improvement']


def test_holdouts_are_two_complete_nonoverlapping_cst_months_after_research():
    m = module()
    r = registration(); r['windows'][1]['start'] = '2026-09-30T23:59:59+08:00'
    with pytest.raises(ValueError, match='holdout_month'):
        m.validate_registration(r)
    r = registration(); r['windows'][0]['end'] = '2026-10-02T00:00:00+08:00'
    with pytest.raises(ValueError, match='chronological'):
        m.validate_registration(r)


def test_scan_aligned_month_is_distinct_and_requires_bounded_shared_windows():
    m=module();r=registration();policy=dict(version='first_scan_start_v1',max_delay_seconds=3600)
    for window in r['windows']:
        window.update(start_policy=policy,end_policy=policy)
        if window['role']=='holdout':
            window.update(observation_model='scan_aligned_month_v1',minimum_duration_seconds=28*86400)
    m.validate_registration(r)
    r['windows'][1]['minimum_duration_seconds']=27*86400
    with pytest.raises(ValueError,match='scan_aligned_month'):
        m.validate_registration(r)


def test_observed_month_boundary_qualification_preserves_actual_span_and_shared_capture():
    m=module();r=registration();window=r['windows'][1]
    window.update(observation_model='scan_aligned_month_v1',minimum_duration_seconds=28*86400)
    start=m._time(window['start'])+timedelta(minutes=21)
    end=m._time(window['end'])+timedelta(minutes=22)
    boundary=m._boundary_summary(window,start,end,{'selected_evidence_id':'a','selected_payload_sha256':'a'*64},
        {'selected_evidence_id':'b','selected_payload_sha256':'b'*64})
    assert boundary['monthly_window_eligible'] is True
    assert boundary['full_registered_span'] is False
    assert boundary['actual_duration_seconds']==31*86400+60
    assert boundary['qualification_model']=='scan_aligned_month_v1'
    assert m._boundary_summary(window,start,start+timedelta(days=27),{}, {})['monthly_window_eligible'] is False


def test_observed_months_must_share_exact_captured_boundary_with_research():
    m=module();r=registration()
    for w in r['windows'][1:]:w['observation_model']='scan_aligned_month_v1'
    def edge(identity):return dict(selected_evidence_id=identity,selected_payload_sha256=identity*64)
    previous=dict(actual_end='2026-10-01T00:21:00+08:00',end_resolution=edge('a'))
    first=dict(actual_start=previous['actual_end'],actual_end='2026-11-01T00:21:00+08:00',
        start_resolution=edge('a'),end_resolution=edge('b'))
    second=dict(actual_start=first['actual_end'],start_resolution=edge('b'))
    research={'window_boundaries':{'research':previous}}
    boundaries={'month1':first,'month2':second}
    assert m._shared_boundary_gaps(r,boundaries,research)==[]
    second['start_resolution']=edge('c')
    assert m._shared_boundary_gaps(r,boundaries,research)==['shared_scan_boundary_mismatch:month2']


def test_holdout_refuses_unfrozen_selection_before_touching_snapshots(tmp_path):
    m = module()
    result = m.run_experiment(registration(), snapshot_roots=[tmp_path], stage='holdout')
    assert result['status'] == 'BLOCKED_DATA'
    assert result['blocked_reasons'] == ['frozen_research_selection_required']
    assert result['evaluations'] == []


def test_holdout_rejects_research_report_from_a_different_registration(tmp_path):
    m = module(); r = registration()
    research = dict(stage='research', status='AVAILABLE', registration_sha256='f'*64)
    selection = dict(registration_sha256=m.fingerprint(r), research_report_sha256=m.fingerprint(research),
                     candidate_by_mode={'standard': 'floor-4'})
    result = m.run_experiment(r, snapshot_roots=[tmp_path], stage='holdout', selection=selection, research_report=research)
    assert result['blocked_reasons'] == ['frozen_research_selection_identity_invalid']


def test_invalid_registration_path_still_writes_a_blocked_result(tmp_path):
    m = module(); out = tmp_path/'out.json'
    assert m.main(['--registration', str(tmp_path/'missing.json'), '--output', str(out)]) == 2
    assert json.loads(out.read_text())['status'] == 'BLOCKED_DATA'


def test_nonobject_registration_fails_closed():
    result = module().run_experiment([])
    assert result['status'] == 'BLOCKED_DATA' and result['evaluations'] == []


def test_nav_metrics_use_cst_boundary_and_entry_risk_not_gross_deposits():
    m = module()
    start = datetime(2026, 10, 31, 16, tzinfo=timezone.utc)
    points = [SimpleNamespace(as_of=start, nav=D(1)),
              SimpleNamespace(as_of=start+timedelta(hours=8), nav=D('.98')),
              SimpleNamespace(as_of=start+timedelta(days=1), nav=D('1.01'))]
    result = m.nav_metrics(points, start, start+timedelta(days=1))
    assert result['daily_returns_cst'] == ['0.01']
    assert result['max_drawdown'] == '0.02'
    # Lifecycle scale is invariant to execution of 30/40/30 partial exits.
    trades = [SimpleNamespace(position_id='p', net_pnl=D(3), exit_time=start, direction='LONG', quantity=D(3)),
              SimpleNamespace(position_id='p', net_pnl=D(-1), exit_time=start, direction='LONG', quantity=D(7))]
    metrics = m.lifecycle_metrics(trades, {'p': D(10)}, minimum=1)
    assert metrics['closed_lifecycles'] == 1
    assert metrics['net_pnl_per_entry_risk'] == '0.2'
    assert metrics['without_best_lifecycle_net_pnl'] == '0'


def test_missing_entry_risk_is_not_a_zero_or_a_qualified_sample():
    m = module()
    trade = SimpleNamespace(position_id='p', net_pnl=D(3), exit_time=datetime.now(timezone.utc), direction='LONG', quantity=D(1))
    result = m.lifecycle_metrics([trade], {}, minimum=1)
    assert result['status'] == 'BLOCKED_DATA'
    assert result['net_pnl_per_entry_risk'] is None


def test_mode_sample_size_cannot_be_borrowed_from_another_mode():
    m = module()
    trades = [SimpleNamespace(position_id='s'), SimpleNamespace(position_id='e'), SimpleNamespace(position_id='l')]
    modes = {'s': 'standard', 'e': 'emm', 'l': 'lv_rm'}
    assert [t.position_id for t in m.select_mode_trades(trades, modes, 'standard')] == ['s', 'e']
    assert [t.position_id for t in m.select_mode_trades(trades, modes, 'lv_rm')] == ['l']


def test_cli_real_discovery_without_registration_cannot_report_winner(tmp_path):
    m = module(); out = tmp_path/'out.json'
    code = m.main(['--snapshot-root', str(tmp_path/'missing'), '--output', str(out)])
    result = json.loads(out.read_text())
    assert code == 2 and result['status'] == 'BLOCKED_DATA'
    assert result['selected_candidates'] == {} and result['evaluations'] == []


@pytest.mark.parametrize('first_scan_policy',[False,True])
def test_real_engine_executes_manifest_bound_raw_fixture_without_relabeling_history(tmp_path,first_scan_policy):
    from .replay.test_hrs_wiring import _hrs_fixture, AT
    m = module()
    portal, original_adapter, _ = _hrs_fixture(tmp_path, candidate_drop_after=2, config_patch={
        'trading': {'leverage': 2, 'order_type': {'entry': 'MARKET'},
                    'time_stop': {'max_holding_hours': 1, 'reanalyze': {'enabled': False}}}})
    raw = copy.deepcopy(portal.hrs_replay_state(portal.window_start))
    raw['state_tables'] = original_adapter._state_loader(portal.window_start)
    bootstrap_path = portal.snapshot_dir/'hrs_replay_state.json'
    bootstrap_path.write_text(json.dumps(raw))
    from ai_tuner.replay.data_portal import DataPortal, compute_manifest_sha256, sha256_file
    manifest_path = portal.snapshot_dir/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files']['hrs_replay_state.json']['sha256'] = sha256_file(str(bootstrap_path))
    manifest['sha256'] = compute_manifest_sha256(manifest['files'])
    manifest_path.write_text(json.dumps(manifest))
    portal = DataPortal(portal.snapshot_dir, 'hrs', portal.as_of, portal.window_start)
    r = registration(); r['baseline']['config'] = raw['config']
    r['baseline']['effective_config_sha256'] = digest(raw['config'])
    r['candidates'][1]['exit_parameters']['min_absolute_percent'] = '.06'
    r['windows'][0].update(start=AT.isoformat(), end=portal.as_of.isoformat(), snapshot_dir=str(portal.snapshot_dir))
    if first_scan_policy:
        from ai_tuner.hrs_evidence_export import resolve_scan_start
        from .test_hrs_evidence_export import envelope
        records=[envelope('opening','scan_start',{'fixture':True},AT),
                 envelope('opening','scan_end',{'status':'complete'},AT)]
        lower=AT-timedelta(minutes=21)
        policy=dict(version='first_scan_start_v1',max_delay_seconds=3600)
        r['windows'][0].update(start=lower.isoformat(),start_policy=policy)
        boundary=resolve_scan_start(records,lower,portal.as_of,policy)
        for name,data in [('raw_hrs_evidence.json',{'decision_evidence':records}),
                          ('hrs_export_receipt.json',{'boundary_resolution':boundary})]:
            path=portal.snapshot_dir/name;path.write_text(json.dumps(data))
            manifest['files'][name]={'path':name,'sha256':sha256_file(str(path))}
        manifest['sha256']=compute_manifest_sha256(manifest['files'])
        manifest_path.write_text(json.dumps(manifest))
        portal=DataPortal(portal.snapshot_dir,'hrs',portal.as_of,AT)
    # Research is deliberately before the holdout dates used by this fixture.
    r['windows'][1].update(start='2026-12-01T00:00:00+08:00', end='2027-01-01T00:00:00+08:00')
    r['windows'][2].update(start='2027-01-01T00:00:00+08:00', end='2027-02-01T00:00:00+08:00')
    result = m.run_experiment(r, snapshot_roots=[portal.snapshot_dir], stage='research')
    assert result['evaluations'] and result['engine_runs'] == 6, result['blocked_reasons']
    if first_scan_policy:
        assert result['evaluations'][0]['window_boundary']['delay_seconds']==1260
        assert result['evaluations'][0]['window_boundary']['full_registered_span'] is False
    assert result['evaluations'][0]['tiers']['baseline']['score_events'] > 0
    assert result['evaluations'][0]['tiers']['baseline']['source_manifest_sha256'] == portal.manifest_sha256
    reference = result['evaluations'][0]['tiers']['baseline']
    variant = result['evaluations'][1]['tiers']['baseline']
    assert reference['lifecycles']['net_pnl_per_entry_risk'] is not None
    assert reference['entry_budget_diagnostics'][0]['planned_stop'] != variant['entry_budget_diagnostics'][0]['planned_stop']
    assert result['performance_accepted'] is False
    assert result['selected_candidates'] == {}
    assert raw['config'] == r['baseline']['config']
    assert result['evaluations'][0]['candidate_sha256'] != result['evaluations'][1]['candidate_sha256']


def frozen_research(m,r,identity):
    research=dict(stage='research',status='AVAILABLE',registration_sha256=m.fingerprint(r))
    if identity is not None:research['engine_identity']=identity
    selection=dict(registration_sha256=m.fingerprint(r),research_report_sha256=m.fingerprint(research),candidate_by_mode={'standard':'floor-4'})
    return research,selection


def test_every_report_records_actual_runtime_source_and_environment_identity(tmp_path):
    m=module();report=m.run_experiment(registration(),snapshot_roots=[tmp_path/'missing'])
    identity=report['engine_identity']
    assert identity['status']=='AVAILABLE'
    for name in ('ai_tuner/replay/adapters/hrs.py','ai_tuner/replay/engine.py','ai_tuner/replay/broker.py',
                 'strategies/hrs/core_decision.py','strategies/hrs/scoring_engine.py','strategies/hrs/pattern.py',
                 'strategies/hrs/exit_geometry.py','strategies/hrs/risk_budget.py','strategies/hrs/risk_ledger.py',
                 'strategies/hrs/loss_gate.py','ai_tuner/replay/data_portal.py'):
        source=Path(m.__file__).resolve().parents[1]/name
        assert identity['source_files'][name]==hashlib.sha256(source.read_bytes()).hexdigest()
    assert identity['python']['version']
    assert 'structlog' in identity['dependencies']
    assert report['performance_accepted'] is False
    out=tmp_path/'missing-registration.json'
    m.main(['--output',str(out)])
    assert json.loads(out.read_text())['engine_identity']==identity


def test_holdout_without_research_runtime_identity_blocks_before_discovery_or_engine(tmp_path,monkeypatch):
    m=module();r=registration();research,selection=frozen_research(m,r,None)
    def forbidden(*args,**kwargs):raise AssertionError('holdout drift must block before discovery or engine')
    monkeypatch.setattr(m,'discover_snapshots',forbidden)
    monkeypatch.setattr(m,'run_replay',forbidden)
    result=m.run_experiment(r,snapshot_roots=[tmp_path],stage='holdout',selection=selection,research_report=research)
    assert result['status']=='BLOCKED_DATA'
    assert result['blocked_reasons']==['research_engine_identity_missing']
    assert result['engine_runs']==0


def test_holdout_source_or_dependency_drift_blocks_before_any_engine_run(tmp_path,monkeypatch):
    m=module();r=registration()
    old=m.engine_identity()
    research,selection=frozen_research(m,r,old)
    current=copy.deepcopy(old);current['dependencies']['structlog']='different-runtime'
    monkeypatch.setattr(m,'engine_identity',lambda:current)
    def forbidden(*args,**kwargs):raise AssertionError('runtime mismatch must block before replay')
    monkeypatch.setattr(m,'discover_snapshots',forbidden)
    monkeypatch.setattr(m,'run_replay',forbidden)
    result=m.run_experiment(r,snapshot_roots=[tmp_path],stage='holdout',selection=selection,research_report=research)
    assert result['status']=='BLOCKED_DATA'
    assert result['blocked_reasons']==['research_engine_identity_mismatch']
    assert result['engine_runs']==0 and result['engine_identity']==current


def test_matching_frozen_runtime_identity_passes_the_code_gate_only(tmp_path):
    m=module();r=registration();research,selection=frozen_research(m,r,m.engine_identity())
    result=m.run_experiment(r,snapshot_roots=[tmp_path/'missing'],stage='holdout',selection=selection,research_report=research)
    assert result['blocked_reasons']==['historical_hrs_snapshots_missing']
    assert result['performance_accepted'] is False and result['engine_runs']==0


def test_runtime_identity_detects_loaded_code_changes_even_if_source_bytes_are_unchanged(monkeypatch):
    from strategies.hrs import core_decision
    m=module();before=m.engine_identity()
    monkeypatch.setattr(core_decision,'analyze_standard',lambda **kwargs:None)
    after=m.engine_identity()
    assert before['source_files']==after['source_files']
    assert before['loaded_code_sha256']!=after['loaded_code_sha256']
    assert before['sha256']!=after['sha256']


def test_runtime_identity_binds_canonical_normalizer_globals(monkeypatch):
    from strategies.hrs import decision_capture
    m=module();before=m.engine_identity()
    original=decision_capture._normalize
    def changed(value):
        return float(value) if isinstance(value,D) else original(value)
    old_payload=decision_capture.canonical_payload(dict(volume=D('3')))[1]
    monkeypatch.setattr(decision_capture,'_normalize',changed)
    assert decision_capture.canonical_payload(dict(volume=D('3')))[1]!=old_payload
    after=m.engine_identity()
    assert before['sha256']!=after['sha256']


def test_unreadable_runtime_source_blocks_traceability_and_engine_execution(monkeypatch):
    m=module();original=m.inspect.getsourcefile
    monkeypatch.setattr(m.inspect,'getsourcefile',lambda item:None if item.__name__=='ai_tuner.replay.broker' else original(item))
    report=m.run_experiment(registration())
    assert report['status']=='BLOCKED_DATA' and report['engine_runs']==0
    assert report['engine_identity']['status']=='UNAVAILABLE'
    assert any('engine_source_unavailable:ai_tuner.replay.broker' in gap for gap in report['blocked_reasons'])


def test_engine_identity_binds_actual_scan_financial_context_code(monkeypatch):
    from strategies.hrs import decision_context
    m=module();original=m.engine_identity()
    assert 'strategies/hrs/decision_context.py' in original['source_files']
    assert 'strategies.hrs.decision_context' in original['loaded_code_sha256']
    async def changed_context(strategy):
        return {'different_financial_capture_contract': True}
    monkeypatch.setattr(decision_context,'capture_financial_context',changed_context)
    changed=m.engine_identity()
    assert changed['loaded_code_sha256']['strategies.hrs.decision_context'] != original['loaded_code_sha256']['strategies.hrs.decision_context']
    assert changed['sha256'] != original['sha256']


def test_engine_identity_binds_funding_inventory_normalization_code(monkeypatch):
    from strategies.hrs import funding_inventory
    m=module();before=m.engine_identity()
    assert 'strategies/hrs/funding_inventory.py' in before['source_files']
    def different_inventory_source(*args,**kwargs):return []
    monkeypatch.setattr(funding_inventory,'inventory_observations',different_inventory_source)
    after=m.engine_identity()
    assert before['source_files']==after['source_files']
    assert before['loaded_code_sha256']!=after['loaded_code_sha256']
    assert before['sha256']!=after['sha256']
