"""Signed ordinary orders matched to canonical ownership and collected fills."""
import asyncio
from decimal import Decimal, InvalidOperation
import json

from ai_tuner.allocation.activity_evidence import STRATEGIES
from ai_tuner.monitor.protective_exit_evidence import _fill_details
from ai_tuner.monitor.trial_risk_evidence import stressed_net
from shared.entry_fill_evidence import verified_fill


_LIMIT = 32
_CANDIDATES = """
WITH candidates AS (
 SELECT e.account_id,e.symbol,e.order_id,e.strategy,t.side AS stored_side
 FROM trading.order_strategy_evidence e
 JOIN trading.trade_records t ON t.id::text=e.source_id AND t.account_id=e.account_id
      AND t.symbol=e.symbol AND t.order_id=e.order_id::text AND t.strategy=e.strategy
 WHERE e.account_id=$1 AND e.source_kind='trade_record' AND t.side IN ('BUY','SELL')
   AND NOT EXISTS (
       SELECT 1 FROM trading.trade_records conflicting
       WHERE conflicting.account_id=e.account_id AND conflicting.symbol=e.symbol
         AND conflicting.order_id=e.order_id::text AND conflicting.strategy IS DISTINCT FROM e.strategy
   )
   AND NOT EXISTS (
       SELECT 1 FROM trading.entry_order_observation o
       WHERE (o.account_id,o.symbol,o.order_id)=(e.account_id,e.symbol,e.order_id::text) AND o.outcome='ok'
   )
   AND EXISTS (
       SELECT 1 FROM trading.execution_fact_fill f
       WHERE (f.account_id,f.symbol,f.order_id)=(e.account_id,e.symbol,e.order_id)
         AND f.trade_time_utc>=to_timestamp($2::bigint/1000.0)
         AND f.trade_time_utc<to_timestamp($3::bigint/1000.0)
   )
 ORDER BY e.observed_at DESC,e.symbol,e.order_id
 LIMIT $4
)
SELECT c.*,
 (SELECT jsonb_agg(jsonb_build_object('id',f.trade_id::text,'qty',f.qty::text,'price',f.price::text,
         'side',f.side,'time',(extract(epoch FROM f.trade_time_utc)*1000)::bigint))
  FROM trading.execution_fact_fill f
  WHERE (f.account_id,f.symbol,f.order_id)=(c.account_id,c.symbol,c.order_id)) AS fills
FROM candidates c
"""


def _execution_costs(trades):
    realised, fees, turnover = Decimal(0), Decimal(0), Decimal(0)
    try:
        for trade in trades:
            gain, commission = (Decimal(str(trade[k])) for k in ('realizedPnl', 'commission'))
            if (trade.get('commissionAsset') != 'USDT' or not gain.is_finite()
                    or not commission.is_finite() or commission < 0):
                return {}
            realised += gain
            fees += commission
            turnover += Decimal(str(trade['qty'])) * Decimal(str(trade['price']))
    except (KeyError, InvalidOperation, TypeError):
        return {}
    net = realised - fees
    return dict(execution_net_usdt=str(net), commission_usdt=str(fees), turnover_usdt=str(turnover),
                execution_stressed_net_usdt=str(stressed_net(net, fees, turnover)),
                economics_scope='order_execution_before_funding')


def verify_ordinary_execution(row, order, trades):
    if (not isinstance(order, dict) or type(order.get('reduceOnly')) is not bool
            or order.get('positionSide') != 'BOTH' or order.get('closePosition', False) is not False
            or row['strategy'] not in STRATEGIES):
        raise ValueError('ordinary order mode or strategy is unverified')
    record = dict(symbol=row['symbol'], side=row['stored_side'], order_id=row['order_id'])
    verified_fill(record, order, trades)
    if any(trade.get('positionSide') != 'BOTH' for trade in trades):
        raise ValueError('ordinary trade mode differs')
    signed = _fill_details(trades)
    stored = json.loads(row['fills']) if isinstance(row['fills'], str) else row['fills']
    if signed != _fill_details(stored):
        raise ValueError('signed and collected trades differ')
    classification = 'ordinary_reduce_only' if order['reduceOnly'] else 'known_strategy_execution'
    return dict(strategy=row['strategy'], symbol=row['symbol'], order_id=str(row['order_id']),
                classification=classification, trade_times_ms={k: v[0] for k, v in signed.items()},
                **_execution_costs(trades))


async def read_ordinary_executions(db, exchange, account, start_ms, end_ms):
    report = dict(account_id=account, start_ms=start_ms, end_ms=end_ms, links=[], issues=[])
    try:
        rows = await asyncio.wait_for(db.fetch_all(_CANDIDATES, account, start_ms, end_ms, _LIMIT+1), 6)
    except Exception as exc:
        report['issues'].append(dict(reason='ordinary_candidates:' + type(exc).__name__))
        return report, set(), set()
    if len(rows) > _LIMIT:
        report['issues'].append(dict(reason='ordinary_order_scan_capped'))
    capacity = asyncio.Semaphore(4)

    async def verify(row):
        async with capacity:
            try:
                if row['account_id'] != account:
                    raise ValueError('ordinary order account differs')
                symbol, order_id = row['symbol'], row['order_id']
                order, trades = await asyncio.wait_for(asyncio.gather(
                    exchange.get_order(symbol, int(order_id)),
                    exchange.get_user_trades(symbol, order_id=str(order_id), limit=1000),
                ), 6)
                return verify_ordinary_execution(row, order, trades), None
            except Exception as exc:
                return None, dict(symbol=row['symbol'], order_id=str(row['order_id']),
                                  reason='ordinary_order_proof:' + type(exc).__name__)

    reductions, owners = set(), set()
    for link, issue in await asyncio.gather(*(verify(row) for row in rows[:_LIMIT])):
        if issue:
            report['issues'].append(issue)
            continue
        report['links'].append(link)
        for trade_id, stamp in link['trade_times_ms'].items():
            if start_ms <= stamp < end_ms:
                key = (account, link['symbol'], trade_id, link['order_id'], stamp)
                if link['classification'] == 'ordinary_reduce_only':
                    reductions.add(key)
                else:
                    owners.add((*key, link['strategy']))
    return report, reductions, owners
