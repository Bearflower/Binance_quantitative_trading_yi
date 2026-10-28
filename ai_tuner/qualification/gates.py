"""十项硬门禁：缺数据或缺校准值优先归 BLOCKED_DATA。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional


GATE_SPECS = (
    ("data_completeness", "min_data_completeness", "data_completeness", "min", "block"),
    ("no_future_data", "future_data_allow_zero", "future_data_accesses", "zero", "fail"),
    ("golden_parity", "golden_parity_required", "parity_status", "match", "fail"),
    ("max_drawdown", "max_drawdown_pct", "max_drawdown_pct", "max", "fail"),
    ("minimum_sample", "min_effective_signals", "effective_signals", "min", "block"),
    ("blind_sample", "min_blind_trades", "blind_trades", "min", "block"),
    ("adjacent_perturbation", "adjacent_perturb_max_dd_delta_pct", "adjacent_dd_delta_pct", "max", "fail"),
    ("cost_1_5x", "max_cost_1_5x_capital_loss_pct", "max_capital_loss_pct_1_5x", "strict_max", "fail"),
    ("concentration", "max_single_trade_concentration_pct", "single_trade_concentration_pct", "max", "fail"),
    ("version_compatibility", "config_version_required", "config_compatible", "true", "block"),
)


def _number(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def evaluate_gates(
    actuals: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> Dict[str, Any]:
    """评估十项门禁，不因前一失败而漏报后续失败。"""
    actuals = actuals if isinstance(actuals, Mapping) else {}
    thresholds = thresholds if isinstance(thresholds, Mapping) else {}
    gates: Dict[str, Dict[str, Any]] = {}
    blocked_reasons = []
    elimination_reasons = []

    for gate_name, threshold_key, actual_key, operator, failure_kind in GATE_SPECS:
        threshold = thresholds.get(threshold_key)
        actual = actuals.get(actual_key)
        reason = None

        if threshold is None:
            reason = "threshold_missing:{0}".format(threshold_key)
        elif actual is None:
            reason = "actual_missing:{0}".format(actual_key)
        elif operator in ("min", "max", "strict_max"):
            threshold_number = _number(threshold)
            actual_number = _number(actual)
            if threshold_number is None or (
                operator == "strict_max" and not Decimal("0") < threshold_number < Decimal("100")
            ):
                reason = "threshold_invalid:{0}".format(threshold_key)
            elif actual_number is None or (
                operator == "strict_max" and not Decimal("0") <= actual_number <= Decimal("100")
            ):
                reason = "actual_invalid:{0}".format(actual_key)
            elif operator == "min" and actual_number < threshold_number:
                reason = gate_name
            elif operator == "max" and actual_number > threshold_number:
                reason = gate_name
            elif operator == "strict_max" and actual_number >= threshold_number:
                reason = gate_name
        elif threshold is not True:
            reason = "threshold_invalid:{0}".format(threshold_key)
        elif operator == "zero":
            number = _number(actual)
            if number is None:
                reason = "actual_invalid:{0}".format(actual_key)
            elif number != Decimal("0"):
                reason = gate_name
        elif operator == "match":
            if actual == "partial_parity" or actual == "no_baseline":
                reason = "parity_incomplete:{0}".format(actual)
            elif actual != "match":
                reason = gate_name
        elif operator == "true" and actual is not True:
            reason = gate_name

        passed = reason is None
        gates[gate_name] = {
            "threshold": threshold,
            "actual": actual,
            "pass": passed,
        }
        if reason is not None:
            if reason.startswith(("threshold_", "actual_", "parity_incomplete")) or failure_kind == "block":
                blocked_reasons.append(reason)
            else:
                elimination_reasons.append(reason)

    # Future-data access is a framework violation and outranks incomplete samples.
    future_violation = "no_future_data" in elimination_reasons
    status = "FAIL" if future_violation else (
        "BLOCKED_DATA" if blocked_reasons else ("FAIL" if elimination_reasons else "PASS")
    )
    return {
        "status": status,
        "gates": gates,
        "blocked_reasons": blocked_reasons,
        "elimination_reasons": elimination_reasons,
    }
