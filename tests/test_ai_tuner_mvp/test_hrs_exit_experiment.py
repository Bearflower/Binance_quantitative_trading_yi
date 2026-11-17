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


def test_real_engine_executes_manifest_bound_raw_fixture_without_relabeling_history(tmp_path):
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
    # Research is deliberately before the holdout dates used by this fixture.
    r['windows'][1].update(start='2026-12-01T00:00:00+08:00', end='2027-01-01T00:00:00+08:00')
    r['windows'][2].update(start='2027-01-01T00:00:00+08:00', end='2027-02-01T00:00:00+08:00')
    result = m.run_experiment(r, snapshot_roots=[portal.snapshot_dir], stage='research')
    assert result['evaluations'] and result['engine_runs'] == 6, result['blocked_reasons']
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
