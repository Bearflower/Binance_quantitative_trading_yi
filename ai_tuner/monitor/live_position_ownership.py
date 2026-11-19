"""Prove an unclaimed one-way PM lot from its exact recent signed fill chain."""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

from shared.entry_fill_evidence import verified_fill


_CANDIDATES_SQL = """
SELECT o.symbol, o.order_id::text AS order_id, o.strategy, t.side,
       (SELECT jsonb_agg(jsonb_build_array(f.trade_id::text, f.qty::text,
                  f.price::text, f.side,
                  (EXTRACT(EPOCH FROM f.trade_time_utc) * 1000)::bigint))
        FROM trading.execution_fact_fill f
        WHERE f.account_id=o.account_id AND f.symbol=o.symbol AND f.order_id=o.order_id) AS ledger_trades
FROM trading.order_strategy_evidence o
JOIN trading.trade_records t ON t.id::text=o.source_id AND t.account_id=o.account_id
 AND t.symbol=o.symbol AND t.strategy=o.strategy AND t.order_id=o.order_id::text
WHERE o.account_id=$1 AND o.symbol=ANY($2::text[]) AND o.source_kind='trade_record'
  AND o.evidence->>'order_id'=o.order_id::text AND (o.evidence->'source_ids') ? t.id::text
  AND t.order_type IN ('MARKET','LIMIT') AND t.status IN ('NEW','FILLED')
  AND t.realized_pnl IS NULL
  AND (o.strategy<>'new_coin' OR EXISTS (
        SELECT 1 FROM new_coin.short_positions p
        WHERE p.symbol=o.symbol AND p.status='open'
          AND p.position_id=o.symbol || '_' || o.order_id::text
          AND p.metadata->>'entry_order_id'=o.order_id::text))
  AND NOT EXISTS (
        SELECT 1 FROM trading.trade_records other
        WHERE other.account_id=o.account_id AND other.symbol=o.symbol
          AND other.order_id=o.order_id::text AND other.strategy IS DISTINCT FROM o.strategy)
"""

STRATEGIES = {'new_coin', 'btc_eth', 'btc_eth_aggressive', 'hrs'}
MAX_RECENT = timedelta(days=6)


def _number(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('nonfinite exchange number')
    return number


def _positive_id(value):
    identity = str(value)
    if not identity.isascii() or not identity.isdecimal() or identity[0] == '0':
        raise ValueError('invalid exchange identity')
    return identity


def _fill_key(row, symbol):
    if row['symbol'] != symbol or row['positionSide'] != 'BOTH' or row['side'] not in ('BUY', 'SELL'):
        raise ValueError('fill scope mismatch')
    qty, price = _number(row['qty']), _number(row['price'])
    if qty <= 0 or price <= 0 or type(row['time']) is not int or row['time'] <= 0:
        raise ValueError('invalid signed fill')
    return _positive_id(row['id']), _positive_id(row['orderId']), qty, price, row['side'], row['time']


async def _verify_lot(exchange, symbol, candidate, position, now):
    current = _number(position['positionAmt'])
    price = _number(position['entryPrice'])
    entry_side = 'BUY' if current > 0 else 'SELL'
    entry_id = _positive_id(candidate['order_id'])
    if (position.get('positionSide') != 'BOTH' or price <= 0
            or candidate.get('side') != entry_side or candidate.get('strategy') not in STRATEGIES):
        raise ValueError('position or owner scope mismatch')

    order = await exchange.get_order(symbol, int(entry_id))
    started = int((now - MAX_RECENT).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    if (order.get('positionSide') != 'BOTH' or order.get('reduceOnly') is not False
            or order.get('closePosition') is True
            or type(order.get('time')) is not int or not started <= order['time'] <= end):
        raise ValueError('entry outside verifiable window')
    orders_fills = await exchange.get_user_trades(symbol, order_id=entry_id, limit=1000)
    quantity, average = verified_fill(dict(symbol=symbol, order_id=entry_id, side=entry_side),
                                     order, orders_fills)
    if (abs(average - price) > price * Decimal('0.000001')
            or abs(average - _number(order['avgPrice'])) > price * Decimal('0.000001')):
        raise ValueError('entry price mismatch')
    entries = {_fill_key(f, symbol) for f in orders_fills}
    ledger = candidate.get('ledger_trades')
    if isinstance(ledger, str):
        ledger = json.loads(ledger)
    ledger_keys = {(tid, entry_id, _number(qty), _number(fill_price), side, stamp)
                   for tid, qty, fill_price, side, stamp in (ledger or [])}
    if entries != ledger_keys or len(entries) != len(ledger or []):
        raise ValueError('signed entry and ledger fills differ')
    first, last = min(f[-1] for f in entries), max(f[-1] for f in entries)
    if not order['time'] <= first <= last <= end:
        raise ValueError('fill outside entry window')
    rows = await exchange.get_user_trades(symbol, start_time=first, end_time=end, limit=1000)
    if not isinstance(rows, list) or not 0 < len(rows) < 1000:
        raise ValueError('complete symbol fills unavailable')
    keys = {_fill_key(f, symbol) for f in rows}
    if (not entries.issubset(keys) or len({f[0] for f in keys}) != len(rows)
            or any(not first <= f[-1] <= end for f in keys)):
        raise ValueError('inconsistent fill window')
    reductions = defaultdict(list)
    for row in rows:
        fact = _fill_key(row, symbol)
        if fact in entries:
            continue
        if fact[1] == entry_id or fact[4] == entry_side or fact[-1] <= last:
            raise ValueError('competing order or ambiguous fill chronology')
        reductions[fact[1]].append(row)
    for order_id, fills in reductions.items():
        reduction = await exchange.get_order(symbol, int(order_id))
        if reduction.get('positionSide') != 'BOTH' or reduction.get('reduceOnly') is not True:
            raise ValueError('unproven reducing order')
        qty, _ = verified_fill(dict(symbol=symbol, order_id=order_id, side=fills[0]['side']),
                               reduction, fills)
        quantity -= qty
    latest = await exchange.get_position(symbol)
    matches = [row for row in latest if row.get('symbol') == symbol and _number(row['positionAmt'])]
    if (quantity != abs(current) or len(matches) != 1
            or matches[0].get('positionSide') != 'BOTH'
            or _number(matches[0]['positionAmt']) != current or _number(matches[0]['entryPrice']) != price):
        raise ValueError('signed remaining position does not reconcile')
    return dict(strategy=candidate['strategy'], entry_order_id=entry_id,
                entry_trade_ids=sorted(f[0] for f in entries), reduction_order_ids=sorted(reductions),
                remaining_qty=str(quantity), source='signed_pm_one_way_order_fill_position_v1')


async def resolve_unclaimed_positions(db, exchange, account, positions, claimed_symbols, *, now=None):
    """Return exact individual proofs and unresolved symbols without changing trading state."""
    now = now or datetime.now(timezone.utc)
    pending = defaultdict(list)
    for position in positions:
        if _number(position['positionAmt']) and position['symbol'] not in claimed_symbols:
            pending[position['symbol']].append(position)
    if not pending:
        return {}, {}
    if account != 'default':
        return {}, dict.fromkeys(pending, 'unsupported_account')
    try:
        candidates = await db.fetch_all(_CANDIDATES_SQL, account, sorted(pending))
    except Exception:
        return {}, dict.fromkeys(pending, 'owner_query_failed')
    by_symbol = defaultdict(list)
    for row in candidates:
        by_symbol[row['symbol']].append(row)
    proofs, gaps = {}, {}
    for symbol, live in sorted(pending.items()):
        if len(live) != 1 or len(by_symbol[symbol]) != 1:
            gaps[symbol] = 'owner_identity_missing_or_ambiguous'
            continue
        try:
            proofs[symbol] = await asyncio.wait_for(
                _verify_lot(exchange, symbol, by_symbol[symbol][0], live[0], now), timeout=8)
        except Exception as exc:
            gaps[symbol] = str(exc) if isinstance(exc, ValueError) and type(exc) is ValueError else type(exc).__name__
    return proofs, gaps
