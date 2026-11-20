"""Verify known protective exits without certifying the order universe."""

import asyncio
from decimal import Decimal, InvalidOperation
import json
import re

from ai_tuner.monitor.hrs_closed_lot import read_closed_lot
from shared.entry_fill_evidence import verified_fill


_LIMIT = 32
_STOP_TYPES = ('STOP', 'STOP_MARKET')
_PROTECTION_TYPES = _STOP_TYPES + ('TAKE_PROFIT', 'TAKE_PROFIT_MARKET')
_CANDIDATES = """
WITH orders AS (
    SELECT e.account_id, e.strategy, e.symbol, e.order_id,
           e.evidence->>'algo_id' AS algo_id,
           e.evidence->>'actual_order_id' AS actual_order_id,
           e.evidence->>'payload_sha256' AS payload_sha256
    FROM trading.order_strategy_evidence e
    WHERE e.account_id=$1 AND e.source_kind='pm_algo_link'
      AND EXISTS (
          SELECT 1 FROM trading.execution_fact_fill f
          WHERE (f.account_id,f.symbol,f.order_id)=(e.account_id,e.symbol,e.order_id)
            AND f.trade_time_utc>=to_timestamp($2::bigint/1000.0)
            AND f.trade_time_utc<to_timestamp($3::bigint/1000.0)
      )
    ORDER BY e.observed_at DESC,e.symbol,e.order_id
    LIMIT $4
)
SELECT o.*, x.episode_entry_order_id AS entry_order_id, x.stop_side AS episode_side,
       x.quantity AS episode_qty,
       ARRAY(SELECT t.trade_id::text FROM trading.aggressive_stop_exit_trades t
             WHERE (t.account_id,t.symbol,t.algo_id)=(x.account_id,x.symbol,x.algo_id))
           AS episode_trade_ids,
       (SELECT jsonb_agg(jsonb_build_object('id', f.trade_id::text, 'qty', f.qty::text,
                   'price', f.price::text, 'side', f.side,
                   'time', (EXTRACT(EPOCH FROM f.trade_time_utc)*1000)::bigint))
        FROM trading.execution_fact_fill f
        WHERE (f.account_id,f.symbol,f.order_id)=(o.account_id,o.symbol,o.order_id)) AS fills
FROM orders o
LEFT JOIN trading.aggressive_stop_exits x
  ON (x.account_id,x.strategy,x.symbol,x.order_id)=(o.account_id,o.strategy,o.symbol,o.order_id)
 AND x.algo_id::text=o.algo_id
"""


def _id(value):
    if not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise ValueError('invalid order or trade id')
    return str(value)


def _fill_details(rows):
    details = {}
    for row in rows:
        trade_id = _id(row['id'])
        stamp = row['time']
        if trade_id in details or type(stamp) is not int or stamp <= 0:
            raise ValueError('invalid or duplicated trade')
        details[trade_id] = stamp, Decimal(str(row['qty'])), Decimal(str(row['price'])), row['side']
    return details


def _one_way_reduces(order):
    reduce_only = order.get('reduceOnly')
    return (reduce_only is True or reduce_only == 'true') and order.get('positionSide') == 'BOTH'


def verify_protective_exit(row, algo, order, trades):
    algo_id, order_id = _id(row['algo_id']), _id(row['order_id'])
    kind, side = algo.get('orderType'), algo.get('side')
    if (row['actual_order_id'] != order_id or not row['strategy']
            or not re.fullmatch(r'[0-9a-f]{64}', row['payload_sha256'])
            or str(algo.get('algoId')) != algo_id or str(algo.get('actualOrderId')) != order_id
            or algo.get('symbol') != row['symbol'] or algo.get('algoStatus') not in ('TRIGGERED', 'FINISHED')
            or kind not in _PROTECTION_TYPES or side not in ('BUY', 'SELL')
            or not _one_way_reduces(algo) or not _one_way_reduces(order)):
        raise ValueError('protective order identity or mode differs')
    record = dict(symbol=row['symbol'], side=side, order_id=order_id)
    quantity, _ = verified_fill(record, order, trades)
    if any(t.get('positionSide') != 'BOTH' for t in trades):
        raise ValueError('unsupported protective fill mode')
    signed = _fill_details(trades)
    stored = json.loads(row['fills']) if isinstance(row['fills'], str) else row['fills']
    if signed != _fill_details(stored):
        raise ValueError('signed and collected trades differ')
    entry_order_id = None
    if row['entry_order_id'] is not None:
        if (kind not in _STOP_TYPES or row['episode_side'] != side
                or Decimal(str(row['episode_qty'])) != quantity
                or set(row['episode_trade_ids']) != set(signed)):
            raise ValueError('durable stop episode differs')
        entry_order_id = _id(row['entry_order_id'])
    return dict(strategy=row['strategy'], symbol=row['symbol'], algo_id=algo_id,
                order_id=order_id, entry_order_id=entry_order_id, kind=kind,
                trade_ids=sorted(signed), trade_times_ms={k: v[0] for k, v in signed.items()})


async def read_protective_exits(db, exchange, account_id, start_ms, end_ms):
    report = dict(scope='known_pm_protective_orders', account_id=account_id,
                  start_ms=start_ms, end_ms=end_ms, links=[], issues=[])
    if not account_id or type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms:
        raise ValueError('invalid protection evidence window')
    try:
        rows = await asyncio.wait_for(db.fetch_all(_CANDIDATES, account_id, start_ms, end_ms, _LIMIT + 1), 6)
    except Exception as exc:
        report['issues'].append(dict(reason='protective_exit_candidates:' + type(exc).__name__))
        return report, set()
    if len(rows) > _LIMIT:
        report['issues'].append(dict(reason='known_protective_order_scan_capped'))
    capacity = asyncio.Semaphore(4)

    async def verify(row):
        async with capacity:
            try:
                if row['account_id'] != account_id:
                    raise ValueError('account differs')
                algo_id, order_id = _id(row['algo_id']), _id(row['order_id'])
                algo, order, trades = await asyncio.wait_for(asyncio.gather(
                    exchange.get_algo_order(int(algo_id)), exchange.get_order(row['symbol'], int(order_id)),
                    exchange.get_user_trades(row['symbol'], order_id=order_id, limit=1000)), 6)
                link = verify_protective_exit(row, algo, order, trades)
            except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
                return None, dict(symbol=row['symbol'], order_id=str(row['order_id']), reason='proof_mismatch',
                                  detail=type(exc).__name__)
            except Exception as exc:
                return None, dict(symbol=row['symbol'], order_id=str(row['order_id']),
                                  reason='protective_exit_lookup:' + type(exc).__name__)
            if link['strategy'] == 'hrs':
                try:
                    lot = await asyncio.wait_for(
                        read_closed_lot(db, exchange, account_id, link, algo, trades), 8)
                    link['entry_order_id'] = lot['entry_order_id']
                    link['closed_lot'] = lot
                except ValueError as exc:
                    link['closed_lot_gap'] = str(exc)
                except Exception as exc:
                    link['closed_lot_gap'] = type(exc).__name__
            return link, None

    keys = set()
    for link, issue in await asyncio.gather(*(verify(row) for row in rows[:_LIMIT])):
        if issue:
            report['issues'].append(issue)
            continue
        report['links'].append(link)
        keys.update((account_id, link['symbol'], trade, link['order_id'], stamp)
                    for trade, stamp in link['trade_times_ms'].items() if start_ms <= stamp < end_ms)
    return report, keys
