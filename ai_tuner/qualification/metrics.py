"""Pure risk metrics for a replay's marked-equity curve.

The replay engine owns price freshness and midnight marks. This module consumes
those marks and fails closed when a required observation is absent or invalid.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Optional, Sequence, Tuple

from ai_tuner.replay.models import DataBlocked, EquityPoint


UTC = timezone.utc
ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise DataBlocked(["naive_timestamp"])
    return ts.astimezone(UTC)


def _positive(value: Decimal, reason: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
        raise DataBlocked([reason])


def _selected_points(
    points: Sequence[EquityPoint], start: datetime, end: datetime
) -> list[Tuple[datetime, Decimal]]:
    first, last = _utc(start), _utc(end)
    if first > last:
        raise DataBlocked(["invalid_duration"])
    selected = []
    for point in points:
        ts = _utc(point.ts)
        if first <= ts <= last:
            _positive(point.marked_equity, "invalid_marked_equity")
            selected.append((ts, point.marked_equity))
    return sorted(selected, key=lambda item: item[0])


def _all_points(points: Sequence[EquityPoint]) -> list[Tuple[datetime, Decimal]]:
    if not points:
        raise DataBlocked(["equity_curve_empty"])
    selected = []
    for point in points:
        ts = _utc(point.ts)
        _positive(point.marked_equity, "invalid_marked_equity")
        selected.append((ts, point.marked_equity))
    return sorted(selected, key=lambda item: item[0])


def daily_returns(
    points: Sequence[EquityPoint], start: datetime, end: datetime
) -> Tuple[Tuple[Decimal, ...], datetime, datetime]:
    """Return daily changes between every complete UTC midnight in the window."""
    first, last = _utc(start), _utc(end)
    if first >= last:
        raise DataBlocked(["invalid_duration"])
    midnight = first.replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight < first:
        midnight += timedelta(days=1)
    final_midnight = last.replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight >= final_midnight:
        raise DataBlocked(["insufficient_daily_boundaries"])

    boundaries = {}
    for ts, equity in _selected_points(points, midnight, final_midnight):
        if ts.time() != datetime.min.time():
            continue
        if ts in boundaries:
            raise DataBlocked(["duplicate_daily_boundary:{0}".format(ts.isoformat())])
        boundaries[ts] = equity

    values = []
    day = midnight
    while day <= final_midnight:
        if day not in boundaries:
            raise DataBlocked(["daily_equity_gap:{0}".format(day.isoformat())])
        values.append(boundaries[day])
        day += timedelta(days=1)
    with localcontext() as ctx:
        ctx.prec = 28
        returns = tuple(
            values[index] / values[index - 1] - ONE
            for index in range(1, len(values))
        )
    return returns, midnight, final_midnight


def sortino(returns: Sequence[Decimal]) -> Tuple[Optional[Decimal], Optional[str]]:
    """Annualized Sortino with zero daily MAR and all days in the denominator."""
    if not returns:
        return None, "insufficient_daily_returns"
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in returns):
        return None, "invalid_daily_return"
    with localcontext() as ctx:
        ctx.prec = 28
        count = Decimal(len(returns))
        mean = sum(returns, ZERO) / count
        downside_sum = sum((min(value, ZERO) ** 2 for value in returns), ZERO)
        if downside_sum == ZERO:
            return None, "no_downside_observation"
        deviation = (downside_sum / count).sqrt()
        return mean / deviation * Decimal(365).sqrt(), None


def max_drawdown_pct(points: Sequence[EquityPoint], initial_equity: Decimal) -> Decimal:
    """Maximum peak-to-trough loss, including the initial capital as a peak."""
    _positive(initial_equity, "invalid_initial_equity")
    selected = _all_points(points)
    with localcontext() as ctx:
        ctx.prec = 28
        peak = initial_equity
        worst = ZERO
        for _, equity in selected:
            worst = max(worst, (peak - equity) / peak)
            peak = max(peak, equity)
        return HUNDRED * worst


def max_capital_loss_pct(points: Sequence[EquityPoint], initial_equity: Decimal) -> Decimal:
    """Worst loss relative to initial capital, regardless of later recovery."""
    _positive(initial_equity, "invalid_initial_equity")
    selected = _all_points(points)
    with localcontext() as ctx:
        ctx.prec = 28
        equities = [equity for _, equity in selected]
        return HUNDRED * max(ZERO, (initial_equity - min(equities)) / initial_equity)


def calmar_diagnostic(
    points: Sequence[EquityPoint], start: datetime, end: datetime, initial_equity: Decimal
) -> dict:
    """Diagnostic-only annualized return divided by intraday drawdown fraction."""
    result = {
        "calmar": None,
        "calmar_unavailable_reason": None,
        "calmar_use": "diagnostic_only",
        "short_window": None,
        "window_return": None,
        "elapsed_days": None,
    }
    try:
        _positive(initial_equity, "invalid_initial_equity")
        selected = _selected_points(points, start, end)
    except DataBlocked as error:
        result["calmar_unavailable_reason"] = error.gaps[0].split(":", 1)[0]
        return result
    if len(selected) < 2:
        result["calmar_unavailable_reason"] = "invalid_duration"
        return result
    first_ts, first_equity = selected[0]
    last_ts, last_equity = selected[-1]
    elapsed = last_ts - first_ts
    if elapsed <= timedelta(0):
        result["calmar_unavailable_reason"] = "invalid_duration"
        return result
    with localcontext() as ctx:
        ctx.prec = 28
        days = (
            Decimal(elapsed.days)
            + Decimal(elapsed.seconds) / Decimal(86400)
            + Decimal(elapsed.microseconds) / Decimal(86400000000)
        )
        result["elapsed_days"] = days
        result["short_window"] = days < Decimal(365)
        result["window_return"] = last_equity / first_equity - ONE
        peak = initial_equity
        worst = ZERO
        for _, equity in selected:
            worst = max(worst, (peak - equity) / peak)
            peak = max(peak, equity)
        if worst == ZERO:
            result["calmar_unavailable_reason"] = "zero_drawdown"
            return result
        annualized = (last_equity / first_equity) ** (Decimal(365) / days) - ONE
        result["calmar"] = annualized / worst
    return result
