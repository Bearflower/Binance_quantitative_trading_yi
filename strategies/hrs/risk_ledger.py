"""Pure HRS equity ledger; no exchange calls or implicit zero-valued evidence.

Integration contract: initialize only from a reviewed opening cash allocation and a
complete opening position inventory. Submit *signed* attributed income amounts
(fees negative), and verified signed futures quantities with entry/mark prices.
The income watermark must certify all income through the observation, including
fees and funding, not merely the latest returned event. Empty collections require
explicit completeness proofs. Late new facts invalidate the segment until its
historical checkpoints are reconciled; do not silently put them into today's PnL.

Flows at the observation time are unitized at the contemporaneous pre-flow NAV.
Earlier flows need their own verified NAV checkpoint reference. Persist state
and checkpoint atomically with LossGateRepository; preserve event IDs on restart.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Tuple

ZERO = Decimal("0")
ONE = Decimal("1")


class LedgerNotReady(ValueError):
    """Evidence is inadequate to authorize new risk; exits remain permitted."""


def decimal_value(value, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise LedgerNotReady(f"invalid_{name}")
    if positive and value <= ZERO:
        raise LedgerNotReady(f"nonpositive_{name}")
    return value


def aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LedgerNotReady(f"invalid_{name}_timestamp")
    return value


@dataclass(frozen=True)
class IncomeEvent:
    event_id: str
    kind: str
    amount: Decimal
    occurred_at: datetime
    segment_id: str
    verified: bool


@dataclass(frozen=True)
class CashFlow:
    event_id: str
    amount: Decimal
    occurred_at: datetime
    segment_id: str
    nav_before: Decimal | None = None
    valuation_evidence: str | None = None


@dataclass(frozen=True)
class PositionMark:
    position_id: str
    segment_id: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    marked_at: datetime
    verified: bool


@dataclass(frozen=True)
class LedgerObservation:
    as_of: datetime
    segment_id: str
    incomes: Tuple[IncomeEvent, ...]
    cash_flows: Tuple[CashFlow, ...]
    positions: Tuple[PositionMark, ...]
    income_complete_through: datetime
    inventory_verified_at: datetime
    income_complete: bool
    positions_complete: bool


@dataclass(frozen=True)
class EquityCheckpoint:
    segment_id: str
    as_of: datetime
    equity: Decimal
    units: Decimal
    nav: Decimal


@dataclass(frozen=True)
class LedgerState:
    segment_id: str
    inception: datetime
    as_of: datetime
    cash_allocation: Decimal
    income_total: Decimal
    units: Decimal
    equity: Decimal
    nav: Decimal
    incomes: Tuple[IncomeEvent, ...] = ()
    cash_flows: Tuple[CashFlow, ...] = ()

    @property
    def checkpoint(self) -> EquityCheckpoint:
        return EquityCheckpoint(self.segment_id, self.as_of, self.equity, self.units, self.nav)


def _validate_observation(obs: LedgerObservation, segment: str, max_age: timedelta) -> Decimal:
    aware(obs.as_of, "observation")
    if not segment or obs.segment_id != segment:
        raise LedgerNotReady("segment_mismatch")
    if max_age <= timedelta(0):
        raise LedgerNotReady("invalid_max_age")
    if obs.income_complete is not True:
        raise LedgerNotReady("income_incomplete")
    if obs.positions_complete is not True:
        raise LedgerNotReady("position_inventory_incomplete")
    if aware(obs.income_complete_through, "income_watermark") < obs.as_of:
        raise LedgerNotReady("income_watermark_incomplete")
    inventory_age = obs.as_of - aware(obs.inventory_verified_at, "inventory")
    if not timedelta(0) <= inventory_age <= max_age:
        raise LedgerNotReady("stale_position_inventory")
    unrealized = ZERO
    ids = set()
    if obs.positions is None or obs.incomes is None or obs.cash_flows is None:
        raise LedgerNotReady("missing_event_inventory")
    for position in obs.positions:
        if position.verified is not True:
            raise LedgerNotReady("unverified_position")
        if position.segment_id != segment or not position.position_id:
            raise LedgerNotReady("position_attribution")
        if position.position_id in ids:
            raise LedgerNotReady("duplicate_position")
        ids.add(position.position_id)
        qty = decimal_value(position.quantity, "position_quantity")
        if qty == ZERO:
            raise LedgerNotReady("zero_position_quantity")
        entry = decimal_value(position.entry_price, "entry_price", positive=True)
        mark = decimal_value(position.mark_price, "mark_price", positive=True)
        age = obs.as_of - aware(position.marked_at, "mark")
        if not timedelta(0) <= age <= max_age:
            raise LedgerNotReady("stale_position_mark")
        unrealized += qty * (mark - entry)
    return unrealized


def initialize_ledger(segment_id: str, opening_cash: Decimal, observation: LedgerObservation,
                      *, max_age: timedelta = timedelta(seconds=120)) -> LedgerState:
    """Start a reviewed capital segment at NAV 1; this does not invent day/30d history."""
    cash = decimal_value(opening_cash, "opening_cash", positive=True)
    unrealized = _validate_observation(observation, segment_id, max_age)
    if observation.incomes or observation.cash_flows:
        raise LedgerNotReady("opening_events_require_reconciliation")
    equity = decimal_value(cash + unrealized, "opening_equity", positive=True)
    return LedgerState(segment_id, observation.as_of, observation.as_of,
                       cash, ZERO, equity, equity, ONE)


def _new_events(events, previous, state: LedgerState, obs: LedgerObservation, kind: str):
    seen = {item.event_id: item for item in previous}
    fresh = []
    for event in events:
        if not event.event_id:
            raise LedgerNotReady(f"missing_{kind}_id")
        decimal_value(event.amount, f"{kind}_amount")
        aware(event.occurred_at, kind)
        if event.segment_id != state.segment_id:
            raise LedgerNotReady(f"{kind}_attribution")
        if kind == "income":
            if event.verified is not True or event.kind not in {"realized_pnl", "fee", "funding"}:
                raise LedgerNotReady("income_attribution_unverified")
            if event.kind == "fee" and event.amount > ZERO:
                raise LedgerNotReady("positive_fee_requires_reconciliation")
        if event.event_id in seen:
            if event != seen[event.event_id]:
                raise LedgerNotReady(f"conflicting_{kind}")
            continue
        if event.occurred_at <= state.as_of:
            raise LedgerNotReady(f"late_{kind}_requires_reconciliation")
        if event.occurred_at > obs.as_of:
            raise LedgerNotReady(f"future_{kind}")
        seen[event.event_id] = event
        fresh.append(event)
    return tuple(fresh)


def reconcile_ledger(state: LedgerState, observation: LedgerObservation, *,
                     max_age: timedelta = timedelta(seconds=120)) -> LedgerState:
    unrealized = _validate_observation(observation, state.segment_id, max_age)
    if observation.as_of < state.as_of:
        raise LedgerNotReady("out_of_order_observation")
    for name in ("units", "equity", "nav"):
        decimal_value(getattr(state, name), name, positive=True)
    decimal_value(state.cash_allocation, "cash_allocation")
    decimal_value(state.income_total, "income_total")
    incomes = _new_events(observation.incomes, state.incomes, state, observation, "income")
    flows = _new_events(observation.cash_flows, state.cash_flows, state, observation, "cashflow")
    cash = state.cash_allocation + sum((flow.amount for flow in flows), ZERO)
    income = state.income_total + sum((event.amount for event in incomes), ZERO)
    equity = decimal_value(cash + income + unrealized, "equity", positive=True)
    units = state.units
    # Earlier flows are already embedded in the current equity; contemporaneous
    # flows are valued only after those certified earlier flows have issued units.
    for flow in sorted(flows, key=lambda item: item.occurred_at):
        if flow.occurred_at != observation.as_of:
            if not flow.valuation_evidence or flow.nav_before is None:
                raise LedgerNotReady("cashflow_valuation_missing")
            nav = decimal_value(flow.nav_before, "cashflow_nav", positive=True)
            units = decimal_value(units + flow.amount / nav, "units", positive=True)
    current_flows = tuple(flow for flow in flows if flow.occurred_at == observation.as_of)
    flow_amount = sum((flow.amount for flow in current_flows), ZERO)
    if current_flows:
        before = decimal_value(equity - flow_amount, "preflow_equity", positive=True)
        nav = before / units
        for flow in current_flows:
            if flow.nav_before is not None and decimal_value(flow.nav_before, "cashflow_nav", positive=True) != nav:
                raise LedgerNotReady("conflicting_cashflow_valuation")
        units = decimal_value(units + flow_amount / nav, "units", positive=True)
    return replace(state, as_of=observation.as_of, cash_allocation=cash,
                   income_total=income, units=units, equity=equity, nav=equity / units,
                   incomes=state.incomes + incomes, cash_flows=state.cash_flows + flows)
