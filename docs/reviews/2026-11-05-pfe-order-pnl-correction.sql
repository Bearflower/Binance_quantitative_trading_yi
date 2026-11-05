-- PFEUSDT 2026-11-05 10:02 CST: two exchange fills belong to order 678821.
-- Apply only after code review and deployment. psql -v ON_ERROR_STOP=1 -f this-file.
-- The assertions make this a no-op if production facts or preimages have changed.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $correction$
DECLARE
    changed_rows integer;
BEGIN
    IF (SELECT count(*) FROM trading.execution_fact_fill
        WHERE account_id = 'default' AND symbol = 'PFEUSDT'
          AND trade_id IN (58293, 58294) AND order_id = 678821
          AND side = 'BUY') <> 2
       OR (SELECT count(*) FROM trading.execution_fact_fill
           WHERE account_id = 'default' AND symbol = 'PFEUSDT'
             AND order_id = 678821) <> 2
       OR EXISTS (SELECT 1 FROM trading.execution_fact_fill
           WHERE account_id = 'default' AND symbol = 'PFEUSDT'
             AND order_id = 678808)
       OR (SELECT sum(realized_pnl) FROM trading.execution_fact_fill
           WHERE account_id = 'default' AND symbol = 'PFEUSDT'
             AND trade_id IN (58293, 58294) AND order_id = 678821) IS DISTINCT FROM -1.72949999
       OR (SELECT sum(qty) FROM trading.execution_fact_fill
           WHERE account_id = 'default' AND symbol = 'PFEUSDT'
             AND trade_id IN (58293, 58294) AND order_id = 678821) IS DISTINCT FROM 3.28
       OR (SELECT count(*) FROM trading.execution_fact_income
           WHERE account_id = 'default' AND symbol = 'PFEUSDT'
             AND trade_id IN (58293, 58294) AND income_type = 'REALIZED_PNL') <> 2
       OR (SELECT count(DISTINCT trade_id) FROM trading.execution_fact_income
           WHERE account_id = 'default' AND symbol = 'PFEUSDT'
             AND trade_id IN (58293, 58294) AND income_type = 'REALIZED_PNL') <> 2
       OR (SELECT sum(income) FROM trading.execution_fact_income
           WHERE account_id = 'default' AND symbol = 'PFEUSDT'
             AND trade_id IN (58293, 58294) AND income_type = 'REALIZED_PNL') IS DISTINCT FROM -1.72949999
       OR EXISTS (
           SELECT 1 FROM trading.execution_fact_fill f
           JOIN trading.execution_fact_income i
             ON i.account_id = f.account_id AND i.symbol = f.symbol
            AND i.trade_id = f.trade_id AND i.income_type = 'REALIZED_PNL'
           WHERE f.account_id = 'default' AND f.symbol = 'PFEUSDT'
             AND f.trade_id IN (58293, 58294)
             AND i.income IS DISTINCT FROM f.realized_pnl
       )
    THEN
        RAISE EXCEPTION 'PFE exchange evidence changed; no correction applied';
    END IF;

    UPDATE trading.trade_records
       SET realized_pnl = NULL, pnl_source = NULL
     WHERE id = 4671 AND account_id = 'default' AND strategy = 'new_coin'
       AND symbol = 'PFEUSDT' AND side = 'BUY' AND order_id = '678808'
       AND status = 'CANCELED' AND realized_pnl = -0.18982317
       AND pnl_source IS NULL;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RAISE EXCEPTION 'PFE canceled order preimage changed; no correction applied';
    END IF;

    UPDATE trading.trade_records
       SET realized_pnl = -1.72949999, pnl_source = 'exchange_fill'
     WHERE id = 4672 AND account_id = 'default' AND strategy = 'new_coin'
       AND symbol = 'PFEUSDT' AND side = 'BUY' AND order_id = '678821'
       AND status = 'NEW' AND quantity = 0 AND realized_pnl = -1.53967682
       AND pnl_source IS NULL AND close_reason IS NULL;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RAISE EXCEPTION 'PFE filled order preimage changed; no correction applied';
    END IF;
END;
$correction$;
COMMIT;
