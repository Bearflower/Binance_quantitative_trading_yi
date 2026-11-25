"""Durable replay evidence survives a fresh interpreter, including attribution."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from backtest.btc_eth.replay.report import write_reports
from tests.test_shadow_replay.test_round3_regressions import _make_real_engine


SOURCE_KEYS = ('run_id', 'profile_id', 'symbol', 'scheduled_at', 'config_hash', 'git_commit')


def run_fixture(tmp_path, pid='xau_original', scenario='filled', intent='mean_reversion'):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = _make_real_engine(tmp_path)
    if pid == 'xau_calibrated':
        import yaml
        from shared.config_loader import compute_config_hash
        from backtest.btc_eth.replay.engine import ReplayEngine
        from tests.test_shadow_replay.test_round3_regressions import _make_minimal_fixture
        config = Path('strategies/btc_eth/shadow/config/xau_calibrated.yaml')
        manifest = dict(engine.run_manifest, symbol='XAUUSDT', config_hashes={
            pid: compute_config_hash(yaml.safe_load(config.read_text()))})
        engine = ReplayEngine(
            replay_config_path='backtest/btc_eth/replay/config.yaml',
            profile_config_path_original='', profile_config_path_aggressive='',
            profile_config_path_calibrated=str(config), run_manifest=manifest,
            **_make_minimal_fixture(tmp_path))
    at = '2026-01-05T10:07:00+00:00'
    event = dict(run_id='physical-run-before-restart', profile_id=pid, symbol='XAUUSDT',
                 scheduled_at=at, evaluated_at=at, config_hash=engine.run_manifest['config_hashes'][pid],
                 git_commit='collector-commit', decision='candidate', direction='SHORT', grade='S',
                 reference_price='2000', intended_entry_price='2100', intended_order_type='LIMIT',
                 feature_snapshot={'atr': 10, 'entry_intent': intent})
    if scenario == 'filled':
        event.update(intended_order_type='MARKET', intended_entry_price='2000')
    elif scenario == 'rejected':
        engine.states[pid].max_daily_trades = 0
    elif scenario == 'expired':
        event.update(scheduled_at='2026-01-05T10:00:00+00:00', evaluated_at='2026-01-05T10:00:00+00:00')
    engine.events = [event]
    return engine, event, engine.run()


@pytest.mark.parametrize('pid', ['xau_original', 'xau_aggressive', 'xau_calibrated'])
@pytest.mark.parametrize('scenario', ['filled', 'pending', 'expired', 'rejected'])
def test_real_run_durable_source_identity_and_restart(tmp_path, pid, scenario):
    engine, event, result = run_fixture(tmp_path / 'inputs', pid, scenario)
    outputs = write_reports(result, engine.run_manifest, str(tmp_path / 'reports'))
    assert 'trade_ledger' in outputs
    reference = outputs['trade_ledger']
    path = Path(reference['path'])
    assert path.name == f"trades_{result['engine_metadata']['replay_run_id']}.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference['sha256']
    program = ('import json,sys; from backtest.btc_eth.replay.ledger import read_trade_ledger; '
               'print(json.dumps(read_trade_ledger(sys.argv[1], sys.argv[2])))')
    saved = json.loads(subprocess.check_output([sys.executable, '-c', program, str(path), reference['sha256']], text=True))
    assert saved['schema_version'] == 1
    assert saved['engine_version'] == result['engine_metadata']['engine_version']
    assert saved['replay_run_id'] == result['engine_metadata']['replay_run_id']
    assert saved['input_identity'] == result['engine_metadata']['input_identity']
    assert saved['run_manifest'] == engine.run_manifest
    assert set(saved['profiles']) == set(engine.profile_ids)
    assert saved['profiles'][pid] == result[pid]['trades']
    trade = saved['profiles'][pid][0]
    assert trade['source_decision'] == {key: event[key] for key in SOURCE_KEYS}
    assert trade['entry_intent'] == 'mean_reversion'
    assert len(saved['profiles'][pid]) == 1
    if scenario == 'filled':
        assert trade['filled'] and trade['exit_reason']
    else:
        reason = trade['unfilled_reason']
        assert reason.startswith('rejected:') if scenario == 'rejected' else reason == {
            'pending': 'pending_at_cutoff', 'expired': 'expired_unfilled'}[scenario]
    report = json.loads(Path(outputs['json_path']).read_text())
    assert report['trade_ledger'] == reference
    markdown = Path(outputs['md_path']).read_text()
    assert path.name in markdown and reference['sha256'] in markdown
    first_bytes = path.read_bytes()
    first_stat = path.stat()
    assert write_reports(result, engine.run_manifest, str(tmp_path / 'reports'))['trade_ledger'] == reference
    assert path.read_bytes() == first_bytes and path.stat().st_mtime_ns == first_stat.st_mtime_ns


def test_changed_inputs_keep_old_ledger_even_when_summary_filename_reused(tmp_path):
    engine, _, first = run_fixture(tmp_path / 'input1', intent='trend')
    other_engine, _, second = run_fixture(tmp_path / 'input2', intent='mean_reversion')
    assert first['engine_metadata']['replay_run_id'] != second['engine_metadata']['replay_run_id']
    out1 = write_reports(first, engine.run_manifest, str(tmp_path / 'reports'))
    raw1 = Path(out1['trade_ledger']['path']).read_bytes()
    out2 = write_reports(second, other_engine.run_manifest, str(tmp_path / 'reports'))
    assert out1['json_path'] == out2['json_path']
    assert Path(out1['trade_ledger']['path']).read_bytes() == raw1
    assert out1['trade_ledger']['path'] != out2['trade_ledger']['path']


def test_conflicting_output_or_corrupted_ledger_fails_closed(tmp_path):
    engine, _, result = run_fixture(tmp_path / 'input')
    output = write_reports(result, engine.run_manifest, str(tmp_path / 'reports'))
    assert 'trade_ledger' in output
    from backtest.btc_eth.replay.ledger import read_trade_ledger
    changed = deepcopy(result)
    changed['xau_original']['trades'][0]['entry_intent'] = 'trend'
    with pytest.raises(ValueError, match='conflict'):
        write_reports(changed, engine.run_manifest, str(tmp_path / 'reports'))
    ref = output['trade_ledger']
    path = Path(ref['path'])
    # Simulate external corruption (writers never replace an existing ledger).
    path.chmod(0o600)
    path.write_text('{}')
    with pytest.raises(ValueError, match='SHA-256'):
        read_trade_ledger(path, ref['sha256'])
    with pytest.raises(ValueError, match='conflict'):
        write_reports(result, engine.run_manifest, str(tmp_path / 'reports'))


def test_fresh_process_rerun_is_byte_identical_and_exclusive(tmp_path):
    engine, _, result = run_fixture(tmp_path / 'input')
    output = write_reports(result, engine.run_manifest, str(tmp_path / 'reports'))
    reference = output['trade_ledger']
    original = Path(reference['path']).read_bytes()
    program = (
        'import json,sys; from pathlib import Path; '
        'from tests.test_shadow_replay.test_durable_ledger import run_fixture; '
        'from backtest.btc_eth.replay.report import write_reports; '
        'engine,event,result=run_fixture(Path(sys.argv[1])); '
        'print(json.dumps(write_reports(result,engine.run_manifest,sys.argv[2])))')
    # Two separate processes publish the same replay concurrently. Both must
    # observe the complete original ledger and must never replace its inode.
    inode = Path(reference['path']).stat().st_ino
    processes = [subprocess.Popen([sys.executable, '-c', program, str(tmp_path / f'input{i}'),
                                  str(tmp_path / 'reports')], stdout=subprocess.PIPE, text=True)
                 for i in range(2)]
    for process in processes:
        stdout, _ = process.communicate(timeout=30)
        assert process.returncode == 0
        assert json.loads(stdout)['trade_ledger'] == reference
    assert Path(reference['path']).read_bytes() == original
    assert Path(reference['path']).stat().st_ino == inode
    assert not list((tmp_path / 'reports').glob('.trade-ledger-*'))


def test_partial_exit_legs_keep_actual_source_without_fabricated_history(tmp_path):
    from datetime import timedelta
    from decimal import Decimal
    from tests.test_shadow_replay.test_entry_intent import AT, engine_for
    from backtest.btc_eth.replay.ledger import write_trade_ledger, read_trade_ledger
    for pid in ('xau_original', 'xau_aggressive', 'xau_calibrated'):
        engine = engine_for(pid)
        event = {'run_id': 'collector-run', 'profile_id': pid, 'symbol': 'XAUUSDT',
                 'scheduled_at': AT.isoformat(), 'direction': 'LONG', 'grade': 'S',
                 'reference_price': '2000', 'feature_snapshot': {'entry_intent': 'trend'}}
        engine._fill_entry(profile_id=pid, event=event, opened_at=AT, fill_at=AT,
                           fill_price=Decimal('2000'), quantity=Decimal('.03'),
                           margin=Decimal('20'), leverage=3, stop_loss=Decimal('1980'),
                           grade='S', order_kind='MARKET', atr=Decimal('10'), position_id='split')
        engine._partial_close(pid, AT + timedelta(minutes=10), Decimal('2020'), 'TP1', Decimal('.009'))
        engine._close_position(pid, AT + timedelta(minutes=20), Decimal('1980'), 'FINAL_CLOSE')
        result = {'engine_metadata': {'replay_run_id': pid, 'engine_version': 'test'},
                  pid: {'trades': engine.replay_trades[pid]}}
        ref = write_trade_ledger(result, {}, str(tmp_path))
        rows = read_trade_ledger(ref['path'], ref['sha256'])['profiles'][pid]
        assert [row['exit_reason'] for row in rows] == ['TP1', 'FINAL_CLOSE']
        for row in rows:
            assert row['entry_intent'] == 'trend'
            assert row['source_decision'] == {key: event[key] for key in SOURCE_KEYS[:4]}
            assert 'git_commit' not in row['source_decision']
            assert 'config_hash' not in row['source_decision']
