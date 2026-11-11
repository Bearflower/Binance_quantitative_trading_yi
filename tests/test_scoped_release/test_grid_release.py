import pytest

from scripts.render_remote_deploy import _validate
from scripts.scoped_grid_release import disabled, expected_grid_delta
from scripts.select_deploy_mode import (
    DeployModeError, GRID_PM_BASE, GRID_PM_MARKER, _validate_grid_scope,
    select_deploy_mode,
)


def test_grid_release_is_marker_scoped_and_funding_static():
    assert select_deploy_mode("push", GRID_PM_BASE, [("A", GRID_PM_MARKER)]) == "grid-pm-oneway"
    _validate("grid-pm-oneway", "a" * 40, "1234ABCD", "keep")
    with pytest.raises(ValueError):
        _validate("grid-pm-oneway", "a" * 40, "1234ABCD", "rotate")
    with pytest.raises(DeployModeError):
        _validate_grid_scope(GRID_PM_BASE, [("A", GRID_PM_MARKER), ("M", "ai_tuner/main.py")])


def test_grid_release_accepts_only_image_and_handoff(monkeypatch):
    monkeypatch.delenv("GRID_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("GRID_EXECUTOR_DEPLOY_ENABLED", raising=False)
    assert disabled("GRID_EXECUTION_MODE=off\n")
    monkeypatch.setenv("GRID_EXECUTION_MODE", "live")
    assert not disabled("GRID_EXECUTION_MODE=off\n")
    before = {"image": "old", "environment": {"STRATEGY_NAME": "grid"}, "command": ["python"]}
    after = {"image": "new", "environment": {"STRATEGY_NAME": "grid",
                                               "GRID_EXECUTION_HANDOFF_DB": "/app/data/grid_execution.sqlite3"},
             "command": ["python"]}
    assert expected_grid_delta(before, after)
    assert not expected_grid_delta(before, dict(after, command=["sh"]))
    assert not expected_grid_delta(before, dict(after, environment={"STRATEGY_NAME": "other"}))
