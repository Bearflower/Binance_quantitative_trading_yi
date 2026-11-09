from decimal import Decimal

import pytest

from strategies.grid.execution.contracts import ExecutionSettings


def test_executor_is_off_by_default():
    settings = ExecutionSettings.from_env({})
    assert settings.mode == "off"
    assert settings.symbol == "ETHUSDT"
    assert settings.use_unified_account is False


@pytest.mark.parametrize("missing", [
    "GRID_SUBACCOUNT_API_KEY",
    "GRID_SUBACCOUNT_API_SECRET",
    "GRID_MAX_MARGIN_USDT",
    "GRID_MAX_NOTIONAL_USDT",
    "GRID_MAX_DAILY_LOSS_USDT",
    "GRID_MAX_DRAWDOWN_USDT",
    "GRID_MAX_OPEN_ORDERS",
    "GRID_EXPECTED_LEVERAGE",
])
def test_live_requires_every_explicit_limit_and_credential(missing):
    values = {
        "GRID_EXECUTION_MODE": "live",
        "GRID_SUBACCOUNT_API_KEY": "subkey",
        "GRID_SUBACCOUNT_API_SECRET": "subsecret",
        "GRID_MAX_MARGIN_USDT": "100",
        "GRID_MAX_NOTIONAL_USDT": "500",
        "GRID_MAX_DAILY_LOSS_USDT": "10",
        "GRID_MAX_DRAWDOWN_USDT": "20",
        "GRID_MAX_OPEN_ORDERS": "12",
        "GRID_EXPECTED_LEVERAGE": "10",
    }
    values.pop(missing)
    with pytest.raises(ValueError, match=missing):
        ExecutionSettings.from_env(values)


def test_live_limits_are_positive_and_not_inferred_from_strategy_config():
    values = {
        "GRID_EXECUTION_MODE": "live",
        "GRID_SUBACCOUNT_API_KEY": "subkey",
        "GRID_SUBACCOUNT_API_SECRET": "subsecret",
        "GRID_MAX_MARGIN_USDT": "0",
        "GRID_MAX_NOTIONAL_USDT": "500",
        "GRID_MAX_DAILY_LOSS_USDT": "10",
        "GRID_MAX_DRAWDOWN_USDT": "20",
        "GRID_MAX_OPEN_ORDERS": "12",
        "GRID_EXPECTED_LEVERAGE": "10",
    }
    with pytest.raises(ValueError, match="GRID_MAX_MARGIN_USDT"):
        ExecutionSettings.from_env(values)
    values["GRID_MAX_MARGIN_USDT"] = "100"
    settings = ExecutionSettings.from_env(values)
    assert settings.max_margin_usdt == Decimal("100")
    assert settings.max_notional_usdt == Decimal("500")


def test_rejects_unknown_mode_and_symbol():
    with pytest.raises(ValueError, match="GRID_EXECUTION_MODE"):
        ExecutionSettings.from_env({"GRID_EXECUTION_MODE": "paper"})
    with pytest.raises(ValueError, match="GRID_SYMBOL"):
        ExecutionSettings.from_env({"GRID_SYMBOL": "BTCUSDT"})
