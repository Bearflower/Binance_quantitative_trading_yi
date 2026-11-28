from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from strategies.hrs.risk_ledger import EquityCheckpoint, LedgerNotReady
from strategies.hrs.loss_gate import (
    SHANGHAI, GateHistory, LossGateState, evaluate_loss_gate, reset_rolling_latch,
)

# 00:00 Asia/Shanghai
T = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)


def cp(at=T, nav="1", segment="hrs-1"):
    return EquityCheckpoint(segment, at, D(nav) * 1000, D("1000"), D(nav))


def history(samples, start=T, through=None):
    return GateHistory(tuple(samples), start, through or samples[-1].as_of, True)


def evaluate(state, current, samples, **kwargs):
    return evaluate_loss_gate(state, current, history(samples), allow_inception_segment=True, **kwargs)


def test_exact_daily_three_percent_latches_and_recovery_does_not_unlock():
    current = cp(T + timedelta(hours=1), ".97")
    result = evaluate(None, current, [cp(), current])
    assert result.state.daily_latched
    assert not result.allow_new_risk
    assert "daily_loss_latched" in result.reasons
    recovered = cp(T + timedelta(hours=2), "1.1")
    result2 = evaluate(result.state, recovered, [cp(), current, recovered])
    assert result2.state.daily_latched


def test_below_daily_threshold_and_inception_label_do_not_claim_30_days():
    current = cp(T + timedelta(hours=1), ".97000001")
    result = evaluate(None, current, [cp(), current])
    assert result.allow_new_risk
    assert result.ready
    assert result.window_label == "inception_segment"
    assert not result.qualified_30_day


def test_missing_origin_and_incomplete_history_are_explicit_not_ready():
    current = cp(T + timedelta(hours=1))
    result = evaluate_loss_gate(None, current, history([current], start=current.as_of))
    assert not result.allow_new_risk
    assert "daily_origin_missing" in result.reasons
    assert "rolling_30_day_history_incomplete" in result.reasons


def test_cross_day_reset_requires_verified_origin_and_valid_data():
    loss = cp(T + timedelta(hours=1), ".97")
    state = evaluate(None, loss, [cp(), loss]).state
    tomorrow = cp(T + timedelta(days=1), ".97")
    invalid = evaluate_loss_gate(state, tomorrow, history([cp(), tomorrow]), data_valid=False,
                                 allow_inception_segment=True)
    assert invalid.state.daily_latched
    assert not invalid.allow_new_risk
    missing = evaluate_loss_gate(state, cp(tomorrow.as_of + timedelta(hours=1), ".98"),
                                 history([cp(), loss]), allow_inception_segment=True)
    assert missing.state.daily_latched
    valid = evaluate(state, tomorrow, [cp(), loss, tomorrow])
    assert not valid.state.daily_latched
    assert valid.allow_new_risk


def test_exact_rolling_six_percent_survives_restart_day_and_deposit():
    peak = cp(T + timedelta(hours=1), "1.1")
    low = cp(T + timedelta(hours=2), "1.034")
    result = evaluate(None, low, [cp(), peak, low])
    assert result.state.rolling_latched
    assert "inception_segment_drawdown_latched" in result.reasons
    # Units/equity can double due to allocation; NAV and latch are unchanged.
    tomorrow = replace(cp(T + timedelta(days=1), "1.2"), equity=D("2400"), units=D("2000"))
    restarted = LossGateState.from_dict(result.state.to_dict())
    result2 = evaluate(restarted, tomorrow, [cp(), peak, low, tomorrow])
    assert result2.state.rolling_latched
    assert not result2.allow_new_risk


def test_30_elapsed_days_need_boundary_and_explicit_complete_coverage():
    current = cp(T + timedelta(days=30, hours=12), "1")
    result = evaluate_loss_gate(None, current, history([cp(), cp(T + timedelta(hours=12)), cp(T + timedelta(days=30)), current]))
    assert result.qualified_30_day
    assert result.window_label == "rolling_30_day"
    assert result.allow_new_risk
    incomplete = evaluate_loss_gate(None, current, replace(history([cp(), current]), complete=False))
    assert not incomplete.ready
    assert not incomplete.qualified_30_day


def test_rolling_window_excludes_old_peak_after_it_expires():
    day30 = T + timedelta(days=31)
    samples = [cp(T, "2"), cp(T + timedelta(days=1), "1"), cp(day30, "1")]
    result = evaluate_loss_gate(None, samples[-1], history(samples))
    assert not result.state.rolling_latched
    assert result.rolling_drawdown == D("0")


def test_intraday_rolling_window_does_not_retain_a_peak_older_than_thirty_days():
    current = cp(T + timedelta(days=30, hours=12))
    samples = [cp(T, '1.1'), cp(T + timedelta(hours=12)), cp(T + timedelta(days=30)), current]
    result = evaluate_loss_gate(None, current, history(samples))
    assert result.qualified_30_day
    assert result.allow_new_risk
    assert result.rolling_drawdown == D('0')


def test_controlled_reset_requires_reason_evidence_and_expected_version():
    low = cp(T + timedelta(hours=1), ".94")
    state = evaluate(None, low, [cp(), low]).state
    with pytest.raises(ValueError, match="reason_and_evidence"):
        reset_rolling_latch(state, expected_revision=state.revision, reason="", evidence="ticket", at=low.as_of)
    with pytest.raises(ValueError, match="revision_conflict"):
        reset_rolling_latch(state, expected_revision=0, reason="approved", evidence="ticket", at=low.as_of)
    reset = reset_rolling_latch(state, expected_revision=state.revision,
                               reason="reviewed reconciliation", evidence="ticket-1", at=low.as_of)
    assert not reset.rolling_latched
    assert reset.daily_latched  # reset cannot clear daily protection
    assert reset.reset_reason == "reviewed reconciliation"
    assert reset.reset_evidence == "ticket-1"


def test_foreign_future_conflicting_history_and_nan_cannot_open_gate():
    current = cp(T + timedelta(hours=1))
    for bad in [cp(segment="other"), cp(T, "NaN"), cp(current.as_of + timedelta(seconds=1)), cp(T, "2")]:
        result = evaluate_loss_gate(None, current, history([cp(), bad, current]), allow_inception_segment=True)
        assert not result.allow_new_risk
        assert "invalid_nav_history" in result.reasons


def test_twenty_nine_elapsed_days_are_not_30_day_qualified():
    current = cp(T + timedelta(days=29, hours=23))
    samples = [cp(), cp(T + timedelta(days=29)), current]
    result = evaluate_loss_gate(None, current, history(samples), allow_inception_segment=True)
    assert not result.qualified_30_day
    assert result.window_label == "inception_segment"


def test_before_midnight_and_after_midnight_are_not_substituted_for_daily_origin():
    before = cp(T - timedelta(seconds=1), "1.1")
    after = cp(T + timedelta(seconds=1), "1.1")
    current = cp(T + timedelta(hours=1), "1")
    result = evaluate_loss_gate(None, current,
                               history([before, after, current], start=before.as_of),
                               allow_inception_segment=True)
    assert not result.ready
    assert "daily_origin_missing" in result.reasons


def test_below_rolling_limit_does_not_latch_even_with_large_unit_reallocation():
    peak = cp(T + timedelta(hours=1), "1.1")
    current = replace(cp(T + timedelta(hours=2), "1.034000001"),
                      equity=D("2068.000002"), units=D("2000"))
    result = evaluate(None, current, [cp(), peak, current])
    assert result.allow_new_risk
    assert not result.state.rolling_latched


def test_missing_coverage_does_not_reset_previous_day_latch():
    low = cp(T + timedelta(hours=1), ".97")
    state = evaluate(None, low, [cp(), low]).state
    tomorrow = cp(T + timedelta(days=1), ".97")
    result = evaluate_loss_gate(state, tomorrow,
                               replace(history([cp(), low, tomorrow]), complete=False),
                               allow_inception_segment=True)
    assert result.state.daily_latched
    assert result.state.day == state.day
    assert not result.allow_new_risk


def test_inconsistent_current_equity_units_is_not_ready():
    current = replace(cp(T + timedelta(hours=1)), equity=D("2000"))
    result = evaluate(None, current, [cp(), current])
    assert not result.allow_new_risk
    assert "invalid_nav_history" in result.reasons


@pytest.mark.parametrize("origin", [D("NaN"), D("0"), D("-1")])
def test_corrupt_persisted_daily_origin_fails_closed(origin):
    state = LossGateState("hrs-1", T.astimezone(SHANGHAI).date(),
                          day_origin_nav=origin)
    current = cp(T + timedelta(hours=1))
    result = evaluate(state, current, [current])
    assert not result.allow_new_risk
    assert "invalid_persisted_gate_state" in result.reasons
