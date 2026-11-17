from copy import deepcopy
import importlib.util
from pathlib import Path


def module():
    path = Path('release/hrs-execution/deploy_optimization.py')
    spec = importlib.util.spec_from_file_location('hrs_release', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_overlay_preserves_other_pins_and_actual_frozen_mounts():
    mod = module()
    previous = {'services': {'hrs-strategy': {'image': 'old', 'environment': {'OLD': '1'}},
                             'btc-eth-aggressive': {'image': 'other-pinned'}}}
    before = deepcopy(previous)
    mounts = [{'Type': 'bind', 'Source': '/root/frozen/config.yaml',
               'Destination': '/app/strategies/hrs/config.yaml', 'RW': False}]
    overlay = mod.release_overlay(previous, 'hrs-image', 'hub-image', mounts)
    assert previous == before
    assert overlay['services']['btc-eth-aggressive']['image'] == 'other-pinned'
    assert overlay['services']['hrs-strategy']['volumes'][0]['read_only'] is True
    assert overlay['services']['hrs-strategy']['environment']['OLD'] == '1'
    assert overlay['services']['hrs-strategy']['environment']['ENTRY_EQUITY_GATE_STRATEGIES'] == 'hrs'


def test_reporter_role_has_no_trading_permissions_or_api_credentials():
    mod = module()
    sql = mod.reader_grants('trading_platform')
    assert 'INSERT' not in sql and 'UPDATE' not in sql and 'DELETE' not in sql
    assert 'ALL TABLES' not in sql
    assert 'hrs.hrs_decision_evidence' in sql
    assert 'trading.execution_fact_income' in sql
