"""Recover missing protective exits using signed exchange order identities."""
from collections import defaultdict
from decimal import Decimal

import structlog

log = structlog.get_logger()

GAPS = """
SELECT DISTINCT i.symbol, f.order_id, f.side
FROM trading.execution_fact_income i
LEFT JOIN trading.execution_fact_fill f USING (account_id,symbol,trade_id)
WHERE i.account_id=$1 AND i.income_type='REALIZED_PNL'
AND i.event_time_utc >= least(now()-interval '7 days',
    coalesce((SELECT to_timestamp(nullif(last_reconciled_ts,0)/1000.0)
              FROM {state_table} WHERE account_id=$1), now()),
    coalesce((SELECT to_timestamp(min(event_ms)/1000.0)
              FROM {state_table}_gaps WHERE account_id=$1), now()))
AND NOT EXISTS (SELECT 1 FROM trading.trade_records t WHERE t.account_id=i.account_id
                AND t.symbol=i.symbol AND t.order_id=f.order_id::text AND t.order_type<>'PNL_SUMMARY')
"""
OWNERS = """
SELECT strategy, algo_id, side FROM trading.trade_records
WHERE account_id=$1 AND symbol=$2 AND order_type='CONDITIONAL_ORDER' AND algo_id IS NOT NULL
AND executed_at <= (SELECT max(trade_time_utc) AT TIME ZONE 'Asia/Shanghai'
                    FROM trading.execution_fact_fill WHERE account_id=$1 AND symbol=$2 AND order_id=$3)
AND executed_at >= (SELECT min(trade_time_utc) AT TIME ZONE 'Asia/Shanghai' - interval '30 days'
                    FROM trading.execution_fact_fill WHERE account_id=$1 AND symbol=$2 AND order_id=$3)
ORDER BY executed_at DESC
"""
FILLS = """
SELECT f.*, ARRAY(SELECT i.income FROM trading.execution_fact_income i
 WHERE i.account_id=f.account_id AND i.symbol=f.symbol AND i.trade_id=f.trade_id
 AND i.asset='USDT' AND i.income_type='REALIZED_PNL') AS incomes
FROM trading.execution_fact_fill f WHERE account_id=$1 AND symbol=$2 AND order_id=$3
"""
REASONS = {'STOP': 'STOP_LOSS', 'STOP_MARKET': 'STOP_LOSS',
           'TAKE_PROFIT': 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET': 'TAKE_PROFIT'}


def verify_fill(order, rows, symbol, side, order_id):
    if not rows or (order.get('symbol'), str(order.get('orderId')), order.get('side'),
                    order.get('status')) != (symbol, str(order_id), side, 'FILLED'):
        raise ValueError('ordinary order is not a confirmed fill')
    for row in rows:
        pnl = row['realized_pnl']
        if (row['side'] != side or row['qty'] <= 0 or row['price'] <= 0
                or row['commission_asset'] != 'USDT' or row['commission'] is None
                or pnl is None or (row['incomes'] != [pnl] and not (pnl == 0 and not row['incomes']))):
            raise ValueError('incomplete fill or income evidence')
    qty = sum(row['qty'] for row in rows)
    if qty != Decimal(order['executedQty']):
        raise ValueError('partial order fill history')
    return (qty, sum(row['qty'] * row['price'] for row in rows) / qty,
            -sum(row['commission'] for row in rows), sum(row['realized_pnl'] for row in rows),
            max(row['trade_time_utc'] for row in rows))


async def repair_order(rec, gap):
    symbol, oid, side = gap['symbol'], gap['order_id'], gap['side']
    if not oid:
        raise ValueError('missing execution fact')
    args = (rec.account_id, symbol, oid)
    owners = defaultdict(set)
    for row in await rec.db.fetch_all(OWNERS, *args):
        owners[str(row['algo_id'])].add((row['strategy'], row['side']))
    matches = []
    for aid, claims in owners.items():
        if not any(s == side for _, s in claims):
            continue
        algo = await rec.binance.get_algo_order(int(aid))
        if str(algo.get('actualOrderId')) != str(oid):
            continue
        if (len(claims) != 1 or algo.get('symbol') != symbol or algo.get('side') != side
                or algo.get('algoStatus') != 'FINISHED' or algo.get('orderType') not in REASONS):
            raise ValueError('ambiguous protective order')
        matches.append((next(iter(claims))[0], REASONS[algo['orderType']]))
        # Binance identifies the triggering parent uniquely by the child order ID.
        break
    if len(matches) != 1 or matches[0][0] in rec.excluded_strategies:
        raise ValueError('no eligible protective order owner')
    strategy, reason = matches[0]
    order = await rec.binance.get_order(symbol, oid)
    async with rec.db.pool.acquire() as conn, conn.transaction():
        # Serialize the final absence check with normal trade_records inserts.
        await conn.execute("SET LOCAL lock_timeout='2s'")
        await conn.execute('LOCK TABLE trading.trade_records IN SHARE ROW EXCLUSIVE MODE')
        current = await conn.fetch("SELECT DISTINCT strategy,side FROM trading.trade_records "
                                   "WHERE account_id=$1 AND symbol=$2 AND algo_id=$3 "
                                   "AND order_type='CONDITIONAL_ORDER'", rec.account_id, symbol, aid)
        if {(r['strategy'], r['side']) for r in current} != {(strategy, side)}:
            raise ValueError('protective order ownership changed')
        if await conn.fetchval("""SELECT count(*) FROM trading.trade_records WHERE symbol=$1
                AND order_id=$2 AND (account_id=$3 OR account_id IS NULL)""",
                               symbol, str(oid), rec.account_id):
            return False
        values = verify_fill(order, await conn.fetch(FILLS, *args), symbol, side, oid)
        await conn.execute("""INSERT INTO trading.trade_records
            (account_id,strategy,symbol,order_id,side,order_type,status,close_reason,
             quantity,price,commission,realized_pnl,executed_at,pnl_source)
            VALUES ($1,$2,$3,$4,$5,'EXCHANGE_FILL','FILLED',$6,$7,$8,$9,$10,
                    $11::timestamptz AT TIME ZONE 'Asia/Shanghai','exchange_fill')""",
                           rec.account_id, strategy, symbol, str(oid), side, reason, *values)
    return True


async def nightly_exit_repair(rec):
    inserted = unresolved = 0
    await rec._ensure_state_table()
    query = GAPS.format(state_table=f"{rec.state_schema}.{rec.state_table}")
    for gap in await rec.db.fetch_all(query, rec.account_id):
        try:
            inserted += await repair_order(rec, gap)
        except Exception as exc:
            unresolved += 1
            log.warning('条件单成交待核实', symbol=gap['symbol'], order_id=gap['order_id'], error=str(exc))
    log.info('凌晨成交补录完成', inserted=inserted, unresolved=unresolved)
