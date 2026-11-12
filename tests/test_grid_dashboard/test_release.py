from pathlib import Path

import pytest

from scripts.scoped_grid_dashboard_release import build_overlay, ReleaseBlocked
from scripts.select_deploy_mode import (GRID_DASHBOARD_BASE, GRID_DASHBOARD_MARKER,
                                        DeployModeError, _validate_grid_dashboard_scope,
                                        select_deploy_mode)
from scripts.render_remote_deploy import _validate, RenderError


def test_scoped_mode_accepts_only_reviewed_grid_dashboard_paths():
    changes = [('A', GRID_DASHBOARD_MARKER),
               ('A', 'strategies/grid/execution/dashboard_snapshot.py'),
               ('M', 'dashboard/frontend/index.html')]
    assert select_deploy_mode('push', GRID_DASHBOARD_BASE, changes) == 'grid-subaccount-dashboard'
    _validate_grid_dashboard_scope(GRID_DASHBOARD_BASE, changes)
    with pytest.raises(DeployModeError):
        _validate_grid_dashboard_scope(GRID_DASHBOARD_BASE, changes +
                                       [('M', 'strategies/grid/execution/main.py')])
    _validate('grid-subaccount-dashboard', 'a' * 40, '12345678', 'keep')
    with pytest.raises(RenderError):
        _validate('grid-subaccount-dashboard', 'a' * 40, '12345678', 'rotate')


def test_grid_snapshot_overlay_adds_only_read_only_dashboard_mount():
    old = {'services': {'dashboard-api': {'image': 'old'},
                        'grid-strategy': {'image': 'grid'}}}
    new = build_overlay(old, 'ghcr.io/bearflower/trading-dashboard-api@sha256:' + 'a' * 64,
                        Path('/root/trading_system/data/grid-dashboard'))
    assert old['services']['dashboard-api'] == {'image': 'old'}
    assert new['services']['grid-strategy'] == old['services']['grid-strategy']
    assert new['services']['dashboard-api']['volumes'] == [
        '/root/trading_system/data/grid-dashboard:/app/grid-dashboard:ro']
    with pytest.raises(ReleaseBlocked):
        build_overlay({'services': {'dashboard-api': {'image': 'old', 'volumes': ['rw']}}},
                      'ghcr.io/bearflower/trading-dashboard-api@sha256:' + 'a' * 64,
                      Path('/root/trading_system/data/grid-dashboard'))
