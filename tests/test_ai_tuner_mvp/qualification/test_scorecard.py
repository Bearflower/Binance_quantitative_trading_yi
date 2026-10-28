"""Synthetic three-tier scorecard evidence, never qualification results."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_tuner.qualification.scorecard import build_scorecard
from ai_tuner.replay.models import EquityPoint, ReplayResult


D = Decimal
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
DIAGNOSTIC_END = START + timedelta(days=56)
REPLAY_END = START + timedelta(days=70)


def point(day, equity, *, hour=0, open_position=False):
    ts = START + timedelta(days=day, hours=hour)
    return EquityPoint(ts, D(str(equity)), D(str(equity)), open_position)


def result(points, manifest="frozen-manifest"):
    return ReplayResult(manifest, tuple(points), (), None, (), ())


def tiers(*, stress=None, baseline=None, twice=None):
    daily = [point(day, 100 + (day % 4) - (day % 3)) for day in range(71)]
    return {
        "baseline": result(baseline if baseline is not None else daily),
        "1.5x": result(stress if stress is not None else daily),
        "2.0x": result(twice if twice is not None else daily),
    }


def scorecard(inputs, **kwargs):
    return build_scorecard(
        inputs,
        initial_equity=D(100),
        replay_start=START,
        replay_end=REPLAY_END,
        diagnostic_start=START,
        diagnostic_end=DIAGNOSTIC_END,
        **kwargs,
    )


def test_scorecard_uses_only_diagnostic_daily_returns_and_pathwise_stress_loss():
    inputs = tiers()
    inputs["1.5x"] = result(
        [point(day, 100 + (day % 4) - (day % 3)) for day in range(71)]
        + [point(40, 69, hour=12, open_position=True)]
    )
    inputs["1.5x"] = result(sorted(inputs["1.5x"].equity_curve, key=lambda p: p.ts))
    card = scorecard(inputs)
    assert card["evidence_status"] == "AVAILABLE"
    assert card["manifest_ref"] == "frozen-manifest"
    assert card["daily_return_count"] == 56
    assert card["daily_return_start"] == START
    assert card["daily_return_end"] == DIAGNOSTIC_END
    assert card["sortino"] is not None
    assert card["calmar"]["short_window"] is True
    assert card["calmar"]["calmar_use"] == "diagnostic_only"
    assert card["tiers"]["1.5x"]["min_marked_equity"] == D(69)
    assert card["max_capital_loss_pct_1_5x"] == D(31)
    assert card["preserved_capital_pct_1_5x"] == D(69)
    assert card["cost_monotonicity"]["holds"] is True
    assert "pass" not in card


@pytest.mark.parametrize("trough, expected", [("70", D(30)), ("70.001", D("29.999"))])
def test_stress_loss_preserves_raw_decimal_boundary(trough, expected):
    daily = [point(day, 100 + (day % 4) - (day % 3)) for day in range(71)]
    stress = sorted(daily + [point(40, trough, hour=12, open_position=True)], key=lambda p: p.ts)
    card = scorecard(tiers(stress=stress))
    assert card["max_capital_loss_pct_1_5x"] == expected


def test_funding_only_open_position_mark_counts_in_minimum():
    daily = [point(day, 100 + (day % 4) - (day % 3)) for day in range(71)]
    stress = sorted(daily + [point(30, 65, hour=8, open_position=True)], key=lambda p: p.ts)
    assert scorecard(tiers(stress=stress))["max_capital_loss_pct_1_5x"] == D(35)


def test_blind_window_does_not_change_sortino_or_calmar():
    first = scorecard(tiers())
    altered = [point(day, 100 + (day % 4) - (day % 3)) for day in range(57)]
    altered += [point(day, 50) for day in range(57, 71)]
    second = scorecard(tiers(baseline=altered))
    assert first["sortino"] == second["sortino"]
    assert first["calmar"] == second["calmar"]


@pytest.mark.parametrize("mutation, reason", [
    (lambda x: x.pop("2.0x"), "tier_missing:2.0x"),
    (lambda x: x.update({"1.5x": result([])}), "equity_curve_empty:1.5x"),
    (lambda x: x.update({"1.5x": result([point(0, 100), point(0, 99)])}), "equity_timestamp_order:1.5x"),
    (lambda x: x.update({"1.5x": result([point(0, "NaN")])}), "invalid_marked_equity:1.5x"),
    (lambda x: x.update({"1.5x": result([point(71, 100)])}), "equity_outside_window:1.5x"),
    (lambda x: x.update({"1.5x": result([point(0, 100)], "other")}), "manifest_mismatch:1.5x"),
])
def test_invalid_tier_data_blocks_with_structured_reason(mutation, reason):
    inputs = tiers()
    mutation(inputs)
    card = scorecard(inputs)
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert reason in card["blocked_reasons"]


@pytest.mark.parametrize("initial", [D(0), D(-1), D("NaN")])
def test_invalid_initial_equity_blocks(initial):
    card = build_scorecard(tiers(), initial_equity=initial, replay_start=START,
                           replay_end=REPLAY_END, diagnostic_start=START,
                           diagnostic_end=DIAGNOSTIC_END)
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert "invalid_initial_equity" in card["blocked_reasons"]


def test_missing_diagnostic_midnight_blocks_without_filling_zero():
    daily = [point(day, 100 + (day % 4) - (day % 3)) for day in range(71) if day != 10]
    card = scorecard(tiers(baseline=daily))
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert any(reason.startswith("daily_equity_gap:") for reason in card["blocked_reasons"])
    assert card["sortino"] is None


def test_missing_terminal_stress_mark_blocks_instead_of_using_earlier_final():
    stress = [point(day, 100 + (day % 4) - (day % 3)) for day in range(70)]
    card = scorecard(tiers(stress=stress))
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert "equity_end_missing:1.5x" in card["blocked_reasons"]


def test_zero_downside_blocks_sortino_but_keeps_calmar_diagnostic_reason():
    rising = [point(day, 100 + day) for day in range(71)]
    card = scorecard(tiers(baseline=rising))
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert card["sortino"] is None
    assert card["sortino_unavailable_reason"] == "no_downside_observation"
    assert card["calmar"]["calmar"] is None
    assert card["calmar"]["calmar_unavailable_reason"] == "zero_drawdown"


def test_final_equity_cost_inversion_is_framework_fail():
    high = [point(day, 100 + (day % 4) - (day % 3)) for day in range(71)]
    high[-1] = point(70, 110)
    card = scorecard(tiers(stress=high))
    assert card["evidence_status"] == "FAIL"
    assert card["cost_monotonicity"]["holds"] is False
    assert "cost_monotonicity" in card["failure_reasons"]


def test_same_inputs_produce_same_evidence():
    inputs = tiers()
    assert scorecard(inputs) == scorecard(inputs)
