"""One executable sizing calculation: actual leverage, all levels and filters."""
from dataclasses import asdict, replace
from decimal import Decimal, ROUND_CEILING
from .planner import build_plan, price_levels, PlanLimitExceeded, _round
from strategies.grid.grid_calculator import CapitalFeasibility, GridMode


def size_grid(params, price, filters, limits, *, advisory_margin, available_margin,
              min_grid_count):
    if any(not v.is_finite() or v <= 0 for v in (advisory_margin, available_margin, price)):
        raise PlanLimitExceeded('no available execution margin')
    if (isinstance(min_grid_count, bool) or not isinstance(min_grid_count, int)
            or not 4 <= min_grid_count <= params.grid_count <= 100
            or limits.leverage <= 0):
        raise ValueError('invalid sizing grid bounds')
    if any(not v.is_finite() or v <= 0 for v in
           (filters.tick_size, filters.step_size, filters.min_qty, filters.min_notional)):
        raise ValueError('invalid exchange filters')
    cap = min(available_margin, limits.max_margin_usdt,
              limits.max_notional_usdt / limits.leverage)
    advice = min(advisory_margin, cap)
    candidates = []
    for count in range(min(params.grid_count, limits.max_open_orders - 1), min_grid_count - 1, -1):
        if params.grid_mode == GridMode.ARITHMETIC:
            spacing = (params.upper_boundary - params.lower_boundary) / count
            profit = spacing / price
        else:
            profit = (params.upper_boundary / params.lower_boundary) ** (Decimal(1) / count) - 1
            spacing = price * profit
        effective = replace(params, grid_count=count, grid_spacing=spacing, profit_rate=profit)
        _, levels = price_levels(effective, filters)
        if not levels or min(levels) <= 0:
            raise ValueError('invalid rounded price levels')
        minimum_qty = _round(max(filters.min_qty, filters.min_notional / min(levels)),
                             filters.step_size, ROUND_CEILING)
        required = sum(levels) * minimum_qty / limits.leverage
        floor_budget = _round(required, Decimal('.01'), ROUND_CEILING)
        candidates.append((effective, minimum_qty, floor_budget))
        if required <= advice:
            return _build(effective, price, filters, limits, advice, cap, advisory_margin,
                          minimum_qty, floor_budget, 'advisory')
    if candidates and candidates[-1][0].grid_count == min_grid_count:
        effective, qty, required = candidates[-1]
        if required <= cap:
            return _build(effective, price, filters, limits, required, cap, advisory_margin,
                          qty, required, 'minimum_viable_grid')
        raise PlanLimitExceeded('minimum grid exceeds account or hard cap',
                                required_margin=required, available_cap=cap)
    raise PlanLimitExceeded('minimum grid exceeds order count cap')


def _build(params, price, filters, limits, budget, cap, advisory, minimum_qty, required, source):
    # Give build_plan the available quantity ceiling, not a producer quantity
    # calculated with a different leverage. It performs the final quantization.
    _, levels = price_levels(params, filters)
    ceiling_qty = budget * limits.leverage / sum(levels)
    plan = build_plan(params, CapitalFeasibility(True, params.grid_count, ceiling_qty, 'EXECUTION'),
                      price, filters, limits, margin_budget_usdt=budget)
    if plan.orders[0].quantity < minimum_qty:
        raise PlanLimitExceeded('rounded allocation below minimum executable quantity')
    raw = asdict(params)
    raw = {k: v.name if isinstance(v, GridMode) else str(v) if isinstance(v, Decimal) else v
           for k, v in raw.items()}
    receipt = dict(version=2, budget=str(budget), advisory_budget=str(advisory),
                   exchange_filters={k: str(v) for k, v in asdict(filters).items()},
                   hard_limits={k: str(v) for k, v in asdict(limits).items()},
                   available_cap=str(cap), budget_source=source,
                   required_margin=str(required), grid_params=raw,
                   grid_count=params.grid_count, leverage=limits.leverage,
                   reserved_margin=str(sum(levels) * plan.orders[0].quantity / limits.leverage),
                   min_order_notional=str(min(levels) * plan.orders[0].quantity))
    return replace(plan, sizing=receipt)
