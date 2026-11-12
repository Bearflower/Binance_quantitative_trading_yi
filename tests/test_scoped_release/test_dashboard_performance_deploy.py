import json
from unittest.mock import AsyncMock

import pytest

from scripts.select_deploy_mode import (
    DASHBOARD_PERFORMANCE_MARKER, DASHBOARD_PERFORMANCE_BASE,
    DeployModeError, _validate_dashboard_scope, select_deploy_mode,
)
from scripts.scoped_dashboard_performance_release import build_overlay
from scripts.render_remote_deploy import RenderError, _validate


def test_dashboard_marker_selects_only_reviewed_single_image_scope():
    changes = [('A', DASHBOARD_PERFORMANCE_MARKER),
               ('M', 'dashboard/backend/services/exchange_ledger.py'),
               ('A', 'shared/accounting/dashboard_performance.py')]
    assert select_deploy_mode('push', DASHBOARD_PERFORMANCE_BASE, changes) == 'dashboard-performance'
    _validate_dashboard_scope(DASHBOARD_PERFORMANCE_BASE, changes)
    with pytest.raises(DeployModeError):
        _validate_dashboard_scope(DASHBOARD_PERFORMANCE_BASE, changes +
                                  [('M', 'strategies/hrs/strategy.py')])
    with pytest.raises(DeployModeError):
        _validate_dashboard_scope('a' * 40, changes)
    _validate('dashboard-performance', 'a' * 40, '12345678', 'keep')
    with pytest.raises(RenderError):
        _validate('dashboard-performance', 'a' * 40, '12345678', 'rotate')


def test_dashboard_overlay_changes_only_dashboard_image():
    before = {'services': {'dashboard-api': {'image': 'old', 'environment': {'LEDGER_DERIVED_ATTRIBUTION_ENABLED': '1'}},
                           'hrs-strategy': {'image': 'hrs'}}}
    after = build_overlay(before, 'ghcr.io/bearflower/trading-dashboard-api@sha256:' + 'a' * 64)
    assert after['services']['hrs-strategy'] == before['services']['hrs-strategy']
    assert after['services']['dashboard-api']['environment'] == before['services']['dashboard-api']['environment']
    assert after['services']['dashboard-api']['image'] != before['services']['dashboard-api']['image']
    assert before['services']['dashboard-api']['image'] == 'old'


def test_failed_dashboard_acceptance_rolls_back_only_dashboard(tmp_path, monkeypatch):
    import scripts.scoped_dashboard_performance_release as module
    runtime = object.__new__(module.Runtime)
    runtime.overlay = tmp_path / 'new.json'
    runtime.previous_overlay = tmp_path / 'old.json'
    runtime.pin_file = tmp_path / 'pin.json'
    runtime.pin = {'sha': 'previous', 'status': 'active'}
    runtime.image = 'new-image'
    runtime.changed = []
    runtime.before = {'Id': 'old-id', 'Image': 'old-image',
                      'State': {'Status': 'running', 'StartedAt': 'old',
                                'Health': {'Status': 'healthy'}}}
    new = {'Id': 'new-id', 'Image': 'new-image',
           'State': {'Status': 'running', 'StartedAt': 'new',
                     'Health': {'Status': 'healthy'}}}
    commands = []
    runtime.compose = lambda _service, *, overlay: ['compose', str(overlay)]
    runtime.run = lambda args, **_: commands.append(args) or 'new-image'
    states = iter((new, runtime.before))
    runtime._healthy = lambda: next(states)
    runtime.record = lambda status, step: commands.append((status, step))
    monkeypatch.setattr(module, '_overview_ready', lambda: (_ for _ in ()).throw(
        module.ReleaseBlocked('metric gate failed')))
    with pytest.raises(module.ReleaseBlocked, match='metric gate failed'):
        runtime.activate()
    assert ['compose', str(runtime.overlay), 'up', '-d', '--no-deps', '--pull', 'never', 'dashboard-api'] in commands
    assert ['compose', str(runtime.previous_overlay), 'up', '-d', '--no-deps', '--pull', 'never', 'dashboard-api'] in commands
    assert ('failed', 'rolled_back') in commands
    assert runtime.pin_file.exists() is False


@pytest.mark.asyncio
async def test_dashboard_exchange_snapshot_only_reads_inventory(monkeypatch):
    from scripts import dashboard_release_exchange_snapshot as module
    client = type('Client', (), {})()
    client.get_position = AsyncMock(return_value=[{'symbol': 'ETHUSDT', 'positionSide': 'BOTH',
                                                    'positionAmt': '0.00000000', 'entryPrice': '0'}])
    client.get_open_orders = AsyncMock(return_value=[])
    client.get_open_algo_orders = AsyncMock(return_value=[])
    client.get_account_info = AsyncMock(return_value={'accountEquity': '500'})
    client.close = AsyncMock()
    monkeypatch.setattr(module, '_build_client_from_env', lambda: (client, 0))
    result = await module.snapshot()
    assert result['ok'] is True and result['read_only'] is True
    assert result['position_exposure'] == []
    client.close.assert_awaited_once()
