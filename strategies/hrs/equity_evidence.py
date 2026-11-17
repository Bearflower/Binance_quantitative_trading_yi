"""Conservative evidence checks for prospective HRS equity.

No row is human-reviewed merely because it came from a derived attribution table.
The existing report reader owns income identity/amount reconciliation; this module
accepts only fully attributed reports and proves positions independently from
signed exchange fills and durable order owners. Unsupported evidence stays blocked.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from .loss_gate import SHANGHAI
from .risk_ledger import IncomeEvent, LedgerNotReady, LedgerObservation, PositionMark, aware


class EvidenceBlocked(LedgerNotReady):
    pass


def number(value, name, *, positive=False):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or (positive and result <= 0):
            raise ValueError()
        return result
    except Exception:
        raise EvidenceBlocked(name) from None


def canonical(value):
    def encode(item):
        if isinstance(item, datetime):
            return aware(item, 'evidence').isoformat()
        if isinstance(item, Decimal):
            return str(number(item, 'nonfinite_evidence'))
        raise TypeError(type(item).__name__)
    payload = json.dumps(value, default=encode, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return payload, hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class AllocationEvidence:
    amount: Decimal
    identity: str
    effective_at: datetime
    source: dict


def allocation_evidence(row, as_of):
    if not row or str(row.get('month', ''))[:7] != as_of.astimezone(SHANGHAI).strftime('%Y-%m'):
        raise EvidenceBlocked('allocation_month_missing')
    entries = json.loads(row['entries'], parse_float=Decimal) if isinstance(row.get('entries'), str) else row.get('entries')
    if not isinstance(entries, list):
        raise EvidenceBlocked('allocation_identity_missing')
    matches = [item for item in entries if isinstance(item, dict) and item.get('strategy_id') == 'hrs']
    if len(matches) != 1 or row.get('id') is None:
        raise EvidenceBlocked('allocation_identity_ambiguous')
    amount = number(matches[0].get('allocated_amount'), 'allocation_amount_invalid', positive=True)
    effective = row.get('created_at')
    if not isinstance(effective, datetime):
        raise EvidenceBlocked('allocation_time_missing')
    # Production capital_allocation.created_at is explicitly Beijing naive.
    effective = effective.replace(tzinfo=SHANGHAI) if effective.tzinfo is None else effective
    if effective > as_of:
        raise EvidenceBlocked('allocation_time_future')
    source = dict(id=row['id'], month=str(row['month']), allocated_amount=str(amount), effective_at=effective)
    return AllocationEvidence(amount, canonical(source)[1], effective, source)


def attributed_incomes(report, account, segment):
    if (report.get('id_match_and_coverage_complete') is not True or report.get('coverage_gaps')
            or report.get('missing_income_fills') or report.get('unattributed')):
        raise EvidenceBlocked('income_or_fill_coverage_incomplete')
    events = []
    seen = {}
    kinds = {'REALIZED_PNL': 'realized_pnl', 'COMMISSION': 'fee', 'FUNDING_FEE': 'funding'}
    for row in report.get('events', ()):
        if row.get('account_id') != account or row.get('asset') != 'USDT' or row.get('reason') or not row.get('strategy'):
            raise EvidenceBlocked('income_attribution_unproven')
        if row.get('income_type') not in kinds or not row.get('tran_id') or not row.get('event_time_utc'):
            raise EvidenceBlocked('income_identity_missing')
        if row['strategy'] != 'hrs':
            continue
        event = IncomeEvent(f"{row['income_type']}:{row['tran_id']}", kinds[row['income_type']],
                            number(row.get('amount'), 'income_amount_invalid'),
                            datetime.fromisoformat(row['event_time_utc']), segment, True)
        aware(event.occurred_at, 'income')
        old = seen.setdefault(event.event_id, event)
        if old != event:
            raise EvidenceBlocked('conflicting_income_identity')
        if old is event:
            events.append(event)
    return tuple(events)


def order_owners(account, rows):
    result = {}
    for row in rows:
        if row.get('account_id') != account or row.get('order_id') is None or not row.get('symbol'):
            continue
        key = (row['symbol'], str(row['order_id']))
        result.setdefault(key, set()).add(row.get('strategy'))
    return result


def owned_fill_positions(account, fills, orders, *, through=None, after=None):
    owners = order_owners(account, orders)
    result = {}
    seen = {}
    for row in sorted(fills, key=lambda item: (item.get('trade_time_utc'), str(item.get('trade_id')))):
        if row.get('account_id') != account:
            raise EvidenceBlocked('fill_account_mismatch')
        at = aware(row.get('trade_time_utc'), 'fill')
        if (through is not None and at > through) or (after is not None and at <= after):
            continue
        key = (row.get('symbol'), str(row.get('trade_id')))
        if key in seen:
            if seen[key] != row:
                raise EvidenceBlocked('fill_identity_conflict')
            continue
        seen[key] = row
        choices = owners.get((row.get('symbol'), str(row.get('order_id'))), set())
        if len(choices) != 1 or None in choices:
            raise EvidenceBlocked('fill_order_owner_unproven')
        strategy = next(iter(choices))
        side = row.get('side')
        if side not in {'BUY', 'SELL'}:
            raise EvidenceBlocked('fill_side_missing')
        qty = number(row.get('qty'), 'fill_quantity_invalid', positive=True) * (1 if side == 'BUY' else -1)
        price = number(row.get('price'), 'fill_price_invalid', positive=True)
        position_key = (strategy, row['symbol'])
        old_qty, old_basis = result.get(position_key, (Decimal(0), Decimal(0)))
        new_qty = old_qty + qty
        if old_qty == 0 or old_qty * qty > 0:
            basis = (abs(old_qty) * old_basis + abs(qty) * price) / abs(new_qty)
        elif new_qty == 0:
            basis = Decimal(0)
        elif old_qty * new_qty > 0:
            basis = old_basis
        else:
            basis = price
        result[position_key] = (new_qty, basis)
    return result


def _inventory(rows):
    if not isinstance(rows, list):
        raise EvidenceBlocked('position_inventory_missing')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get('symbol'):
            raise EvidenceBlocked('position_inventory_invalid')
        qty = number(row.get('positionAmt'), 'position_quantity_missing')
        if qty == 0:
            continue
        if row.get('positionSide', 'BOTH') != 'BOTH' or row['symbol'] in result:
            raise EvidenceBlocked('hedged_or_duplicate_position')
        result[row['symbol']] = (qty, number(row.get('entryPrice'), 'position_entry_missing', positive=True))
    return result


def prove_positions(account, segment, as_of, inventory, account_inventory, local, fills, orders,
                    marks, *, bootstrap, inception=None):
    actual = _inventory(inventory)
    independent = _inventory(account_inventory)
    if actual != independent:
        raise EvidenceBlocked('position_quantity_inventory_conflict')
    # All fills supplied here must have complete collection proof. For an HRS
    # segment, its initial position is zero; other strategies may predate it.
    positions = owned_fill_positions(account, fills, orders, through=as_of)
    hrs_positions = owned_fill_positions(account, fills, orders, through=as_of, after=inception) if inception else positions
    if bootstrap and local:
        raise EvidenceBlocked('bootstrap_hrs_not_flat')
    output = []
    for symbol, (qty, entry) in actual.items():
        matches = [(strategy, held, basis) for (strategy, sym), (held, basis) in positions.items()
                   if sym == symbol and held != 0]
        if len(matches) != 1 or matches[0][1:] != (qty, entry):
            raise EvidenceBlocked('position_order_owner_unproven')
        owner = matches[0][0]
        if owner != 'hrs':
            if symbol in local or hrs_positions.get(('hrs', symbol), (Decimal(0),))[0] != 0:
                raise EvidenceBlocked('position_owner_conflict')
            continue
        if bootstrap:
            raise EvidenceBlocked('bootstrap_hrs_not_flat')
        expected, basis = hrs_positions.get(('hrs', symbol), (Decimal(0), Decimal(0)))
        tracked = local.get(symbol)
        if not tracked or expected != qty or basis != entry:
            raise EvidenceBlocked('position_quantity_or_basis_unproven')
        tracked_qty = number(tracked.get('remaining_quantity'), 'local_position_quantity_missing')
        sign = 1 if tracked.get('direction') == 'long' else -1 if tracked.get('direction') == 'short' else 0
        if sign == 0 or tracked_qty * sign != qty or not tracked.get('position_id'):
            raise EvidenceBlocked('position_quantity_local_conflict')
        mark = marks.get(symbol)
        if not isinstance(mark, dict) or mark.get('symbol') != symbol or mark.get('time') is None:
            raise EvidenceBlocked('mark_identity_missing')
        marked_at = datetime.fromtimestamp(int(mark['time']) / 1000, timezone.utc)
        if not timedelta(0) <= as_of - marked_at <= timedelta(seconds=120):
            raise EvidenceBlocked('mark_stale_or_future')
        output.append(PositionMark(tracked['position_id'], segment, qty, entry,
                                   number(mark.get('markPrice'), 'mark_price_invalid', positive=True), marked_at, True))
    for (strategy, symbol), (qty, _) in hrs_positions.items():
        if strategy == 'hrs' and qty != 0 and symbol not in actual:
            raise EvidenceBlocked('position_quantity_missing_on_exchange')
    if set(local) != {symbol for (strategy, symbol), (qty, _) in hrs_positions.items() if strategy == 'hrs' and qty != 0}:
        raise EvidenceBlocked('local_position_inventory_conflict')
    return tuple(output)


def flat_boundary_observation(state, boundary, available_at, fills, orders, incomes, *, coverage_complete):
    """Value a historically flat boundary from a genuine segment, not today's state."""
    if (coverage_complete is not True or boundary < state.inception or boundary > available_at
            or boundary < state.as_of):
        raise EvidenceBlocked('boundary_coverage_missing')
    positions = owned_fill_positions(next(iter({row['account_id'] for row in fills}), ''), fills, orders,
                                    through=boundary, after=state.inception)
    if any(owner == 'hrs' and qty != 0 for (owner, _), (qty, _) in positions.items()):
        raise EvidenceBlocked('boundary_not_flat')
    events = tuple(event for event in incomes if event.occurred_at <= boundary)
    # Effective inventory is reconstructed at boundary; acquisition time is kept
    # separately in the immutable service evidence, never claimed as live capture.
    return LedgerObservation(boundary, state.segment_id, events, (), (), boundary, boundary, True, True)
