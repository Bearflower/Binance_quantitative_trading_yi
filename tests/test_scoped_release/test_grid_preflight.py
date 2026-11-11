import pytest

from scripts.preflight_grid_scoped_release import (GateError, require_execution_disabled,
                                                  validate_delta)


def _config(image, *, handoff=False, other=None):
    env = {"STRATEGY_NAME": "grid"}
    if handoff:
        env["GRID_EXECUTION_HANDOFF_DB"] = "/app/data/grid_execution.sqlite3"
    return {"services": {
        "grid-strategy": {"image": image, "environment": env, "command": ["python"]},
        "btc-eth-strategy": {"image": other or "old-btc", "environment": {}},
    }}


def test_only_grid_image_and_handoff_are_accepted():
    validate_delta(_config("old-grid"), _config("new-grid", handoff=True))


@pytest.mark.parametrize("after", [
    _config("new-grid", handoff=True, other="new-btc"),
    _config("new-grid"),
    {"services": {"grid-strategy": {"image": "new-grid", "environment": {}}}},
])
def test_unreviewed_compose_change_blocks(after):
    with pytest.raises(GateError):
        validate_delta(_config("old-grid"), after)


@pytest.mark.parametrize("env_text,process_env", [
    ('GRID_EXECUTION_MODE = "live"', {}),
    ("GRID_EXECUTOR_DEPLOY_ENABLED=true", {}),
    ("GRID_EXECUTION_MODE=off", {"GRID_EXECUTION_MODE": "live"}),
])
def test_live_setting_blocks_even_with_whitespace_or_process_override(env_text, process_env):
    with pytest.raises(GateError):
        require_execution_disabled(env_text, process_env)
