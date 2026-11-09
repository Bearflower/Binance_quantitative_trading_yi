"""Pure order plan from V2.5.5 grid parameters, with conservative whole-plan caps."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Tuple

from strategies.grid.grid_calculator import CapitalFeasibility, DynamicGridParams, GridMode


@dataclass(frozen=True)
class ExchangeFilters:
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class HardLimits:
    max_margin_usdt: Decimal
    max_notional_usdt: Decimal
    max_open_orders: int
    leverage: int


@dataclass(frozen=True)
class PlannedOrder:
    level_index: int
    side: str
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class GridPlan:
    orders: Tuple[PlannedOrder, ...]
    levels: Tuple[Decimal, ...]
    max_notional_usdt: Decimal
    max_margin_usdt: Decimal


def _round(value: Decimal, step: Decimal, rounding) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def build_plan(params: DynamicGridParams, feasibility: CapitalFeasibility,
               market_price: Decimal, exchange_filters: ExchangeFilters,
               hard_limits: HardLimits) -> GridPlan:
    if not feasibility.feasible or feasibility.grid_count != params.grid_count:
        raise ValueError("capital feasibility mismatch")
    if market_price <= 0 or not params.lower_boundary < market_price < params.upper_boundary:
        raise ValueError("market price outside grid bounds")
    if any(value <= 0 for value in (exchange_filters.tick_size, exchange_filters.step_size,
                                    exchange_filters.min_qty, exchange_filters.min_notional)):
        raise ValueError("invalid exchange filters")
    if (hard_limits.max_margin_usdt <= 0 or hard_limits.max_notional_usdt <= 0
            or hard_limits.max_open_orders <= 0 or hard_limits.leverage <= 0):
        raise ValueError("invalid hard limits")
    if not params.stop_loss_low < params.lower_boundary or not params.stop_loss_high > params.upper_boundary:
        raise ValueError("invalid stop boundaries")

    qty = _round(feasibility.qty_per_grid, exchange_filters.step_size, ROUND_FLOOR)
    if qty < exchange_filters.min_qty:
        raise ValueError("quantity below minimum")
    raw_levels = []
    for index in range(params.grid_count + 1):
        if params.grid_mode == GridMode.ARITHMETIC:
            raw = params.lower_boundary + (
                params.upper_boundary - params.lower_boundary
            ) * Decimal(index) / Decimal(params.grid_count)
        elif params.grid_mode == GridMode.GEOMETRIC:
            ratio = params.upper_boundary / params.lower_boundary
            raw = params.lower_boundary * (ratio ** (Decimal(index) / Decimal(params.grid_count)))
        else:
            raise ValueError("unknown grid mode")
        raw_levels.append(raw)

    levels = tuple(_round(raw, exchange_filters.tick_size, ROUND_FLOOR) for raw in raw_levels)
    if any(left >= right for left, right in zip(levels, levels[1:])):
        raise ValueError("rounded grid levels overlap")
    if min((right - left) / left for left, right in zip(levels, levels[1:])) < params.profit_rate * Decimal("0.95"):
        raise ValueError("rounded profit rate below plan")

    orders = []
    for index, price in enumerate(levels):
        if price == market_price:
            continue
        side = "BUY" if price < market_price else "SELL"
        if price * qty < exchange_filters.min_notional:
            raise ValueError("order notional below minimum")
        orders.append(PlannedOrder(index, side, price, qty))
    if not orders or len(orders) > hard_limits.max_open_orders:
        raise ValueError("open order count exceeds limit")
    total_notional = sum((order.price * order.quantity for order in orders), Decimal(0))
    margin = total_notional / Decimal(hard_limits.leverage)
    if total_notional > hard_limits.max_notional_usdt:
        raise ValueError("whole-plan notional exceeds limit")
    if margin > hard_limits.max_margin_usdt:
        raise ValueError("whole-plan margin exceeds limit")
    return GridPlan(tuple(orders), levels, total_notional, margin)
