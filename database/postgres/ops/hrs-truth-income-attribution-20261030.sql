-- Reviewed TRUTHUSDT income attribution, 2026-10-30. Run with psql -v ON_ERROR_STOP=1.
-- Binance PM income tranId suffix equals userTrades id for each of the seven rows.
-- All seven userTrades share orderId 1907933328, BUY side, and exact event timestamp.
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;

CREATE TEMP TABLE reviewed_hrs_income (
    tran_id bigint PRIMARY KEY,
    income numeric(20, 8) NOT NULL,
    trade_id bigint NOT NULL
) ON COMMIT DROP;

INSERT INTO reviewed_hrs_income (tran_id, income, trade_id) VALUES
    (103111173965421, -0.89938466, 173965421),
    (103111173965422, -1.72148366, 173965422),
    (103111173965423, -2.17104460, 173965423),
    (103111173965424, -10.09951384, 173965424),
    (103111173965425, -1.60125297, 173965425),
    (103111173965426, -0.66891529, 173965426),
    (103111173965427, -1.01920394, 173965427);

DO $$
BEGIN
    IF (SELECT count(*) FROM reviewed_hrs_income) <> 7
       OR (SELECT sum(income) FROM reviewed_hrs_income) <> -18.18079896
       OR EXISTS (
           SELECT 1 FROM reviewed_hrs_income
           WHERE tran_id % 1000000000 <> trade_id
       ) THEN
        RAISE EXCEPTION 'reviewed exchange evidence changed';
    END IF;
    IF (SELECT count(*) FROM trading.trade_records
        WHERE id = 4033 AND strategy = 'hrs' AND symbol = 'TRUTHUSDT'
          AND side = 'SELL' AND order_type = 'LIMIT' AND order_id = '1854621367'
          AND executed_at = TIMESTAMP '2026-10-15 15:22:44.931741') <> 1
       OR (SELECT count(*) FROM trading.trade_records
           WHERE id = 4516 AND strategy = 'hrs' AND symbol = 'TRUTHUSDT'
             AND side = 'BUY' AND order_type = 'PNL_SUMMARY'
             AND realized_pnl = -18.18079896
             AND executed_at = TIMESTAMP '2026-10-29 13:22:22.226652') <> 1
       OR (SELECT count(*) FROM trading.trade_records
           WHERE strategy = 'hrs' AND symbol = 'TRUTHUSDT' AND order_type = 'LIMIT') <> 1
       OR (SELECT count(*) FROM trading.trade_records
           WHERE strategy = 'hrs' AND symbol = 'TRUTHUSDT' AND order_type = 'PNL_SUMMARY') <> 1
       OR (SELECT count(*) FROM trading.trade_records
           WHERE strategy = 'hrs' AND symbol = 'TRUTHUSDT') <> 5
       OR EXISTS (SELECT 1 FROM trading.trade_records
                  WHERE symbol = 'TRUTHUSDT' AND strategy <> 'hrs')
       OR EXISTS (SELECT 1 FROM hrs.hrs_positions WHERE symbol = 'TRUTHUSDT') THEN
        RAISE EXCEPTION 'HRS lifecycle evidence changed';
    END IF;
END $$;

INSERT INTO trading.hrs_income_attribution (
    account_id, tran_id, strategy, symbol, income, event_time_ms,
    evidence, reviewed_by
)
SELECT 'default', r.tran_id, 'hrs', 'TRUTHUSDT', r.income, 1793248465546,
       jsonb_build_object(
           'exchange_user_trade_id', r.trade_id,
           'exchange_order_id', '1907933328',
           'entry_trade_record_id', 4033,
           'summary_trade_record_id', 4516,
           'review', 'Binance PM income and userTrades exact ID/time/amount match; HRS summary sum matches'
       ),
       'codex:exchange-fill-review-2026-10-30'
FROM reviewed_hrs_income r
ON CONFLICT (account_id, tran_id) DO NOTHING;

DO $$
BEGIN
    IF (SELECT count(*) FROM trading.hrs_income_attribution a
        JOIN reviewed_hrs_income r USING (tran_id)
        WHERE a.account_id = 'default' AND a.strategy = 'hrs'
          AND a.symbol = 'TRUTHUSDT' AND a.income = r.income
          AND a.event_time_ms = 1793248465546) <> 7 THEN
        RAISE EXCEPTION 'reviewed attribution rows incomplete or conflicting';
    END IF;
END $$;

COMMIT;
