"""Synthetic three-tier scorecard evidence, never qualification results."""

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_tuner.qualification.scorecard import build_scorecard
from ai_tuner.qualification.metrics import calmar_diagnostic
from ai_tuner.replay.artifacts import canonical_json
from ai_tuner.replay.broker import VirtualBroker
from ai_tuner.replay.data_portal import DataPortal
from ai_tuner.replay.engine import run_replay
from ai_tuner.replay.models import ConfigHashes, EquityPoint, ReplayResult, UniverseSlice

from tests.test_ai_tuner_mvp.replay.conftest import ScriptedAdapter, make_bars, write_snapshot


D = Decimal
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
DIAGNOSTIC_END = START + timedelta(days=56)
REPLAY_END = START + timedelta(days=70)


def point(day, equity, *, hour=0, open_position=False):
    ts = START + timedelta(days=day, hours=hour)
    return EquityPoint(ts, D(str(equity)), D(str(equity)), open_position)


def result(points, manifest="frozen-manifest", invariant_violations=()):
    return ReplayResult(manifest, tuple(points), (), None, (), tuple(invariant_violations))


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


@pytest.mark.parametrize("tier", ["1.5x", "2.0x"])
def test_missing_daily_mark_in_any_cost_tier_blocks(tier):
    inputs = tiers()
    daily = [point(day, 100 + (day % 4) - (day % 3)) for day in range(71) if day != 10]
    inputs[tier] = result(daily)
    card = scorecard(inputs)
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert any(reason.startswith("daily_equity_gap:{0}:".format(tier)) for reason in card["blocked_reasons"])


def test_diagnostic_window_cannot_extend_into_blind_segment():
    card = build_scorecard(
        tiers(), initial_equity=D(100), replay_start=START,
        replay_end=REPLAY_END, diagnostic_start=START,
        diagnostic_end=REPLAY_END,
    )
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert "blind_window_overlap" in card["blocked_reasons"]


def test_diagnostic_window_must_be_eight_weeks():
    card = build_scorecard(
        tiers(), initial_equity=D(100), replay_start=START,
        replay_end=REPLAY_END, diagnostic_start=START,
        diagnostic_end=START + timedelta(days=55),
    )
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert "diagnostic_window_not_eight_weeks" in card["blocked_reasons"]


def test_start_mark_must_match_initial_equity_and_be_flat():
    inputs = tiers()
    altered = list(inputs["1.5x"].equity_curve)
    altered[0] = point(0, 110, open_position=True)
    inputs["1.5x"] = result(altered)
    card = scorecard(inputs)
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert "initial_equity_mismatch:1.5x" in card["blocked_reasons"]
    assert "initial_position_open:1.5x" in card["blocked_reasons"]


def test_cost_monotonicity_violation_is_exposed_at_top_level():
    high = [point(day, 100 + (day % 4) - (day % 3)) for day in range(71)]
    high[-1] = point(70, 110)
    card = scorecard(tiers(stress=high))
    assert card["cost_monotonicity_violation"] is True
    assert scorecard(tiers())["cost_monotonicity_violation"] is False


def test_independent_input_errors_are_aggregated():
    inputs = tiers()
    inputs["1.5x"] = result(inputs["1.5x"].equity_curve, manifest="other")
    inputs["2.0x"] = result([])
    card = scorecard(inputs)
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert "manifest_mismatch:1.5x" in card["blocked_reasons"]
    assert "equity_curve_empty:2.0x" in card["blocked_reasons"]
    assert card["cost_monotonicity_violation"] is False
    assert card["cost_monotonicity"]["holds"] is None


def test_all_missing_and_unexpected_tiers_are_reported_together():
    inputs = tiers()
    inputs.pop("1.5x")
    inputs.pop("2.0x")
    inputs["3.0x"] = result([point(0, 100)])
    card = scorecard(inputs)
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert "tier_missing:1.5x" in card["blocked_reasons"]
    assert "tier_missing:2.0x" in card["blocked_reasons"]
    assert "unexpected_tier:3.0x" in card["blocked_reasons"]


def test_calmar_anchors_diagnostic_start_while_baseline_mdd_uses_replay_start():
    marks = [point(day, 100 if day < 7 else 100 + day) for day in range(78)]
    marks[7] = point(7, 90)
    marks[21] = point(21, 119)
    inputs = {tier: result(marks) for tier in ("baseline", "1.5x", "2.0x")}
    card = build_scorecard(
        inputs,
        initial_equity=D(100),
        replay_start=START,
        replay_end=START + timedelta(days=77),
        diagnostic_start=START + timedelta(days=7),
        diagnostic_end=START + timedelta(days=63),
    )
    assert card["evidence_status"] == "AVAILABLE"
    assert card["calmar"] == calmar_diagnostic(
        marks, START + timedelta(days=7), START + timedelta(days=63), D(90)
    )
    assert card["baseline_max_drawdown_pct"] == D(10)


def test_non_midnight_diagnostic_calmar_uses_complete_daily_boundaries():
    marks = [point(day, 100 + (day % 4) - (day % 3)) for day in range(72)]
    inputs = {tier: result(marks) for tier in ("baseline", "1.5x", "2.0x")}
    card = build_scorecard(
        inputs,
        initial_equity=D(100),
        replay_start=START,
        replay_end=START + timedelta(days=71),
        diagnostic_start=START + timedelta(hours=12),
        diagnostic_end=DIAGNOSTIC_END + timedelta(hours=12),
    )
    expected = calmar_diagnostic(
        marks, START + timedelta(days=1), DIAGNOSTIC_END,
        marks[1].marked_equity,
    )
    assert card["evidence_status"] == "AVAILABLE"
    assert card["sortino"] is not None
    assert card["daily_return_count"] == 55
    assert card["daily_return_start"] == START + timedelta(days=1)
    assert card["daily_return_end"] == DIAGNOSTIC_END
    assert card["calmar"] == expected
    assert card["calmar"]["calmar"] is not None
    assert card["calmar"]["elapsed_days"] == D(55)
    assert card["calmar"]["window_return"] == marks[56].marked_equity / marks[1].marked_equity - 1


def test_single_tier_replay_invariant_violation_is_framework_fail():
    inputs = tiers()
    inputs["1.5x"] = result(inputs["1.5x"].equity_curve,
                             invariant_violations=("reduce_only_overfill",))
    card = scorecard(inputs)
    assert card["evidence_status"] == "FAIL"
    assert "replay_invariant_violation:1.5x:reduce_only_overfill" in card["failure_reasons"]


def test_replay_invariant_violations_aggregate_across_tiers():
    inputs = tiers()
    inputs["baseline"] = result(inputs["baseline"].equity_curve,
                                invariant_violations=("negative_cash", "duplicate_fill"))
    inputs["2.0x"] = result(inputs["2.0x"].equity_curve,
                            invariant_violations=("negative_cash",))
    card = scorecard(inputs)
    assert card["evidence_status"] == "FAIL"
    assert card["failure_reasons"] == [
        "replay_invariant_violation:baseline:negative_cash",
        "replay_invariant_violation:baseline:duplicate_fill",
        "replay_invariant_violation:2.0x:negative_cash",
    ]


def test_blocked_data_outweighs_replay_invariant_fail():
    inputs = tiers()
    missing_day = tuple(point(day, 100 + (day % 4) - (day % 3))
                        for day in range(71) if day != 10)
    inputs["1.5x"] = result(missing_day, invariant_violations=("duplicate_fill",))
    card = scorecard(inputs)
    assert card["evidence_status"] == "BLOCKED_DATA"
    assert "replay_invariant_violation:1.5x:duplicate_fill" in card["failure_reasons"]
    assert any(reason.startswith("daily_equity_gap:1.5x:") for reason in card["blocked_reasons"])


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


def test_same_frozen_tiers_produce_identical_canonical_scorecard_bytes():
    inputs = tiers()
    first = canonical_json(scorecard(inputs)).encode("utf-8")
    second = canonical_json(scorecard(inputs)).encode("utf-8")
    assert first == second
    assert first.endswith(b"\n")
    assert hashlib.sha256(first).hexdigest() == "2352e152c79a966735bf77f349069ad6186672e230b6a00b088e84286f8c5883"


def test_two_synthetic_replays_produce_identical_canonical_scorecard_bytes(tmp_path):
    snapshot_dir = tmp_path / "frozen_snapshot"
    write_snapshot(
        str(snapshot_dir), "fixture", REPLAY_END, START,
        {"BTCUSDT|1d": make_bars(
            "BTCUSDT", "1d", START, [("100", "100", "100", "100")] * 70,
            step_minutes=1440,
        )},
        ["BTCUSDT"], ["1d"],
    )
    universe = UniverseSlice(
        "fixture", START, REPLAY_END + timedelta(days=1),
        ("BTCUSDT",), ("1d",), "strategy_config", "a" * 64,
    )
    hashes = ConfigHashes("fixture", REPLAY_END, "b" * 64, "c" * 64, "d" * 64)

    def replay_tiers():
        portal = DataPortal(snapshot_dir, "fixture", REPLAY_END, START)
        results = {}
        for tier, multiplier in (("baseline", D(1)), ("1.5x", D("1.5")), ("2.0x", D(2))):
            broker = VirtualBroker(
                filters={}, fee_bps=D(5) * multiplier,
                slippage_bps=D(2) * multiplier, stop_offset_pct=D("0.002"),
                tp_offset_pct=D("0.0015"), initial_equity=D(100),
                max_leverage=D(0), tp1_close_ratio=D("0.5"),
            )
            adapter = ScriptedAdapter(universe, hashes, lambda bar, ctx: ())
            results[tier] = run_replay(portal, adapter, broker)
        return results

    first = canonical_json(scorecard(replay_tiers())).encode("utf-8")
    second = canonical_json(scorecard(replay_tiers())).encode("utf-8")
    assert first == second
    assert b'"evidence_status":"BLOCKED_DATA"' in first  # flat cash has no downside
