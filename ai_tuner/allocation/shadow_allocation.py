"""Read-only allocation preview from independently verified monthly evidence.

This module has no persistence or policy activation path. Incomplete inputs
produce reasons, never a ranking or a live allocation.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
from typing import Any, Mapping

from .allocation_calculator import AllocationCalculator
from shared.accounting.published_report import REPORT_VERSION


def _amount(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() and math.isfinite(float(amount)) else None


def preview_monthly_allocation(
    *, report: Mapping[str, Any], capital: Mapping[str, Any],
    activity: Mapping[str, Any], account_id: str, start_ms: int, end_ms: int,
    strategy_ids: tuple[str, ...], total_capital: Any,
    reserve_ratio: Any, rank_ratios: list[Any], effective_month: str,
) -> dict:
    """Return a shadow result only when all strategies have complete evidence."""
    reasons: list[str] = []
    if (not account_id or start_ms >= end_ms or not strategy_ids
            or len(set(strategy_ids)) != len(strategy_ids)):
        raise ValueError('invalid allocation identity/window')
    if (not isinstance(report, Mapping) or report.get('account_id') != account_id
            or report.get('start_ms') != start_ms or report.get('end_ms') != end_ms
            or report.get('requested_end_ms') != end_ms
            or report.get('window_key') != f'monthly:{start_ms}'
            or report.get('publication_status') != 'published'
            or report.get('refresh_error') is not None
            or report.get('report_version') != REPORT_VERSION
            or report.get('account_coverage_complete') is not True
            or report.get('strategy_attribution_complete') is not True
            or not isinstance(report.get('report_id'), str) or not report['report_id']):
        reasons.append('income_report_unverified')
    total = _amount(total_capital)
    reserve = _amount(reserve_ratio)
    ratios = ([_amount(value) for value in rank_ratios]
              if isinstance(rank_ratios, (list, tuple)) else [])
    if total is None or total <= 0:
        reasons.append('capital_pool_unverified')
    if (reserve is None or reserve < 0 or reserve >= 1
            or len(ratios) != len(strategy_ids)
            or any(value is None or value < 0 for value in ratios)
            or (reserve is not None and all(value is not None for value in ratios)
                and sum(ratios) + reserve > 1)):
        reasons.append('allocation_ratios_invalid')
    pnl_data = {}
    by_strategy = report.get('by_strategy') if isinstance(report, Mapping) else None
    for strategy in strategy_ids:
        income = by_strategy.get(strategy) if isinstance(by_strategy, Mapping) else None
        net = _amount(income.get('net_usdt')) if isinstance(income, Mapping) else None
        if net is None:
            reasons.append(f'{strategy}:net_unverified')
        capital_row = capital.get(strategy) if isinstance(capital, Mapping) else None
        basis = (_amount(capital_row.get('average_capital_usdt'))
                 if isinstance(capital_row, Mapping) else None)
        if (basis is None or basis <= 0 or not isinstance(capital_row, Mapping)
                or capital_row.get('coverage_complete') is not True
                or not capital_row.get('source_id')
                or capital_row.get('account_id') != account_id
                or capital_row.get('start_ms') != start_ms
                or capital_row.get('end_ms') != end_ms):
            reasons.append(f'{strategy}:capital_unverified')
        activity_row = activity.get(strategy) if isinstance(activity, Mapping) else None
        fills = (activity_row.get('owned_entry_fills')
                 if isinstance(activity_row, Mapping) else None)
        if (type(fills) is not int or fills < 0
                or activity_row.get('coverage_complete') is not True
                or not activity_row.get('source_id')
                or activity_row.get('account_id') != account_id
                or activity_row.get('start_ms') != start_ms
                or activity_row.get('end_ms') != end_ms):
            reasons.append(f'{strategy}:activity_unverified')
        if net is not None and basis is not None and basis > 0 and type(fills) is int and fills >= 0:
            pnl_data[strategy] = {'pnl': float(net), 'capital': float(basis),
                                  'has_trades': fills > 0}
    if reasons:
        return {'status': 'BLOCKED_DATA', 'reasons': sorted(set(reasons)),
                'allocation': None, 'mode': 'shadow_only'}
    result = AllocationCalculator().calculate(
        total_capital=float(total), pnl_data=pnl_data, is_first_month=False,
        fallback_ratios={}, fallback_capitals={},
        rank_ratios=[float(value) for value in ratios],
        reserve_ratio=float(reserve),
        strategy_names={strategy: strategy for strategy in strategy_ids},
        month=effective_month,
    )
    return {
        'status': 'PREVIEW', 'mode': 'shadow_only', 'reasons': [],
        'report_id': report['report_id'],
        'allocation': {
            'month': result.month,
            'reserve_amount': result.reserve_amount,
            'entries': [dict(strategy_id=entry.strategy_id, rank=entry.rank,
                             return_rate=entry.return_rate,
                             allocated_ratio=entry.allocated_ratio,
                             allocated_amount=entry.allocated_amount)
                        for entry in result.entries],
        },
    }
