"""Entry intent is attribution only: preserve fills, costs and admission."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.btc_eth.replay.report import compute_metrics, _aggregate_positions
from tests.test_shadow_replay.test_round5_engine_integration import _make_engine_shell

AT = datetime(2026, 11, 25, 8, tzinfo=timezone.utc)
PROFILES = ('xau_original', 'xau_aggressive', 'xau_calibrated')


def engine_for(pid):
    engine = _make_engine_shell()
    if pid == 'xau_calibrated':
        for attr in ('states', 'replay_trades', 'pending_limit_orders', '_open_seq',
                     '_profile_max_margin', '_profile_max_lev'):
            mapping = getattr(engine, attr)
            mapping[pid] = deepcopy(mapping['xau_original'])
    engine.profile_ids = [pid]
    return engine


def fill_and_exit(engine, pid, feature):
    event = {'symbol': 'XAUUSDT', 'direction': 'LONG', 'grade': 'S',
             'reference_price': '2000', 'feature_snapshot': feature}
    before = deepcopy(event)
    engine._fill_entry(profile_id=pid, event=event, opened_at=AT, fill_at=AT,
                       fill_price=Decimal('2000'), quantity=Decimal('.03'),
                       margin=Decimal('20'), leverage=3, stop_loss=Decimal('1980'),
                       grade='S', order_kind='MARKET', atr=Decimal('10'),
                       position_id='test-position')
    engine._partial_close(pid, AT + timedelta(minutes=10), Decimal('2020'), 'TP1', Decimal('.009'))
    engine._close_position(pid, AT + timedelta(minutes=20), Decimal('1980'), 'FINAL_CLOSE')
    assert event == before


def metrics(engine, pid, candidates=None):
    state = engine.states[pid]
    return compute_metrics({
        'trades': engine.replay_trades[pid], 'state': {
            'final_equity': str(state.equity), 'realized_pnl': str(state.realized_pnl),
            'fee_paid': str(state.fee_paid), 'funding_fee': str(state.funding_fee),
            'slippage_cost': str(state.slippage_cost)},
        'equity_history': [], 'counterfactual': {},
    }, {'initial_equity': '300', 'invariant_violations': []}, decision_stats=candidates)


@pytest.mark.parametrize('pid', PROFILES)
@pytest.mark.parametrize('intent', ['trend', 'mean_reversion'])
def test_real_partial_and_final_close_preserve_intent_and_metrics(pid, intent):
    engine = engine_for(pid)
    fill_and_exit(engine, pid, {'entry_intent': intent, 'market_state': 'RANGING'})
    assert [t.get('entry_intent') for t in engine.replay_trades[pid]] == [intent, intent]
    assert _aggregate_positions(engine.replay_trades[pid])[0]['entry_intent'] == intent
    actual = metrics(engine, pid)
    group = actual['entry_intent_breakdown'][intent]
    assert group['n_closed_trades'] == group['n_filled_orders'] == 1
    assert group['fill_rate'] == 1
    for field in ('net_pnl', 'fee_paid', 'funding_fee', 'slippage_cost'):
        assert Decimal(group[field]) == Decimal(actual[field])
    legacy = engine_for(pid)
    fill_and_exit(legacy, pid, {'market_state': 'RANGING'})
    old = metrics(legacy, pid)
    assert {k: v for k, v in actual.items() if k != 'entry_intent_breakdown'} == {
        k: v for k, v in old.items() if k != 'entry_intent_breakdown'}
    assert old['entry_intent_breakdown']['unknown']['n_closed_trades'] == 1


@pytest.mark.parametrize('feature', [None, {}, {'entry_intent': []}, {'entry_intent': 'ranging'},
                                     {'market_state': 'STRONG_TREND'}])
def test_old_or_invalid_labels_never_inferred_from_market_state(feature):
    engine = engine_for('xau_original')
    fill_and_exit(engine, 'xau_original', feature)
    assert metrics(engine, 'xau_original')['entry_intent_breakdown']['unknown']['n_closed_trades'] == 1


def test_unfilled_and_candidate_counts_are_classified_without_counting_rejections_as_orders():
    pid = 'xau_original'
    engine = engine_for(pid)
    event = {'feature_snapshot': {'entry_intent': 'mean_reversion'}}
    order = {'position_id': None, 'opened_at': AT, 'limit_price': '2000', 'direction': 'LONG',
             'symbol': 'XAUUSDT', 'source_event': event}
    for reason in ('pending_at_cutoff', 'expired_unfilled', "rejected:['daily_trade_limit']"):
        engine._record_unfilled(pid, order, reason)
    stats = {'candidate': 3, 'entry_intent_candidates': {'mean_reversion': 3, 'trend': 0, 'unknown': 0}}
    result = metrics(engine, pid, stats)
    group = result['entry_intent_breakdown']['mean_reversion']
    assert group['n_candidates'] == 3
    assert group['n_total_virtual_orders'] == 2
    assert group['n_pending_at_cutoff'] == group['n_rejected_candidates'] == 1
    assert group['fill_rate'] == 0
    assert result['entry_intent_breakdown']['trend']['fill_rate'] is None


@pytest.mark.parametrize('pid', PROFILES)
def test_decision_summary_uses_only_explicit_candidate_intent(pid):
    engine = engine_for(pid)
    engine.events = [dict(profile_id=pid, scheduled_at=AT.isoformat(), decision=decision,
                          feature_snapshot=feature) for decision, feature in [
        ('candidate', {'entry_intent': 'trend'}), ('candidate', {'entry_intent': 'mean_reversion'}),
        ('candidate', {'market_state': 'RANGING'}), ('no_signal', {'entry_intent': 'trend'})]]
    before = deepcopy(engine.events)
    summary = engine._build_decision_summary()['per_profile'][pid]
    assert summary['entry_intent_candidates'] == {'trend': 1, 'mean_reversion': 1, 'unknown': 1}
    assert summary['candidate'] == 3
    assert engine.events == before


@pytest.mark.parametrize('calibrated', [False, True])
def test_reports_render_all_intents_without_independent_admission(tmp_path, calibrated):
    import json
    from pathlib import Path
    from backtest.btc_eth.replay.report import write_reports
    pid = 'xau_calibrated' if calibrated else 'xau_original'
    engine = engine_for(pid)
    fill_and_exit(engine, pid, {'entry_intent': 'trend'})
    state = engine.states[pid]
    profile = {'trades': engine.replay_trades[pid], 'state': {
        'final_equity': str(state.equity), 'realized_pnl': str(state.realized_pnl),
        'fee_paid': str(state.fee_paid), 'funding_fee': str(state.funding_fee),
        'slippage_cost': str(state.slippage_cost)}, 'counterfactual': {}, 'equity_history': []}
    meta = dict(replay_run_id='intent-test', engine_version='test', manifest_sha256='fixture',
                input_cutoff_at=AT.isoformat(), initial_equity='300', symbol_filters={},
                invariant_violations=[], profile_ids=[pid] if calibrated else list(PROFILES[:2]))
    result = {pid: profile, 'engine_metadata': meta}
    if not calibrated:
        result['xau_aggressive'] = deepcopy(profile)
    output = write_reports(result, {'run_id': 'intent-test', 'config_hashes': {p: 'frozen' for p in meta['profile_ids']}}, str(tmp_path),
                           str(Path('backtest/btc_eth/replay/config.yaml')),
                           data_completeness={'complete': False, 'gaps': 1})
    text = Path(output['md_path']).read_text()
    for label in ('趋势', '价值回归', '未知', '仅分组统计', '信号数', '手续费', '资金费', '滑点'):
        assert label in text
    assert '不可计算' in text
    report = json.loads(Path(output['json_path']).read_text())
    assert report[pid]['entry_intent_breakdown']['trend']['n_closed_trades'] == 1
    assert 'admission' not in report[pid]['entry_intent_breakdown']['trend']
    assert report['admission']['admission'] == 'insufficient_data'


def test_conflicting_exit_labels_are_wholly_unknown_without_double_counting():
    engine = engine_for('xau_original')
    fill_and_exit(engine, 'xau_original', {'entry_intent': 'trend'})
    engine.replay_trades['xau_original'][1]['entry_intent'] = 'mean_reversion'
    report = metrics(engine, 'xau_original')
    assert report['entry_intent_breakdown']['unknown']['n_closed_trades'] == 1
    assert sum(x['n_closed_trades'] for x in report['entry_intent_breakdown'].values()) == 1


@pytest.mark.parametrize('pid', PROFILES[:2])
@pytest.mark.parametrize('scenario', ['pending', 'filled', 'rejected'])
def test_real_run_carries_labels_through_order_lifecycle(tmp_path, pid, scenario):
    from tests.test_shadow_replay.test_round3_regressions import _make_real_engine
    engine = _make_real_engine(tmp_path)
    at = '2026-01-05T10:07:00+00:00'
    event = dict(profile_id=pid, symbol='XAUUSDT', scheduled_at=at, evaluated_at=at,
                 decision='candidate', direction='SHORT', grade='S', score=.9,
                 reference_price='2000', intended_entry_price='2100', intended_order_type='LIMIT',
                 feature_snapshot={'atr': 10, 'entry_intent': 'mean_reversion'})
    if scenario == 'filled':
        event.update(intended_order_type='MARKET', intended_entry_price='2000')
    if scenario == 'rejected':
        engine.states[pid].max_daily_trades = 0
    engine.events = [event]
    before = deepcopy(engine.events)
    result = engine.run()
    assert engine.events == before
    trades = result[pid]['trades']
    assert len(trades) == 1
    assert trades[0]['entry_intent'] == 'mean_reversion'
    if scenario == 'pending':
        assert trades[0]['unfilled_reason'] == 'pending_at_cutoff'
    elif scenario == 'filled':
        assert trades[0]['filled']
    else:
        assert trades[0]['unfilled_reason'].startswith('rejected:')
    stats = result['engine_metadata']['decision_summary']['per_profile'][pid]
    report = compute_metrics(result[pid], result['engine_metadata'], decision_stats=stats)
    groups = report['entry_intent_breakdown']
    assert groups['mean_reversion']['n_candidates'] == 1
    for key in ('n_total_virtual_orders', 'n_rejected_candidates', 'n_filled_orders', 'n_closed_trades', 'n_pending_at_cutoff'):
        assert sum(group[key] for group in groups.values()) == report[key]
    for key in ('net_pnl', 'fee_paid', 'funding_fee', 'slippage_cost'):
        assert sum(Decimal(group[key]) for group in groups.values()) == Decimal(report[key])
