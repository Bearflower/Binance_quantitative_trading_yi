"""Reconstruct reviewed HRS checkpoint gaps from actual closed Binance marks."""
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from .equity_evidence import EvidenceBlocked, number, order_owners, owned_fill_positions
from .loss_gate import DAILY_LIMIT, ROLLING_LIMIT, SHANGHAI
from .risk_ledger import EquityCheckpoint

MINUTE = timedelta(minutes=1)
BAR_SOURCE = '/fapi/v1/markPriceKlines'


def _require(condition, reason):
    if not condition:
        raise EvidenceBlocked('history_gap_' + reason)


def _bars_by_close(bars, left, right):
    start = left.replace(second=0, microsecond=0)
    end = right.replace(second=0, microsecond=0)
    expected_count = (end - start) // MINUTE
    _require(0 < expected_count <= 1000 and len(bars) == expected_count, 'bar_coverage')
    result = {}
    for index, bar in enumerate(bars):
        opened = start + index * MINUTE
        open_ms = int(opened.timestamp()) * 1000
        _require(len(bar) >= 7 and bar[0] == open_ms and bar[6] == open_ms + 59999, 'bar_identity')
        opened_price, high, low, close = (number(value, 'history_gap_bar_price', positive=True) for value in bar[1:5])
        _require(low <= min(opened_price, close) <= max(opened_price, close) <= high, 'bar_price_range')
        result[opened + MINUTE] = (bar, close)
    return result


def reconstruct_gaps(manifest, account, ledger, checkpoints, fills, orders, opening, available_at):
    """Keep live points intact and reconstruct only constant-unit, proved paths.

    Reviewed occupied gaps may contain unchanged inventory or a single exit to
    flat. The existing economic ledger supplies exact income and capital events.
    The historical API supplies marks only; it has no authority over balances.
    """
    _require(manifest['account'] == account and manifest['segment'] == ledger.segment_id, 'segment_identity')
    collected = datetime.fromisoformat(manifest['available_at'])
    _require(collected.utcoffset() is not None and collected <= available_at, 'source_availability')
    _require(ledger.as_of - ledger.inception < timedelta(days=30), 'inception_only')
    endpoints = {left.as_of: right.as_of for left, right in zip(checkpoints, checkpoints[1:])}
    by_time = {point.as_of: point for point in checkpoints}
    owner_map = order_owners(account, orders)
    restored, retained = [], {}
    for item in manifest['gaps']:
        left, right = (datetime.fromisoformat(item[key]) for key in ('left', 'right'))
        _require(endpoints.get(left) == right and ledger.inception <= left < right <= min(ledger.as_of,collected)
                 and right - left > 2 * MINUTE, 'checkpoint_identity')
        units = by_time[left].units
        _require(by_time[right].units == units and not any(left < row.occurred_at <= right for row in ledger.cash_flows),
                 'unproven_cash_flow')
        inventory = owned_fill_positions(account, fills, orders, through=left, after=ledger.inception,
                                         opening_inventory=opening)
        held = {symbol: (qty, basis) for (strategy, symbol), (qty, basis) in inventory.items() if strategy == 'hrs' and qty}
        _require(held and set(held) == set(item['bars']), 'covered_symbols')
        owned_fill_positions(account, fills, orders, through=right, after=ledger.inception,
                             opening_inventory=opening)
        changes = [row for row in fills if left < row['trade_time_utc'] <= right
                   and owner_map.get((row['symbol'], str(row['order_id']))) == {'hrs'}]
        if changes:
            _require(len(changes) == 1, 'position_path')
            exit_fill = changes[0]
            qty, _ = held.get(exit_fill['symbol'], (Decimal(0), None))
            _require(qty != 0 and exit_fill['side'] == ('SELL' if qty > 0 else 'BUY')
                     and number(exit_fill['qty'], 'history_gap_exit_quantity', positive=True) == abs(qty), 'position_path')
        bars = {symbol: _bars_by_close(value, left, right) for symbol, value in item['bars'].items()}
        cursor = left.replace(second=0, microsecond=0) + MINUTE
        while cursor < right:
            post_exit = changes and changes[0]['trade_time_utc'] <= cursor
            cash = ledger.cash_allocation - sum(row.amount for row in ledger.cash_flows if row.occurred_at > cursor)
            income = sum(row.amount for row in ledger.incomes if row.occurred_at <= cursor)
            unrealized = Decimal(0)
            for symbol, (qty, basis) in held.items():
                if post_exit and symbol == changes[0]['symbol']:
                    continue
                bar, mark = bars[symbol][cursor]
                retained[f'{symbol}:{int(cursor.timestamp()) * 1000}'] = dict(
                    bar=bar, available_at=collected, source=BAR_SOURCE)
                unrealized += qty * (mark - basis)
            equity = number(cash + income + unrealized, 'history_gap_equity', positive=True)
            restored.append(EquityCheckpoint(ledger.segment_id, cursor, equity, units, equity / units))
            cursor += MINUTE
    _require(restored and len({point.as_of for point in restored}) == len(restored), 'duplicate_or_empty')
    return tuple(restored), retained


def replay_inception_latches(gate, samples):
    """Preserve latches and detect a sampled breach during the recovered history."""
    peak = samples[0].nav
    rolling, scope = gate.rolling_latched, gate.rolling_latch_scope
    today = samples[-1].as_of.astimezone(SHANGHAI).date()
    daily = gate.daily_latched and gate.day == today
    day_origin = None
    for sample in samples:
        peak = max(peak, sample.nav)
        if not rolling and Decimal(1) - sample.nav / peak >= ROLLING_LIMIT:
            rolling, scope = True, 'inception_segment'
        local = sample.as_of.astimezone(SHANGHAI)
        if local.date() != today:
            continue
        if local.time() == time.min:
            day_origin = sample.nav
        if day_origin is not None and sample.nav / day_origin - Decimal(1) <= -DAILY_LIMIT:
            daily = True
    if day_origin is not None and daily:
        return replace(gate, rolling_latched=rolling, rolling_latch_scope=scope,
                       day=today, day_origin_nav=day_origin, daily_latched=True)
    return replace(gate, rolling_latched=rolling, rolling_latch_scope=scope)
