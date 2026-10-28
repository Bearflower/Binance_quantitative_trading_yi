"""资格考试门禁：缺数据优先阻断，未校准阈值不产生 PASS。"""

import pytest

from ai_tuner.qualification.gates import evaluate_gates


def _thresholds():
    return {
        "min_data_completeness": "1.0",
        "future_data_allow_zero": True,
        "golden_parity_required": True,
        "max_drawdown_pct": "15",
        "min_effective_signals": 100,
        "min_blind_trades": 10,
        "adjacent_perturb_max_dd_delta_pct": "3",
        "max_cost_1_5x_capital_loss_pct": "30",
        "max_single_trade_concentration_pct": "40",
        "config_version_required": True,
    }


def _actuals():
    return {
        "data_completeness": "1.0",
        "future_data_accesses": 0,
        "parity_status": "match",
        "max_drawdown_pct": "10",
        "effective_signals": 120,
        "blind_trades": 20,
        "adjacent_dd_delta_pct": "1",
        "max_capital_loss_pct_1_5x": "5",
        "single_trade_concentration_pct": "20",
        "config_compatible": True,
    }


def test_all_ten_gates_pass_with_complete_evidence():
    result = evaluate_gates(_actuals(), _thresholds())
    assert result["status"] == "PASS"
    assert len(result["gates"]) == 10
    assert all(item["pass"] for item in result["gates"].values())


@pytest.mark.parametrize("key", [
    "min_blind_trades", "adjacent_perturb_max_dd_delta_pct",
    "max_single_trade_concentration_pct",
])
def test_uncalibrated_threshold_blocks(key):
    thresholds = _thresholds()
    del thresholds[key]
    result = evaluate_gates(_actuals(), thresholds)
    assert result["status"] == "BLOCKED_DATA"
    assert key in " ".join(result["blocked_reasons"])


def test_insufficient_signals_are_blocked_not_failed():
    actuals = _actuals()
    actuals["effective_signals"] = 37
    result = evaluate_gates(actuals, _thresholds())
    assert result["status"] == "BLOCKED_DATA"
    assert result["gates"]["minimum_sample"]["pass"] is False


def test_partial_parity_blocks_even_when_drawdown_fails():
    actuals = _actuals()
    actuals["parity_status"] = "partial_parity"
    actuals["max_drawdown_pct"] = "20"
    result = evaluate_gates(actuals, _thresholds())
    assert result["status"] == "BLOCKED_DATA"
    assert "max_drawdown" in result["elimination_reasons"]


def test_real_drawdown_failure_is_fail():
    actuals = _actuals()
    actuals["max_drawdown_pct"] = "20"
    result = evaluate_gates(actuals, _thresholds())
    assert result["status"] == "FAIL"


def test_future_data_access_is_fail():
    actuals = _actuals()
    actuals["future_data_accesses"] = 1
    result = evaluate_gates(actuals, _thresholds())
    assert result["status"] == "FAIL"


def test_future_data_access_overrides_other_blocked_gates():
    actuals = _actuals()
    actuals["future_data_accesses"] = 1
    del actuals["effective_signals"]
    result = evaluate_gates(actuals, _thresholds())
    assert result["status"] == "FAIL"
    assert "no_future_data" in result["elimination_reasons"]


def test_missing_gate_maps_block_instead_of_throwing():
    assert evaluate_gates(None, None)["status"] == "BLOCKED_DATA"


def test_missing_actual_is_blocked():
    actuals = _actuals()
    del actuals["max_capital_loss_pct_1_5x"]
    result = evaluate_gates(actuals, _thresholds())
    assert result["status"] == "BLOCKED_DATA"
    assert "actual_missing:max_capital_loss_pct_1_5x" in result["blocked_reasons"]


@pytest.mark.parametrize("loss, expected_status", [
    ("29.999", "PASS"),
    ("30.000", "FAIL"),
    ("30.001", "FAIL"),
])
def test_cost_gate_uses_strict_loss_boundary(loss, expected_status):
    actuals = _actuals()
    actuals["max_capital_loss_pct_1_5x"] = loss
    result = evaluate_gates(actuals, _thresholds())
    assert result["status"] == expected_status
    assert result["gates"]["cost_1_5x"]["pass"] is (expected_status == "PASS")


@pytest.mark.parametrize("threshold", ["0", "-1", "100", "101", "NaN", "Infinity", "bad"])
def test_cost_gate_rejects_out_of_range_or_nonfinite_threshold(threshold):
    thresholds = _thresholds()
    thresholds["max_cost_1_5x_capital_loss_pct"] = threshold
    result = evaluate_gates(_actuals(), thresholds)
    assert result["status"] == "BLOCKED_DATA"
    assert "threshold_invalid:max_cost_1_5x_capital_loss_pct" in result["blocked_reasons"]


@pytest.mark.parametrize("loss", ["-0.001", "100.001", "NaN", "Infinity", "bad"])
def test_cost_gate_rejects_out_of_range_or_nonfinite_actual(loss):
    actuals = _actuals()
    actuals["max_capital_loss_pct_1_5x"] = loss
    result = evaluate_gates(actuals, _thresholds())
    assert result["status"] == "BLOCKED_DATA"
    assert "actual_invalid:max_capital_loss_pct_1_5x" in result["blocked_reasons"]


def test_legacy_cost_keys_alone_do_not_satisfy_gate():
    thresholds = _thresholds()
    actuals = _actuals()
    del thresholds["max_cost_1_5x_capital_loss_pct"]
    del actuals["max_capital_loss_pct_1_5x"]
    thresholds["cost_1_5x_capital_preservation_pct"] = "30"
    actuals["cost_1_5x_capital_preservation_pct"] = "95"
    result = evaluate_gates(actuals, thresholds)
    assert result["status"] == "BLOCKED_DATA"
    assert "threshold_missing:max_cost_1_5x_capital_loss_pct" in result["blocked_reasons"]
