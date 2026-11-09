"""Fail-closed replay of a single account/symbol net position from a flat base."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class OwnerInterval:
    start: datetime
    end: datetime
    strategy: str
    side: str


@dataclass(frozen=True)
class ReplayResult:
    verified: bool
    reason: str | None
    intervals: tuple[OwnerInterval, ...]
    fill_times: frozenset[datetime]
    fill_ids: tuple[str, ...]
    fill_trace: tuple[tuple[str, datetime], ...]
    flat_at: datetime
    coverage_end: datetime
    final_qty: Decimal


def _decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite quantity")
    return result


def _owner(fill, evidence, account_id, symbol):
    order_id = fill.get("order_id")
    row = evidence.get(str(order_id)) if order_id is not None else None
    if not row or (str(row.get("account_id")) != account_id
                   or str(row.get("symbol")) != symbol
                   or str(row.get("order_id")) != str(order_id)):
        return None
    return str(row["strategy"]) if row.get("strategy") else None


def replay_position(baseline, fills, order_evidence, coverage_end, exchange_qty,
                    *, coverage_complete=True):
    """Return owner intervals only when replay and exchange quantity reconcile."""
    account_id, symbol = str(baseline["account_id"]), str(baseline["symbol"])
    flat_at = baseline["flat_at"]
    qty = Decimal(0)
    intervals = []
    fill_times = set()
    fill_ids = []
    fill_trace = []

    def result(reason=None):
        return ReplayResult(reason is None, reason,
                            tuple(intervals) if reason is None else (),
                            frozenset(fill_times), tuple(fill_ids), tuple(fill_trace),
                            flat_at, coverage_end, qty)

    if _decimal(baseline["exchange_qty"]) != 0:
        return result("baseline_not_flat")
    if flat_at >= coverage_end:
        return result("invalid_replay_window")
    if not coverage_complete:
        return result("fill_coverage_incomplete")

    ordered = sorted(fills, key=lambda row: (row["trade_time_utc"], int(row["trade_id"])))
    seen_ids = set()
    active_index = None
    for fill in ordered:
        if str(fill.get("account_id")) != account_id or str(fill.get("symbol")) != symbol:
            return result("fill_scope_mismatch")
        stamp = fill["trade_time_utc"]
        if not (flat_at < stamp < coverage_end):
            return result("fill_outside_verified_window")
        trade_id = str(fill["trade_id"])
        if trade_id in seen_ids:
            return result("duplicate_trade_id")
        seen_ids.add(trade_id)
        fill_ids.append(trade_id)
        fill_trace.append((trade_id, stamp))
        fill_times.add(stamp)
        side = str(fill.get("side") or "").upper()
        amount = _decimal(fill.get("qty"))
        if side not in {"BUY", "SELL"} or amount <= 0:
            return result("invalid_fill_quantity_or_side")
        delta = amount if side == "BUY" else -amount
        owner = _owner(fill, order_evidence, account_id, symbol)
        if qty == 0:
            if not owner:
                return result("opening_order_owner_missing")
            intervals.append(OwnerInterval(stamp, coverage_end, owner, side))
            active_index = len(intervals) - 1
            qty = delta
            continue
        if (qty > 0) == (delta > 0):
            if not owner:
                return result("opening_order_owner_missing")
            if owner != intervals[active_index].strategy:
                return result("multiple_position_owners")
            qty += delta
            continue
        if abs(delta) <= abs(qty):
            if owner and owner != intervals[active_index].strategy:
                return result("closing_order_owner_conflict")
            qty += delta
            if qty == 0:
                intervals[active_index] = replace(intervals[active_index], end=stamp)
                active_index = None
            continue
        # A single fill closed the old position and opened the other direction.
        intervals[active_index] = replace(intervals[active_index], end=stamp)
        if not owner:
            return result("opening_order_owner_missing")
        qty += delta
        intervals.append(OwnerInterval(stamp, coverage_end, owner, side))
        active_index = len(intervals) - 1

    if qty != _decimal(exchange_qty):
        return result("exchange_quantity_mismatch")
    return result()


def owner_at(replay, funding_time):
    """Return a unique strategy only inside a verified, non-boundary interval."""
    if not replay.verified:
        return None, replay.reason
    if not (replay.flat_at < funding_time < replay.coverage_end):
        return None, "funding_outside_verified_window"
    if funding_time in replay.fill_times:
        return None, "funding_fill_time_collision"
    owners = {row.strategy for row in replay.intervals
              if row.start < funding_time < row.end}
    if not owners:
        return None, "no_open_position"
    if len(owners) != 1:
        return None, "multiple_position_owners"
    return next(iter(owners)), None
