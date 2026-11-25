"""Conservative one-way position and resting-order exposure accounting."""

from dataclasses import dataclass
from decimal import Decimal

from .planner import HardLimits, PlanLimitExceeded


@dataclass(frozen=True)
class Exposure:
    notional: Decimal
    position_notional: Decimal
    order_count: int


def projected_exposure(positions, orders, mark_price, journal_position) -> Exposure:
    if (len(positions) != 1 or positions[0].get("positionSide") != "BOTH"
            or not mark_price.is_finite() or mark_price <= 0):
        raise ValueError("unverified exposure inputs")
    quantities = [Decimal(str(positions[0]["positionAmt"])), journal_position]
    buy = sell = Decimal(0)
    price = mark_price
    ordinary = [o for o in orders if o["role"] in ("ENTRY", "REVERSE")]
    for order in ordinary:
        remaining = Decimal(order["quantity"]) - Decimal(order["executed_qty"])
        limit_price = Decimal(order["price"])
        if (not remaining.is_finite() or remaining < 0
                or not limit_price.is_finite() or limit_price <= 0):
            raise ValueError("invalid order exposure")
        price = max(price, limit_price)
        if order["side"] == "BUY":
            buy += remaining
        elif order["side"] == "SELL":
            sell += remaining
        else:
            raise ValueError("invalid order exposure side")
    if any(not q.is_finite() for q in quantities):
        raise ValueError("invalid position exposure")
    # The journal and exchange can observe an in-flight fill at different times.
    # Retain the larger risk envelope rather than dropping unconfirmed quantity.
    worst_qty = max(max(abs(q), abs(q + buy), abs(q - sell)) for q in quantities)
    position_notional = max(abs(q) for q in quantities) * mark_price
    return Exposure(worst_qty * price, position_notional, len(ordinary))


def validate_exposure(exposure: Exposure, limits: HardLimits) -> None:
    if exposure.order_count > limits.max_open_orders:
        raise PlanLimitExceeded("live ordinary order count exceeds limit")
    cap = min(limits.max_notional_usdt,
              limits.max_margin_usdt * Decimal(limits.leverage))
    if exposure.notional > cap:
        raise PlanLimitExceeded("live position and order exposure exceeds budget")
