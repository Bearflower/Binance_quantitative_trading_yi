"""Prove a post-acceptance HRS sampling gap from signed fills and closed marks.

This path never changes the accepted PM prefix. It values every closed minute
with the inventory at that minute and retains conservative OHLC bounds for all
intervening entry/exit and income orderings. A partial current minute remains
covered by the ordinary 120-second live sampling allowance.
"""
from datetime import timedelta
from decimal import Decimal

from .equity_evidence import EvidenceBlocked, number, order_key, order_owners, owned_fill_positions
from .equity_history_gap import MINUTE, BOUND_QUANTUM, _bars_by_close, _require, mark_scope
from .risk_ledger import EquityCheckpoint


KIND = 'accepted_live_mark_gap_v1'
MAX_BARS = 1000


def _held(account, ledger, at, fills, orders, opening):
    inventory = owned_fill_positions(account, fills, orders, through=at,
                                      after=ledger.inception, opening_inventory=opening)
    return {symbol: (qty, basis) for (owner, symbol), (qty, basis) in inventory.items()
            if owner == 'hrs' and qty}


def plan_live_gap(account, ledger, left, right, fills, orders, opening):
    """Identify every possibly occupied symbol and reject unproved economics."""
    _require(left.segment_id == right.segment_id == ledger.segment_id
             and ledger.inception <= left.as_of < right.as_of <= ledger.as_of
             and right.as_of-left.as_of > 2*MINUTE, 'live_checkpoint_identity')
    _require(left.units == right.units and not any(
        left.as_of < flow.occurred_at <= right.as_of for flow in ledger.cash_flows),
        'live_cash_flow')
    for event in ledger.incomes:
        _require(event.verified is True and event.segment_id == ledger.segment_id
                 and event.kind in {'fee', 'funding', 'realized_pnl'}
                 and event.amount.is_finite() and (event.kind != 'fee' or event.amount <= 0),
                 'live_income_unproven')
    owners = order_owners(account, orders)
    changes = [row for row in fills if left.as_of < row['trade_time_utc'] <= right.as_of]
    symbols = set(_held(account, ledger, left.as_of, fills, orders, opening))
    seen = {}
    for row in changes:
        key = order_key(account, row)
        owner = owners.get(key)
        if owner == {'hrs'}:
            identity = (row['symbol'], str(row['trade_id']))
            _require(identity not in seen or seen[identity] == row,
                     'live_fill_identity')
            seen[identity] = row
            symbols.add(row['symbol'])
        elif row['symbol'] in symbols or owner and 'hrs' in owner:
            raise EvidenceBlocked('history_gap_live_fill_owner')
    symbols.update(_held(account, ledger, right.as_of, fills, orders, opening))
    _require(bool(symbols), 'live_empty_inventory')
    # Another strategy's fill in a covered symbol can invalidate the one-way
    # position path even if the HRS positions at both endpoints happen to agree.
    _require(all(owners.get(order_key(account, row)) == {'hrs'}
                 for row in changes if row['symbol'] in symbols), 'live_fill_owner')
    first, end = mark_scope(left.as_of, right.as_of)
    count = (end-first)//MINUTE * len(symbols)
    _require(0 < count <= MAX_BARS, 'live_source_budget')
    return dict(left=left.as_of.isoformat(), right=right.as_of.isoformat(),
                symbols=sorted(symbols))


def _checked_bars(item):
    from datetime import datetime
    left, right = (datetime.fromisoformat(item[key]) for key in ('left', 'right'))
    _require(set(item['bars']) == set(item['symbols']), 'live_bar_symbols')
    return left, right, {symbol: _bars_by_close(rows, left, right)
                         for symbol, rows in item['bars'].items()}


def reconstruct_live_gap(account, ledger, item, fills, orders, opening, units,
                         available_at):
    left, right, bars = _checked_bars(item)
    first, end = mark_scope(left, right)
    restored = []
    retained = {}
    cursor = first + MINUTE
    while cursor < right:
        held = _held(account, ledger, cursor, fills, orders, opening)
        _require(set(held) <= set(bars), 'live_inventory_symbols')
        cash = ledger.cash_allocation-sum(
            flow.amount for flow in ledger.cash_flows if flow.occurred_at > cursor)
        income = sum((event.amount for event in ledger.incomes if event.occurred_at <= cursor), Decimal(0))
        unrealized = Decimal(0)
        for symbol, (qty, basis) in held.items():
            bar, mark = bars[symbol][cursor]
            retained[f'{symbol}:{int(cursor.timestamp()*1000)}'] = {
                'bar': bar, 'source': '/fapi/v1/markPriceKlines',
                'available_at': available_at}
            unrealized += qty*(mark-basis)
        equity = number(cash+income+unrealized, 'history_gap_live_equity', positive=True)
        restored.append(EquityCheckpoint(ledger.segment_id, cursor, equity, units, equity/units))
        cursor += MINUTE
    _require(bool(restored) and restored[-1].as_of >= right-MINUTE,
             'live_tail_uncovered')
    return tuple(restored), retained


def live_gap_envelopes(account, ledger, item, fills, orders, opening, units):
    """Overapproximate every intraminute portfolio state, including entries."""
    left, right, bars = _checked_bars(item)
    owners = order_owners(account, orders)
    first, end = mark_scope(left, right)
    bounds = []
    close = first + MINUTE
    while close <= end:
        lo, hi = max(left, close-MINUTE), min(right, close)
        if lo >= hi:
            close += MINUTE
            continue
        changes = sorted({row['trade_time_utc'] for row in fills
                          if lo < row['trade_time_utc'] <= hi
                          and owners.get(order_key(account, row)) == {'hrs'}})
        states = [_held(account, ledger, lo, fills, orders, opening)]
        for at in changes:
            states.append(_held(account, ledger, at-BOUND_QUANTUM, fills, orders, opening))
            states.append(_held(account, ledger, at, fills, orders, opening))
        states.append(_held(account, ledger, hi, fills, orders, opening))
        _require(all(set(state) <= set(bars) for state in states),
                 'live_envelope_inventory')
        cash = ledger.cash_allocation-sum(
            flow.amount for flow in ledger.cash_flows if flow.occurred_at > lo)
        prior = sum((event.amount for event in ledger.incomes if event.occurred_at <= lo), Decimal(0))
        during = [event.amount for event in ledger.incomes if lo < event.occurred_at <= hi]
        income_low = prior+sum((value for value in during if value < 0), Decimal(0))
        income_high = prior+sum((value for value in during if value > 0), Decimal(0))
        lows, highs = [], []
        for state in states:
            low, high = cash+income_low, cash+income_high
            for symbol, (qty, basis) in state.items():
                bar, _ = bars[symbol][close]
                upper = number(bar[2], 'live_bar_high', positive=True)
                lower = number(bar[3], 'live_bar_low', positive=True)
                low += qty*((lower if qty > 0 else upper)-basis)
                high += qty*((upper if qty > 0 else lower)-basis)
            lows.append(low)
            highs.append(high)
        bounds.append(dict(as_of=(hi-BOUND_QUANTUM).isoformat(),
                           source_units=str(units),
                           low_nav=str(number(min(lows), 'live_bound_low', positive=True)/units),
                           high_nav=str(number(max(highs), 'live_bound_high', positive=True)/units)))
        close += MINUTE
    return bounds
