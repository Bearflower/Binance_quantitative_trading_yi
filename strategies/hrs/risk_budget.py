"""Pure, fail-closed sizing for HRS orders against HRS-allocated capital."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    quantity: Decimal = Decimal("0")
    worst_loss: Decimal = Decimal("0")
    risk_limit: Decimal = Decimal("0")


def _valid_positive(value: Optional[Decimal]) -> bool:
    return value is not None and value.is_finite() and value > 0


def calculate_open_position_risk(
    positions: Mapping[str, Mapping[str, Any]], *, fee_rate: Decimal,
    pressure_slippage: Decimal,
) -> Optional[Decimal]:
    """Sum stressed stop exposure; unknown protection blocks new risk."""
    if (fee_rate is None or not fee_rate.is_finite() or fee_rate < 0
            or pressure_slippage is None or not pressure_slippage.is_finite()
            or pressure_slippage < 0 or pressure_slippage >= 1):
        return None
    total = Decimal("0")
    for position in positions.values():
        try:
            quantity = Decimal(str(position.get("remaining_quantity")))
            entry = Decimal(str(position.get("entry_price")))
            stop = Decimal(str(position.get("risk_stop_price")))
            direction = position.get("direction")
            sl_id = position.get("algo_ids", {}).get("sl")
        except (ArithmeticError, TypeError, ValueError, AttributeError):
            return None
        if (not _valid_positive(quantity) or not _valid_positive(entry)
                or not _valid_positive(stop) or not sl_id
                or direction not in {"short", "long"}):
            return None
        adverse_stop = stop * (
            Decimal("1") + pressure_slippage if direction == "short"
            else Decimal("1") - pressure_slippage
        )
        total += quantity * (abs(adverse_stop - entry)
                             + fee_rate * (entry + adverse_stop))
    return total


def assess_entry(
    *, allocation: Optional[Decimal], entry_price: Decimal, stop_price: Decimal,
    direction: str, existing_open_risk: Decimal, leverage: Decimal,
    remaining_margin: Decimal, qty_step: Decimal, min_qty: Decimal,
    min_notional: Decimal, fee_rate: Decimal, pressure_slippage: Decimal,
    single_risk_ratio: Decimal = Decimal("0.01"),
    portfolio_risk_ratio: Decimal = Decimal("0.03"),
    take_profit_ratios: Tuple[Decimal, Decimal] = (Decimal("0.30"), Decimal("0.40")),
) -> RiskDecision:
    """Return a rounded order size whose stressed stop loss fits both budgets.

    The loss estimate includes entry and exit fees and adverse execution beyond
    the stop trigger. It is a sizing constraint, not a guarantee of maximum loss.
    """
    if not _valid_positive(allocation):
        return RiskDecision(False, "invalid_allocation")
    positives = (entry_price, stop_price, leverage, remaining_margin, qty_step,
                 min_qty, min_notional, single_risk_ratio, portfolio_risk_ratio)
    nonnegative = (existing_open_risk, fee_rate, pressure_slippage)
    if (direction not in {"short", "long"}
            or any(not _valid_positive(value) for value in positives)
            or any(value is None or not value.is_finite() or value < 0
                   for value in nonnegative)
            or pressure_slippage >= 1
            or len(take_profit_ratios) != 2
            or any(not _valid_positive(ratio) or ratio >= 1 for ratio in take_profit_ratios)):
        return RiskDecision(False, "invalid_inputs")
    if (direction == "short" and stop_price <= entry_price) or (
        direction == "long" and stop_price >= entry_price
    ):
        return RiskDecision(False, "stop_not_protective")

    risk_limit = min(allocation * single_risk_ratio,
                     allocation * portfolio_risk_ratio - existing_open_risk)
    if risk_limit <= 0:
        return RiskDecision(False, "portfolio_risk_exhausted", risk_limit=risk_limit)

    adverse_stop = stop_price * (
        Decimal("1") + pressure_slippage if direction == "short"
        else Decimal("1") - pressure_slippage
    )
    per_unit_loss = abs(adverse_stop - entry_price) + fee_rate * (entry_price + adverse_stop)
    if per_unit_loss <= 0:
        return RiskDecision(False, "invalid_inputs")
    raw_quantity = min(risk_limit / per_unit_loss, remaining_margin * leverage / entry_price)
    if raw_quantity < min_qty:
        return RiskDecision(False, "minimum_order_exceeds_risk_budget", risk_limit=risk_limit)
    quantity = min_qty + (
        (raw_quantity - min_qty) / qty_step
    ).to_integral_value(rounding=ROUND_FLOOR) * qty_step
    if quantity < min_qty or quantity * entry_price < min_notional:
        return RiskDecision(False, "minimum_order_exceeds_risk_budget", risk_limit=risk_limit)
    for ratio in take_profit_ratios:
        tp_quantity = (quantity * ratio / qty_step).to_integral_value(rounding=ROUND_FLOOR) * qty_step
        if tp_quantity < min_qty:
            return RiskDecision(False, "take_profit_quantity_unavailable", risk_limit=risk_limit)
    return RiskDecision(True, "allowed", quantity=quantity,
                        worst_loss=quantity * per_unit_loss, risk_limit=risk_limit)
