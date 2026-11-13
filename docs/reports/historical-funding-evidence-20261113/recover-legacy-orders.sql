WITH matched AS (
 SELECT t.id,t.strategy,t.symbol,t.order_id,f.account_id,
 min(f.trade_time_utc) first_fill,max(f.trade_time_utc) last_fill,
 min(t.executed_at AT TIME ZONE 'Asia/Shanghai') recorded_at
 FROM trading.trade_records t JOIN trading.execution_fact_fill f
 ON t.order_id=f.order_id::text AND t.symbol=f.symbol AND upper(t.side)=upper(f.side)
 WHERE t.account_id IS NULL AND f.account_id='default'
 AND t.strategy in ('btc_eth','btc_eth_aggressive','new_coin','hrs')
 AND abs(extract(epoch from (t.executed_at AT TIME ZONE 'Asia/Shanghai')-f.trade_time_utc))<=10
 AND NOT EXISTS (SELECT 1 FROM trading.trade_records x WHERE x.symbol=t.symbol AND x.order_id=t.order_id AND (x.strategy<>t.strategy OR x.account_id IS NOT NULL AND x.account_id<>'default'))
 GROUP BY t.id,t.strategy,t.symbol,t.order_id,f.account_id
), unique_orders AS (
 SELECT account_id,symbol,order_id,min(strategy) strategy,array_agg(id::text ORDER BY id::text) ids,min(recorded_at) recorded_at,min(first_fill) first_fill
 FROM matched GROUP BY account_id,symbol,order_id HAVING count(distinct strategy)=1
)
INSERT INTO trading.order_strategy_evidence(account_id,symbol,order_id,strategy,source_kind,source_id,evidence)
SELECT account_id,symbol,order_id::bigint,strategy,'trade_record',ids[1],
jsonb_build_object('source_kind','trade_record','source_ids',ids,'order_id',order_id,'account_proof','authenticated_exchange_order_symbol_side_time','recorded_at',recorded_at,'first_fill',first_fill)
FROM unique_orders ON CONFLICT DO NOTHING RETURNING symbol,strategy;
