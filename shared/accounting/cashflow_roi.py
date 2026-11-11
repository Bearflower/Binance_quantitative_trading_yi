"""Strict, side-effect-free account cashflow bridge and time-weighted return.

The caller must supply independently collected, complete exchange facts. This
module cannot turn daily equity snapshots into event-time valuations.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


def _number(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def evaluate_account_roi(
    *, start_equity_usd: Any, end_equity_usd: Any,
    cashflows: Sequence[Mapping[str, Any]], income_net_usd: Any,
    unrealized_change_usd: Any, bridge_tolerance_usd: Any,
    coverage_complete: bool, start_watermark: Any, end_watermark: Any,
) -> dict:
    """Return ROI only if cashflows, event valuations and equity bridge agree."""
    reasons: list[str] = []
    start = _number(start_equity_usd)
    end = _number(end_equity_usd)
    income = _number(income_net_usd)
    unrealized = _number(unrealized_change_usd)
    tolerance = _number(bridge_tolerance_usd)
    if (start is None or start <= 0 or end is None or end < 0):
        reasons.append('equity_anchor_unverified')
    if income is None or unrealized is None:
        reasons.append('income_or_unrealized_unverified')
    if tolerance is None or tolerance < 0:
        reasons.append('bridge_tolerance_invalid')
    if coverage_complete is not True or not start_watermark or not end_watermark:
        reasons.append('cashflow_coverage_unverified')
    if not isinstance(cashflows, (list, tuple)):
        reasons.append('cashflow_events_unverified')
        cashflows = []
    seen: set[tuple[str, str]] = set()
    external_net = Decimal(0)
    timed_external = []
    previous_time = None
    for event in cashflows:
        if not isinstance(event, Mapping):
            reasons.append('cashflow_event_invalid')
            continue
        source = event.get('source')
        event_id = event.get('event_id')
        valid_identity = isinstance(source, str) and bool(source) and event_id is not None
        identity = (source, str(event_id)) if valid_identity else ('', '')
        if not valid_identity or identity in seen:
            reasons.append('cashflow_identity_unverified')
        if valid_identity:
            seen.add(identity)
        event_time = event.get('exchange_time_ms')
        if (type(event_time) is not int or event_time <= 0
                or (previous_time is not None and event_time <= previous_time)):
            reasons.append('cashflow_order_unverified')
        else:
            previous_time = event_time
        classification = event.get('boundary')
        if classification not in ('external_in', 'external_out', 'internal'):
            reasons.append('cashflow_boundary_unverified')
            continue
        value = _number(event.get('value_usd'))
        if value is None or value <= 0 or not event.get('valuation_id'):
            reasons.append('cashflow_valuation_unverified')
            continue
        if classification == 'internal':
            continue
        signed = value if classification == 'external_in' else -value
        external_net += signed
        pre = _number(event.get('pre_equity_usd'))
        post = _number(event.get('post_equity_usd'))
        if pre is None or pre <= 0 or post is None or post <= 0:
            reasons.append('event_time_equity_unverified')
            continue
        if tolerance is not None and abs(post - pre - signed) > tolerance:
            reasons.append('event_equity_bridge_mismatch')
        timed_external.append((pre, post))
    bridge_residual = None
    if None not in (start, end, income, unrealized):
        bridge_residual = end - start - external_net - income - unrealized
        if tolerance is not None and abs(bridge_residual) > tolerance:
            reasons.append('account_equity_bridge_mismatch')
    if reasons:
        return {'status': 'UNKNOWN', 'yield': None,
                'unavailable_reasons': sorted(set(reasons)),
                'bridge_residual_usd': str(bridge_residual) if bridge_residual is not None else None,
                'cashflow_count': len(cashflows)}
    product = Decimal(1)
    previous_equity = start
    for pre, post in timed_external:
        product *= pre / previous_equity
        previous_equity = post
    product *= end / previous_equity
    return {'status': 'VERIFIED', 'yield': str(product - 1),
            'unavailable_reasons': [], 'bridge_residual_usd': str(bridge_residual),
            'cashflow_count': len(cashflows), 'method': 'time_weighted_return'}
