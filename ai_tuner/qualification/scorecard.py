"""Assemble evidence from three completed, frozen replay results.

Equal manifest references prove snapshot identity only. The admission runner must
separately verify strategy, candidate, parameter, and overlay identity before any
formal qualification decision. This module neither reruns a strategy nor admits a
candidate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Mapping

from ai_tuner.qualification.metrics import (
    calmar_diagnostic,
    daily_returns,
    max_capital_loss_pct,
    max_drawdown_pct,
    sortino,
)
from ai_tuner.replay.models import DataBlocked, ReplayResult


TIERS = ("baseline", "1.5x", "2.0x")
HUNDRED = Decimal(100)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DataBlocked(["invalid_window_timestamp"])
    return value.astimezone(timezone.utc)


def _validate_inputs(
    results: Mapping[str, ReplayResult], initial_equity: Decimal,
    replay_start: datetime, replay_end: datetime,
    diagnostic_start: datetime, diagnostic_end: datetime,
) -> tuple[datetime, datetime, datetime, datetime]:
    if not isinstance(initial_equity, Decimal) or not initial_equity.is_finite() or initial_equity <= 0:
        raise DataBlocked(["invalid_initial_equity"])
    start, end, diag_start, diag_end = map(
        _utc, (replay_start, replay_end, diagnostic_start, diagnostic_end)
    )
    if not start <= diag_start < diag_end <= end:
        raise DataBlocked(["invalid_window_order"])
    window_reasons = []
    if diag_end - diag_start != timedelta(weeks=8):
        window_reasons.append("diagnostic_window_not_eight_weeks")
    if diag_end > end - timedelta(weeks=2):
        window_reasons.append("blind_window_overlap")
    if window_reasons:
        raise DataBlocked(window_reasons)
    if not isinstance(results, Mapping):
        raise DataBlocked(["invalid_tiers"])
    reasons = []
    for tier in TIERS:
        if tier not in results:
            reasons.append("tier_missing:{0}".format(tier))
    for tier in sorted((key for key in results if key not in TIERS), key=str):
        reasons.append("unexpected_tier:{0}".format(tier))
    reference = None
    for tier in TIERS:
        if tier not in results:
            continue
        result = results[tier]
        if not isinstance(result, ReplayResult):
            reasons.append("invalid_replay_result:{0}".format(tier))
            continue
        if not isinstance(result.manifest_ref, str) or not result.manifest_ref.strip():
            reasons.append("manifest_missing:{0}".format(tier))
        elif reference is None:
            reference = result.manifest_ref
        elif result.manifest_ref != reference:
            reasons.append("manifest_mismatch:{0}".format(tier))
        if not result.equity_curve:
            reasons.append("equity_curve_empty:{0}".format(tier))
            continue
        previous = None
        first_stamp = None
        curve_valid = True
        for point in result.equity_curve:
            try:
                stamp = _utc(point.ts)
            except (AttributeError, DataBlocked):
                reasons.append("invalid_equity_timestamp:{0}".format(tier))
                curve_valid = False
                continue
            if not start <= stamp <= end:
                reasons.append("equity_outside_window:{0}".format(tier))
                curve_valid = False
            if previous is not None and stamp <= previous:
                reasons.append("equity_timestamp_order:{0}".format(tier))
                curve_valid = False
            if first_stamp is None:
                first_stamp = stamp
            previous = stamp
            equity = getattr(point, "marked_equity", None)
            if not isinstance(equity, Decimal) or not equity.is_finite() or equity <= 0:
                reasons.append("invalid_marked_equity:{0}".format(tier))
                curve_valid = False
        if first_stamp != start:
            reasons.append("equity_start_missing:{0}".format(tier))
            curve_valid = False
        if previous != end:
            reasons.append("equity_end_missing:{0}".format(tier))
            curve_valid = False
        if first_stamp == start:
            first_point = result.equity_curve[0]
            if getattr(first_point, "marked_equity", None) != initial_equity:
                reasons.append("initial_equity_mismatch:{0}".format(tier))
            if getattr(first_point, "has_open_position", None) is not False:
                reasons.append("initial_position_open:{0}".format(tier))
        if curve_valid:
            try:
                daily_returns(result.equity_curve, start, end)
            except DataBlocked as error:
                for gap in error.gaps:
                    code, _, detail = gap.partition(":")
                    reasons.append("{0}:{1}{2}".format(
                        code, tier, ":" + detail if detail else ""
                    ))
    if reasons:
        raise DataBlocked(dict.fromkeys(reasons))
    return start, end, diag_start, diag_end


def build_scorecard(
    results: Mapping[str, ReplayResult], *, initial_equity: Decimal,
    replay_start: datetime, replay_end: datetime,
    diagnostic_start: datetime, diagnostic_end: datetime,
) -> Dict[str, Any]:
    """Return risk evidence only; status AVAILABLE is not candidate admission."""
    card: Dict[str, Any] = {
        "evidence_status": "AVAILABLE",
        "blocked_reasons": [],
        "failure_reasons": [],
        "manifest_ref": None,
        "daily_return_count": None,
        "daily_return_start": None,
        "daily_return_end": None,
        "sortino": None,
        "sortino_unavailable_reason": None,
        "calmar": None,
        "baseline_max_drawdown_pct": None,
        "tiers": {},
        "max_capital_loss_pct_1_5x": None,
        "preserved_capital_pct_1_5x": None,
        "cost_monotonicity": {"holds": None},
        "cost_monotonicity_violation": False,
    }
    try:
        _, _, diag_start, diag_end = _validate_inputs(
            results, initial_equity, replay_start, replay_end,
            diagnostic_start, diagnostic_end,
        )
        card["manifest_ref"] = results["baseline"].manifest_ref
        for tier in TIERS:
            points = results[tier].equity_curve
            minimum = min(initial_equity, *(point.marked_equity for point in points))
            card["tiers"][tier] = {
                "final_equity": points[-1].marked_equity,
                "min_marked_equity": minimum,
                "max_capital_loss_pct": max_capital_loss_pct(points, initial_equity),
            }
        card["baseline_max_drawdown_pct"] = max_drawdown_pct(
            results["baseline"].equity_curve, initial_equity
        )
        loss = card["tiers"]["1.5x"]["max_capital_loss_pct"]
        card["max_capital_loss_pct_1_5x"] = loss
        card["preserved_capital_pct_1_5x"] = HUNDRED - loss
        finals = [card["tiers"][tier]["final_equity"] for tier in TIERS]
        monotonic = finals[0] >= finals[1] >= finals[2]
        card["cost_monotonicity"] = {"holds": monotonic, "final_equities": dict(zip(TIERS, finals))}
        card["cost_monotonicity_violation"] = not monotonic
        if not monotonic:
            card["failure_reasons"].append("cost_monotonicity")
        returns, first, last = daily_returns(
            results["baseline"].equity_curve, diag_start, diag_end
        )
        card["daily_return_count"] = len(returns)
        card["daily_return_start"] = first
        card["daily_return_end"] = last
        if len(returns) < 30:
            raise DataBlocked(["insufficient_diagnostic_daily_returns"])
        value, reason = sortino(returns)
        card["sortino"] = value
        card["sortino_unavailable_reason"] = reason
        if reason:
            card["blocked_reasons"].append(reason)
        diagnostic_start_mark = next(
            (point for point in results["baseline"].equity_curve
             if point.ts.astimezone(timezone.utc) == first),
            None,
        )
        card["calmar"] = calmar_diagnostic(
            results["baseline"].equity_curve, first, last,
            diagnostic_start_mark.marked_equity if diagnostic_start_mark else initial_equity,
        )
    except DataBlocked as error:
        card["blocked_reasons"].extend(error.gaps)
        if card["sortino"] is None and card["sortino_unavailable_reason"] is None:
            card["sortino_unavailable_reason"] = error.gaps[0].split(":", 1)[0]
    card["evidence_status"] = (
        "BLOCKED_DATA" if card["blocked_reasons"] else
        "FAIL" if card["failure_reasons"] else "AVAILABLE"
    )
    return card
