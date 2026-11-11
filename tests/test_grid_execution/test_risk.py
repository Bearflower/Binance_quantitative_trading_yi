from datetime import datetime, timezone
from decimal import Decimal

import pytest

from strategies.grid.execution.risk import (
    RiskEvidence, RiskLimits, RiskStatus, assess_risk, beijing_day,
)


def _ms(iso):
    return int(datetime.fromisoformat(iso).astimezone(timezone.utc).timestamp() * 1000)


def _evidence(**changes):
    observed = _ms("2026-11-09T12:00:00+08:00")
    values = dict(
        observed_at_ms=observed,
        baseline_day="2026-11-09",
        baseline_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1100"),
        equity_usdt=Decimal("1000"),
        baseline_verified=True,
        income_complete=True,
        transfer_count=0,
        income_window_start_ms=_ms("2026-11-09T00:00:00+08:00"),
        income_window_end_ms=observed,
    )
    values.update(changes)
    return RiskEvidence(**values)


def _limits(**changes):
    values = dict(max_daily_loss_usdt=Decimal("40"),
                  max_drawdown_usdt=Decimal("200"))
    values.update(changes)
    return RiskLimits(**values)


def test_beijing_day_boundary_uses_utc_epoch_not_naive_host_time():
    assert beijing_day(_ms("2026-11-08T15:59:59+00:00")) == "2026-11-08"
    assert beijing_day(_ms("2026-11-08T16:00:00+00:00")) == "2026-11-09"


@pytest.mark.parametrize("equity,expected_reason", [
    ("970", "daily_pct"),
    ("960", "daily_pct"),
    ("935", "daily_pct"),
])
def test_daily_three_percent_limit_inclusive(equity, expected_reason):
    result = assess_risk(_evidence(equity_usdt=Decimal(equity)), _limits())
    assert result.status == RiskStatus.EXIT_LOCKED
    assert expected_reason in result.reasons


def test_drawdown_fifteen_percent_limit_inclusive():
    result = assess_risk(_evidence(baseline_equity_usdt=Decimal("1000"),
                                   peak_equity_usdt=Decimal("1200"),
                                   equity_usdt=Decimal("1020")),
                         _limits(max_daily_loss_usdt=Decimal("1000")))
    assert result.status == RiskStatus.EXIT_LOCKED
    assert "drawdown_pct" in result.reasons


def test_absolute_cap_can_stop_before_percent_limit():
    result = assess_risk(_evidence(peak_equity_usdt=Decimal("1000"),
                                   equity_usdt=Decimal("990")),
                         _limits(max_daily_loss_usdt=Decimal("5")))
    assert result.status == RiskStatus.EXIT_LOCKED
    assert "daily_usdt" in result.reasons
    assert "daily_pct" not in result.reasons


@pytest.mark.parametrize("changes", [
    {"income_complete": False},
    {"baseline_verified": False},
    {"transfer_count": 1},
    {"income_window_start_ms": None},
    {"income_window_end_ms": None},
    {"income_window_start_ms": _ms("2026-11-09T00:00:01+08:00")},
    {"income_window_end_ms": _ms("2026-11-09T11:59:59+08:00")},
    {"baseline_day": "2026-11-08"},
    {"equity_usdt": None},
    {"peak_equity_usdt": None},
])
def test_missing_or_transfer_evidence_locks_openings(changes):
    result = assess_risk(_evidence(**changes), _limits())
    assert result.status == RiskStatus.UNKNOWN_LOCKED


def test_safe_risk_updates_peak_without_mutating_baseline():
    result = assess_risk(_evidence(equity_usdt=Decimal("1120")), _limits())
    assert result.status == RiskStatus.ALLOW
    assert result.next_peak_equity_usdt == Decimal("1120")
    assert result.daily_loss_usdt == Decimal("0")


def test_limits_must_be_explicit_positive():
    with pytest.raises(ValueError):
        RiskLimits(Decimal("0"), Decimal("200"))
