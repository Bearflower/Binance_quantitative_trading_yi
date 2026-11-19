"""Pure, fail-closed sizing for HRS orders against HRS-allocated capital."""

from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
from typing import Any, Literal, Mapping, Optional, Tuple


STRICT = "hrs_percent_v1"
FLOOR = "hrs_minimum_executable_v1"
BudgetVersion = Literal["hrs_percent_v1", "hrs_minimum_executable_v1"]


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    quantity: Decimal = Decimal("0")
    worst_loss: Decimal = Decimal("0")
    risk_limit: Decimal = Decimal("0")
    exit_ratios: Tuple[Decimal, Decimal] = (Decimal("0"), Decimal("0"))
    policy_revision: int = 0
    client_order_id: str = ""
    effective_allocation: Decimal = Decimal("0")
    risk_allocation: Optional[Decimal] = None
    budget_version: BudgetVersion = STRICT
    minimum_quantity: Decimal = Decimal("0")
    minimum_risk: Decimal = Decimal("0")
    base_single_limit: Decimal = Decimal("0")
    base_portfolio_limit: Decimal = Decimal("0")
    effective_single_limit: Decimal = Decimal("0")
    effective_portfolio_limit: Decimal = Decimal("0")
    budget_inputs: Mapping[str, Any] = field(default_factory=dict)


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


def _assess_percent_entry(
    *, allocation: Optional[Decimal], entry_price: Decimal, stop_price: Decimal,
    direction: str, existing_open_risk: Decimal, leverage: Decimal,
    remaining_margin: Decimal, qty_step: Decimal, min_qty: Decimal,
    min_notional: Decimal, fee_rate: Decimal, pressure_slippage: Decimal,
    single_risk_ratio: Decimal = Decimal("0.01"),
    portfolio_risk_ratio: Decimal = Decimal("0.03"),
    take_profit_ratios: Tuple[Decimal, Decimal] = (Decimal("0.30"), Decimal("0.40")),
    take_profit_prices: Optional[Tuple[Decimal, Decimal]] = None,
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
            or any(ratio is None or not ratio.is_finite() or ratio < 0 for ratio in take_profit_ratios)
            or not ((take_profit_ratios == (Decimal(1), Decimal(0)))
                    or (all(0 < ratio < 1 for ratio in take_profit_ratios)
                        and sum(take_profit_ratios) < 1))):
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
    from shared.entry_risk_policy import EntryDenied, executable_exit_ratios
    tp_prices = take_profit_prices or (entry_price, entry_price)
    if len(tp_prices) != 2 or any(not _valid_positive(price) for price in tp_prices):
        return RiskDecision(False, "invalid_inputs")
    try:
        if tuple(take_profit_ratios) == (Decimal(1), Decimal(0)):
            # Explicit full TP1 is already a supported HRS executable fallback.
            # Do not ask the shared split/tail planner to manufacture a tail.
            if quantity < min_qty or quantity * tp_prices[0] < min_notional:
                return RiskDecision(False, "take_profit_quantity_unavailable", risk_limit=risk_limit)
            exit_ratios = (Decimal(1), Decimal(0))
        else:
            exit_ratios = executable_exit_ratios(
                quantity, qty_step, min_qty, min_notional,
                [*tp_prices, adverse_stop], take_profit_ratios,
            )
        if exit_ratios != tuple(take_profit_ratios):
            # HRS has no software tail exit for a missing TP2. Use one full
            # TP1 instead of carrying a residual whose intended TP2 is absent.
            if quantity < min_qty or quantity * tp_prices[0] < min_notional:
                return RiskDecision(False, "take_profit_quantity_unavailable", risk_limit=risk_limit)
            exit_ratios = (Decimal(1), Decimal(0))
    except EntryDenied:
        return RiskDecision(False, "take_profit_quantity_unavailable", risk_limit=risk_limit)
    return RiskDecision(True, "allowed", quantity=quantity,
                        worst_loss=quantity * per_unit_loss, risk_limit=risk_limit,
                        exit_ratios=exit_ratios, risk_allocation=allocation)


_NUMERIC_INPUTS = (
    "allocation", "entry_price", "stop_price", "existing_open_risk", "leverage",
    "remaining_margin", "qty_step", "min_qty", "min_notional", "fee_rate",
    "pressure_slippage", "single_risk_ratio", "portfolio_risk_ratio",
)
_INPUT_KEYS = frozenset((*_NUMERIC_INPUTS, "direction", "take_profit_ratios",
                         "take_profit_prices", "budget_version"))


def policy_budget_version(policy: Mapping[str, Any]) -> BudgetVersion:
    """Only an explicit reason JSON contract can opt a policy into the floor.

    Historical prose is strict, regardless of its contents or policy revision.
    Malformed structured reasons and unknown declarations fail closed.
    """
    reason = policy.get("reason")
    if reason is None or reason == "":
        return STRICT
    if not isinstance(reason, str):
        raise ValueError("invalid HRS risk budget policy reason")
    reason = reason.strip()
    try:
        contract = json.loads(reason)
    except (ValueError, TypeError) as exc:
        if reason.startswith(("{", "[")):
            raise ValueError("malformed HRS risk budget policy reason") from exc
        return STRICT
    if not isinstance(contract, dict):
        raise ValueError("invalid HRS risk budget policy contract")
    version = contract.get("hrs_risk_budget_version", STRICT)
    if not isinstance(version, str) or version not in (STRICT, FLOOR):
        raise ValueError("unknown HRS risk budget version")
    return version


def _finite_decimal(value: Any) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _capturable(inputs: Mapping[str, Any]) -> bool:
    if not all(_finite_decimal(inputs[key]) for key in _NUMERIC_INPUTS):
        return False
    for key in ("take_profit_ratios", "take_profit_prices"):
        values = inputs[key]
        if key == "take_profit_prices" and values is None:
            continue
        if (not isinstance(values, (tuple, list)) or len(values) != 2
                or not all(_finite_decimal(value) for value in values)):
            return False
    return True


def assess_budget_inputs(inputs: Mapping[str, Any]) -> RiskDecision:
    """Reconstruct an exact assessor contract from normalized JSON audit data.

    Callers may replace current allocation, margin and open risk before re-running
    this helper. Unknown/missing fields and ambiguous numeric types are rejected.
    """
    if not isinstance(inputs, Mapping) or set(inputs) != _INPUT_KEYS:
        raise ValueError("incomplete HRS risk budget input contract")
    restored = dict(inputs)

    def decimal(value):
        if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
            raise ValueError("invalid HRS risk budget decimal")
        try:
            result = Decimal(value)
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("invalid HRS risk budget decimal") from exc
        if not result.is_finite():
            raise ValueError("nonfinite HRS risk budget decimal")
        return result

    for key in _NUMERIC_INPUTS:
        restored[key] = decimal(restored[key])
    for key in ("take_profit_ratios", "take_profit_prices"):
        if key == "take_profit_prices" and restored[key] is None:
            continue
        if not isinstance(restored[key], (tuple, list)) or len(restored[key]) != 2:
            raise ValueError("invalid HRS risk budget exit contract")
        restored[key] = tuple(decimal(value) for value in restored[key])
    if not isinstance(restored["direction"], str) or restored["direction"] not in ("short", "long"):
        raise ValueError("invalid HRS risk budget direction")
    if restored["budget_version"] not in (STRICT, FLOOR):
        raise ValueError("unknown HRS risk budget version")
    return assess_entry(**restored)


def assess_entry(
    *, allocation: Optional[Decimal], entry_price: Decimal, stop_price: Decimal,
    direction: str, existing_open_risk: Decimal, leverage: Decimal,
    remaining_margin: Decimal, qty_step: Decimal, min_qty: Decimal,
    min_notional: Decimal, fee_rate: Decimal, pressure_slippage: Decimal,
    single_risk_ratio: Decimal = Decimal("0.01"),
    portfolio_risk_ratio: Decimal = Decimal("0.03"),
    take_profit_ratios: Tuple[Decimal, Decimal] = (Decimal("0.30"), Decimal("0.40")),
    take_profit_prices: Optional[Tuple[Decimal, Decimal]] = None,
    budget_version: BudgetVersion = STRICT,
) -> RiskDecision:
    """Size against strict percentages or an explicit minimum executable floor.

    Floor mode can raise risk budgets to admit one executable lot; the actual
    remaining margin remains a physical ceiling. Stops and targets never move.
    """
    inputs = dict(allocation=allocation, entry_price=entry_price, stop_price=stop_price,
                  direction=direction, existing_open_risk=existing_open_risk,
                  leverage=leverage, remaining_margin=remaining_margin,
                  qty_step=qty_step, min_qty=min_qty, min_notional=min_notional,
                  fee_rate=fee_rate, pressure_slippage=pressure_slippage,
                  single_risk_ratio=single_risk_ratio, portfolio_risk_ratio=portfolio_risk_ratio,
                  take_profit_ratios=take_profit_ratios, take_profit_prices=take_profit_prices,
                  budget_version=budget_version)
    if not isinstance(budget_version, str) or budget_version not in (STRICT, FLOOR):
        return RiskDecision(False, "invalid_budget_version")
    arguments = {key: value for key, value in inputs.items() if key != "budget_version"}
    if budget_version == STRICT:
        # Keep the historical assessor's validation order and numerical behavior.
        decision = _assess_percent_entry(**arguments)
        if not _capturable(inputs):
            return decision
        return replace(decision, budget_inputs=inputs,
                       base_single_limit=allocation * single_risk_ratio,
                       base_portfolio_limit=allocation * portfolio_risk_ratio,
                       effective_single_limit=allocation * single_risk_ratio,
                       effective_portfolio_limit=allocation * portfolio_risk_ratio)
    invalid = RiskDecision(False, "invalid_inputs", budget_version=FLOOR)
    if not _capturable(inputs):
        return invalid
    positive_keys = set(_NUMERIC_INPUTS) - {"existing_open_risk", "fee_rate", "pressure_slippage"}
    if (any(inputs[key] <= 0 for key in positive_keys)
            or existing_open_risk < 0 or fee_rate < 0
            or not 0 <= pressure_slippage < 1
            or single_risk_ratio > Decimal(".01")
            or portfolio_risk_ratio > Decimal(".03")
            or direction not in ("short", "long")):
        return invalid
    ratios = tuple(take_profit_ratios)
    if not (ratios == (Decimal(1), Decimal(0)) or (
            all(0 < ratio < 1 for ratio in ratios) and sum(ratios) < 1)):
        return invalid
    if (direction == "short" and stop_price <= entry_price) or (
            direction == "long" and stop_price >= entry_price):
        return replace(invalid, reason="stop_not_protective", budget_inputs=inputs)
    if take_profit_prices is None:
        return invalid
    tp_prices = take_profit_prices
    if (any(price <= 0 for price in tp_prices)
            or (take_profit_prices is not None and any(
                price >= entry_price if direction == "short" else price <= entry_price
                for price in tp_prices))):
        return invalid
    try:
        with localcontext() as context:
            values = [inputs[key] for key in _NUMERIC_INPUTS] + list(ratios) + list(tp_prices)
            if any(value.adjusted() > context.Emax for value in values):
                return invalid
            # Preserve the supplied decimal scales across fee/stop/quantity
            # products and portfolio sums. Four input spans cover the product
            # depth; guard digits keep divisions away from lattice boundaries.
            span = max(value.adjusted() for value in values) - min(
                value.as_tuple().exponent for value in values) + 1
            context.prec = max(context.prec, 4 * span + 16)
            adverse_stop = stop_price * (Decimal(1) + pressure_slippage if direction == "short"
                                         else Decimal(1) - pressure_slippage)
            per_unit_loss = abs(adverse_stop - entry_price) + fee_rate * (entry_price + adverse_stop)
            required = max(min_qty, min_notional / entry_price, min_notional / tp_prices[0])
            minimum = min_qty + ((required - min_qty) / qty_step).to_integral_value(
                rounding=ROUND_CEILING) * qty_step
            # Verify products as well as quotients at Decimal precision boundaries.
            if minimum * min(entry_price, tp_prices[0]) < min_notional:
                minimum += qty_step
            minimum_risk = minimum * per_unit_loss
            base_single = allocation * single_risk_ratio
            base_portfolio = allocation * portfolio_risk_ratio
            single = max(base_single, minimum_risk)
            portfolio = max(base_portfolio, existing_open_risk + minimum_risk)
            remaining_risk = max(base_portfolio - existing_open_risk, minimum_risk)
            limit = min(single, remaining_risk)
            metadata = dict(budget_version=FLOOR, minimum_quantity=minimum,
                            minimum_risk=minimum_risk, base_single_limit=base_single,
                            base_portfolio_limit=base_portfolio, effective_single_limit=single,
                            effective_portfolio_limit=portfolio, risk_limit=limit,
                            budget_inputs=inputs, risk_allocation=allocation)
            if minimum * entry_price / leverage > remaining_margin:
                return RiskDecision(False, "minimum_order_exceeds_margin", **metadata)
            raw = min(limit / per_unit_loss, remaining_margin * leverage / entry_price)
            quantity = max(minimum, min_qty + ((raw - min_qty) / qty_step).to_integral_value(
                rounding=ROUND_FLOOR) * qty_step)
            if quantity * entry_price / leverage > remaining_margin or quantity * per_unit_loss > limit:
                quantity -= qty_step
            if quantity < minimum:
                return RiskDecision(False, "minimum_order_exceeds_margin", **metadata)
            from shared.entry_risk_policy import EntryDenied, executable_exit_ratios
            try:
                exits = ratios if ratios == (Decimal(1), Decimal(0)) else executable_exit_ratios(
                    quantity, qty_step, min_qty, min_notional, [*tp_prices, adverse_stop], ratios)
                if exits != ratios:
                    exits = (Decimal(1), Decimal(0))
            except EntryDenied:
                return RiskDecision(False, "take_profit_quantity_unavailable", **metadata)
            return RiskDecision(True, "allowed", quantity=quantity,
                                worst_loss=quantity * per_unit_loss, exit_ratios=exits, **metadata)
    except ArithmeticError:
        return invalid
