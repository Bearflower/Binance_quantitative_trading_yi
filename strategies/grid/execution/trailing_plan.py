"""Pure bounded trailing rules over immutable, signed absolute grid indices.

Only new edge orders use the fixed quote budget. Existing fills and their paired
quantities are deliberately outside this module's inputs and outputs.
"""
from dataclasses import dataclass
from decimal import Decimal, DecimalException, ROUND_FLOOR
from typing import Optional

from .planner import PlanLimitExceeded


@dataclass(frozen=True)
class TrailingPolicy:
    raw_lower: Decimal
    raw_upper: Decimal
    grid_count: int
    mode: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    stop_low: Decimal
    stop_high: Decimal
    move_up: Optional[Decimal]
    move_down: Optional[Decimal]
    quote_per_order: Decimal


@dataclass(frozen=True)
class Shift:
    direction: str
    lower_index: int
    upper_index: int
    remove_index: int
    add_index: int
    side: str
    price: Decimal
    quantity: Decimal
    stop_low: Decimal
    stop_high: Decimal


def _positive(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError('expected finite positive Decimal')


def _index(value: int) -> None:
    if type(value) is not int:
        raise ValueError('grid index must be an integer')


def _validate(policy: TrailingPolicy) -> None:
    _index(policy.grid_count)
    if policy.grid_count <= 0 or policy.mode not in ('ARITHMETIC', 'GEOMETRIC'):
        raise ValueError('invalid grid count or mode')
    for value in (policy.raw_lower, policy.raw_upper, policy.tick_size,
                  policy.step_size, policy.min_qty, policy.min_notional,
                  policy.stop_low, policy.stop_high, policy.quote_per_order):
        _positive(value)
    if not policy.stop_low < policy.raw_lower < policy.raw_upper < policy.stop_high:
        raise ValueError('invalid grid or stop boundaries')
    if policy.move_up is not None:
        _positive(policy.move_up)
        if not policy.raw_upper <= policy.move_up < policy.stop_high:
            raise ValueError('invalid upper movement cap')
    if policy.move_down is not None:
        _positive(policy.move_down)
        if not policy.stop_low < policy.move_down <= policy.raw_lower:
            raise ValueError('invalid lower movement cap')


def _level(policy: TrailingPolicy, index: int) -> Decimal:
    try:
        if policy.mode == 'ARITHMETIC':
            raw = policy.raw_lower + (policy.raw_upper - policy.raw_lower) * Decimal(index) / Decimal(policy.grid_count)
        else:
            raw = policy.raw_lower * ((policy.raw_upper / policy.raw_lower) **
                                      (Decimal(index) / Decimal(policy.grid_count)))
        value = (raw / policy.tick_size).to_integral_value(rounding=ROUND_FLOOR) * policy.tick_size
        _positive(value)
        return value
    except DecimalException as exc:
        raise ValueError('unrepresentable grid price') from exc


def price_at(policy: TrailingPolicy, index: int) -> Decimal:
    """Floor an absolute level to the tick, never deriving it from rounded levels."""
    _validate(policy)
    _index(index)
    value = _level(policy, index)
    following = _level(policy, index + 1)
    if value >= following:
        raise ValueError('rounded grid levels overlap')
    # No predecessor exists in the positive arithmetic domain at its lowest tick.
    if policy.mode == 'GEOMETRIC' or (
            policy.raw_lower + (policy.raw_upper - policy.raw_lower) *
            Decimal(index - 1) / Decimal(policy.grid_count) >= policy.tick_size):
        if _level(policy, index - 1) >= value:
            raise ValueError('rounded grid levels overlap')
    return value


def termination(price: Decimal, stop_low: Decimal, stop_high: Decimal) -> Optional[str]:
    """Inclusive whole-grid boundaries, also applicable when inventory is empty."""
    for value in (price, stop_low, stop_high):
        _positive(value)
    if stop_low >= stop_high:
        raise ValueError('invalid stop boundaries')
    if price <= stop_low:
        return 'LOW'
    if price >= stop_high:
        return 'HIGH'
    return None


def new_order_quantity(policy: TrailingPolicy, price: Decimal) -> Decimal:
    """Size only a new edge order; floor quantity before minimum-filter checks."""
    _validate(policy)
    _positive(price)
    try:
        quantity = (policy.quote_per_order / price / policy.step_size).to_integral_value(
            rounding=ROUND_FLOOR) * policy.step_size
        if quantity < policy.min_qty:
            raise PlanLimitExceeded('new order quantity below minimum')
        if quantity * price < policy.min_notional:
            raise PlanLimitExceeded('new order notional below minimum')
        return quantity
    except DecimalException as exc:
        raise ValueError('unrepresentable order quantity') from exc


def _stops(policy: TrailingPolicy, lower: Decimal, upper: Decimal):
    low, high = policy.stop_low, policy.stop_high
    if policy.move_up is not None and policy.move_down is None:
        low += lower - price_at(policy, 0)
    elif policy.move_down is not None and policy.move_up is None:
        high += upper - price_at(policy, policy.grid_count)
    if not 0 < low < lower < upper < high:
        raise ValueError('stop boundaries do not bracket window')
    return low, high


def next_shift(policy: TrailingPolicy, lower_index: int, upper_index: int,
               price: Decimal) -> Optional[Shift]:
    """Return at most one shift; reaching a movement cap never terminates a grid."""
    _validate(policy)
    _index(lower_index)
    _index(upper_index)
    _positive(price)
    if upper_index - lower_index != policy.grid_count:
        raise ValueError('window span does not match grid count')
    lower, upper = price_at(policy, lower_index), price_at(policy, upper_index)
    low, high = _stops(policy, lower, upper)
    if termination(price, low, high):
        return None
    direction = None
    if policy.move_up is not None and price >= price_at(policy, upper_index + 1):
        if price_at(policy, upper_index + 1) <= policy.move_up:
            direction = 'UP'
    elif policy.move_down is not None:
        # An arithmetic predecessor may leave the positive-price domain before
        # the next full step fits above the cap. That means no move is possible;
        # do not ask price_at to validate this unexecutable candidate.
        if policy.mode == 'ARITHMETIC':
            raw_predecessor = policy.raw_lower + (
                policy.raw_upper - policy.raw_lower
            ) * Decimal(lower_index - 1) / Decimal(policy.grid_count)
            if raw_predecessor < policy.move_down:
                return None
        predecessor = price_at(policy, lower_index - 1)
        if price <= predecessor and predecessor >= policy.move_down:
            direction = 'DOWN'
    if direction is None:
        return None
    delta = 1 if direction == 'UP' else -1
    new_lower, new_upper = lower_index + delta, upper_index + delta
    shifted_low, shifted_high = _stops(
        policy, price_at(policy, new_lower), price_at(policy, new_upper))
    remove, add, side = ((lower_index, upper_index, 'BUY') if delta == 1 else
                         (upper_index, lower_index, 'SELL'))
    order_price = price_at(policy, add)
    return Shift(direction, new_lower, new_upper, remove, add, side, order_price,
                 new_order_quantity(policy, order_price), shifted_low, shifted_high)
