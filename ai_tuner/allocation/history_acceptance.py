"""Allocation under bounded historical gaps: preserve approved weights, never rank gaps."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
import json

from shared.accounting.report_reader import read_monthly_economic_report
from ai_tuner.allocation.allocation_calculator import AllocationEntry, AllocationResult
from ai_tuner.allocation.pnl_collector import AllocationInputUnavailable

MODE = 'maintain_approved_allocation_due_to_historical_gaps'


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('invalid_allocation_amount')
    return result


def cap_result(result, policy, reserve_ratio, *, fixed_hrs_amount):
    """Keep approved HRS principal; scale only the other three new-entry budgets.

    Even a fully verified ranking cannot change HRS capital while this acceptance
    is active: non-flat unitized capital changes require a separate NAV proof.
    """
    ceilings = policy.payload['allocation_ceilings_usdt']
    if (len(result.entries) != len(ceilings) or 'hrs' not in ceilings
            or {entry.strategy_id for entry in result.entries} != set(ceilings)):
        raise ValueError('approved_allocation_strategy_scope_mismatch')
    total = amount(result.total_capital)
    reserve = amount(reserve_ratio)
    if total <= 0 or not 0 <= reserve < 1:
        raise ValueError('allocation_pool_invalid')
    fixed = amount(fixed_hrs_amount)
    if (fixed <= 0 or fixed != fixed.quantize(Decimal('.00000001'))
            or fixed > amount(ceilings['hrs'])):
        raise ValueError('fixed_hrs_allocation_exceeds_cap_or_precision')
    remaining = total*(1-reserve)-fixed
    if remaining < 0:
        raise ValueError('allocation_pool_below_fixed_hrs')
    candidates = [(entry, min(amount(entry.allocated_amount), amount(ceilings[entry.strategy_id])))
                  for entry in result.entries if entry.strategy_id != 'hrs']
    wanted = sum((value for _,value in candidates), Decimal(0))
    scale = min(Decimal(1),remaining/wanted) if wanted else Decimal(0)
    values = {entry.strategy_id:(value*scale).quantize(Decimal('.00000001'),rounding=ROUND_DOWN)
              for entry,value in candidates}
    values['hrs'] = fixed
    entries = [replace(entry, allocated_amount=float(values[entry.strategy_id]),
                       allocated_ratio=float(values[entry.strategy_id]/total)) for entry in result.entries]
    spent = sum((Decimal(str(entry.allocated_amount)) for entry in entries), Decimal(0))
    return replace(result, entries=entries, allocatable_amount=float(spent), reserve_amount=float(total-spent),
                   history_acceptance_sha256=policy.receipt_sha256)


async def approved_entries(db, policy, month):
    row = await db.fetch_one("SELECT entries FROM public.capital_allocation WHERE status='active' "
        "AND month<$1 ORDER BY month DESC,id DESC LIMIT 1",datetime.strptime(month,'%Y-%m').date())
    if not row:
        raise AllocationInputUnavailable('approved_current_allocation_missing')
    raw = json.loads(row['entries']) if isinstance(row['entries'],str) else row['entries']
    if not isinstance(raw,list) or any(not isinstance(e,dict) for e in raw):
        raise AllocationInputUnavailable('approved_current_allocation_scope_mismatch')
    by_id = {entry.get('strategy_id'):entry for entry in raw}
    scopes = set(policy.payload['allocation_ceilings_usdt'])
    if len(by_id)!=len(raw) or set(by_id)!=scopes:
        raise AllocationInputUnavailable('approved_current_allocation_scope_mismatch')
    return by_id


async def approved_hrs_amount(db, policy, month):
    return amount((await approved_entries(db,policy,month))['hrs']['allocated_amount'])


async def retained_allocation(db, policy, start, end, total_capital, reserve_ratio, month, strategy_names):
    """Only the receipt's old interval can lack coverage; the later month tail is strict."""
    if not (policy.accepted_from <= start < policy.accepted_through and end > policy.accepted_from):
        raise AllocationInputUnavailable('allocation_window_not_in_accepted_history')
    start_ms,end_ms = int(start.timestamp()*1000), int(end.timestamp()*1000)
    report = await read_monthly_economic_report(db, policy.account_id,start_ms,end_ms,
        include_reviewed_overrides=True,include_derived_attribution=True)
    if report['attribution_conflicts'] or report['missing_income_fills'] or report['unattributed_count']:
        raise AllocationInputUnavailable('allocation_income_ownership_unresolved')
    if end > policy.accepted_through:
        tail = await read_monthly_economic_report(db,policy.account_id,
            int(policy.accepted_through.timestamp()*1000),end_ms,
            include_reviewed_overrides=True,include_derived_attribution=True)
        if tail.get('id_match_and_coverage_complete') is not True:
            raise AllocationInputUnavailable('allocation_current_window_unverified')
    by_id = await approved_entries(db,policy,month)
    scopes = set(policy.payload['allocation_ceilings_usdt'])
    if set(strategy_names)!=scopes:
        raise AllocationInputUnavailable('approved_current_allocation_scope_mismatch')
    entries = [AllocationEntry(strategy_id=sid,strategy_name=strategy_names[sid],
        realized_pnl=float(Decimal(str(report['by_strategy'].get(sid,{}).get('net_usdt','0')))),
        initial_capital=float(amount(by_id[sid]['allocated_amount'])),return_rate=None,rank=None,
        allocated_ratio=0,allocated_amount=float(amount(by_id[sid]['allocated_amount']))) for sid in sorted(scopes)]
    result = AllocationResult(month=month,total_capital=float(total_capital),reserve_amount=0,
        allocatable_amount=0,is_first_month=False,entries=entries,caliber=MODE,
        history_acceptance_sha256=policy.receipt_sha256)
    return cap_result(result,policy,reserve_ratio,fixed_hrs_amount=amount(by_id['hrs']['allocated_amount']))
