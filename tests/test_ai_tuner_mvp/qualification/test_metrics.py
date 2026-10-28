"""Pure risk metrics over replay marked-equity points."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from ai_tuner.qualification.metrics import (
    calmar_diagnostic,
    daily_returns,
    max_capital_loss_pct,
    max_drawdown_pct,
    sortino,
)
from ai_tuner.replay.models import DataBlocked, EquityPoint


UTC = timezone.utc
D = Decimal


def point(day, equity, hour=0, minute=0):
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day, hours=hour, minutes=minute)
    return EquityPoint(ts, D(str(equity)), D(str(equity)), False)


def test_daily_returns_use_consecutive_utc_midnight_marks():
    marks = [point(0, 100), point(1, 101), point(2, "98.98"), point(3, "101.9494")]
    returns, first, last = daily_returns(marks, marks[0].ts, marks[-1].ts)
    assert returns == (D("0.01"), D("-0.02"), D("0.03"))
    assert (first, last) == (marks[0].ts, marks[-1].ts)


def test_daily_returns_trim_partial_utc_days():
    marks = [point(0, 100), point(1, 101), point(2, 102), point(3, 103)]
    returns, first, last = daily_returns(
        marks, point(0, 100, hour=12).ts, point(3, 103, hour=12).ts
    )
    assert returns == (D(102) / D(101) - 1, D(103) / D(102) - 1)
    assert (first, last) == (marks[1].ts, marks[3].ts)


def test_daily_returns_reject_missing_day_instead_of_forward_filling():
    marks = [point(0, 100), point(2, 101)]
    with pytest.raises(DataBlocked, match="daily_equity_gap"):
        daily_returns(marks, marks[0].ts, marks[-1].ts)


def test_daily_returns_reject_duplicate_midnight():
    marks = [point(0, 100), point(1, 101), point(1, 102)]
    with pytest.raises(DataBlocked, match="duplicate_daily_boundary"):
        daily_returns(marks, marks[0].ts, marks[-1].ts)


def test_daily_returns_reject_naive_timestamp():
    naive = EquityPoint(datetime(2026, 1, 1), D(100), D(100), False)
    with pytest.raises(DataBlocked, match="naive_timestamp"):
        daily_returns([naive, point(1, 101)], point(0, 100).ts, point(1, 101).ts)


def test_daily_returns_normalize_aware_timezone_to_utc_boundary():
    offset = timezone(timedelta(hours=8))
    local_midnight_mark = EquityPoint(
        datetime(2026, 1, 2, 8, tzinfo=offset), D(101), D(101), False
    )
    marks = [point(0, 100), local_midnight_mark]
    returns, first, last = daily_returns(marks, marks[0].ts, point(1, 101).ts)
    assert returns == (D("0.01"),)
    assert (first, last) == (point(0, 100).ts, point(1, 101).ts)


@pytest.mark.parametrize("equity", ["0", "-1", "NaN", "Infinity"])
def test_daily_returns_reject_invalid_marked_equity(equity):
    marks = [point(0, 100), point(1, equity)]
    with pytest.raises(DataBlocked, match="invalid_marked_equity"):
        daily_returns(marks, marks[0].ts, marks[-1].ts)


def test_sortino_uses_all_days_in_downside_deviation():
    returns = (D("0.01"), D("-0.02"), D("0.03"))
    with localcontext() as ctx:
        ctx.prec = 28
        expected = (sum(returns) / D(3)) / ((D("0.02") ** 2 / D(3)).sqrt()) * D(365).sqrt()
    value, reason = sortino(returns)
    assert value == expected
    assert reason is None


def test_sortino_without_downside_is_unavailable():
    assert sortino((D("0.01"), D("0"))) == (None, "no_downside_observation")


def test_sortino_rejects_nonfinite_returns():
    assert sortino((D("NaN"),)) == (None, "invalid_daily_return")


def test_drawdown_uses_intraday_trough_and_initial_peak():
    marks = [point(0, 100), point(0, 69, hour=12), point(1, 105)]
    assert max_drawdown_pct(marks, D(100)) == D(31)
    assert max_capital_loss_pct(marks, D(100)) == D(31)


def test_drawdown_anchors_initial_equity_even_if_first_mark_is_lower():
    assert max_drawdown_pct([point(0, 90), point(1, 95)], D(100)) == D(10)


@pytest.mark.parametrize("initial", [D(0), D(-1)])
def test_loss_metrics_reject_nonpositive_initial_equity(initial):
    with pytest.raises(DataBlocked, match="invalid_initial_equity"):
        max_drawdown_pct([point(0, 100)], initial)
    with pytest.raises(DataBlocked, match="invalid_initial_equity"):
        max_capital_loss_pct([point(0, 100)], initial)


def test_calmar_zero_drawdown_is_diagnostic_null():
    marks = [point(0, 100), point(1, 105)]
    result = calmar_diagnostic(marks, marks[0].ts, marks[-1].ts, D(100))
    assert result["calmar"] is None
    assert result["calmar_unavailable_reason"] == "zero_drawdown"
    assert result["calmar_use"] == "diagnostic_only"


def test_calmar_eight_week_window_is_marked_short_and_uses_intraday_drawdown():
    marks = [point(0, 100), point(14, 80, hour=12), point(56, 110)]
    result = calmar_diagnostic(marks, marks[0].ts, marks[-1].ts, D(100))
    with localcontext() as ctx:
        ctx.prec = 28
        expected = ((D("1.1") ** (D(365) / D(56))) - 1) / D("0.2")
    assert result["calmar"] == expected
    assert result["window_return"] == D("0.1")
    assert result["elapsed_days"] == D(56)
    assert result["short_window"] is True
    assert result["calmar_use"] == "diagnostic_only"


def test_calmar_invalid_duration_is_null_with_reason():
    marks = [point(0, 100)]
    result = calmar_diagnostic(marks, marks[0].ts, marks[0].ts, D(100))
    assert result["calmar"] is None
    assert result["calmar_unavailable_reason"] == "invalid_duration"


def test_calmar_nonpositive_equity_is_null_with_reason():
    marks = [point(0, 100), point(1, 0)]
    result = calmar_diagnostic(marks, marks[0].ts, marks[-1].ts, D(100))
    assert result["calmar"] is None
    assert result["calmar_unavailable_reason"] == "invalid_marked_equity"
