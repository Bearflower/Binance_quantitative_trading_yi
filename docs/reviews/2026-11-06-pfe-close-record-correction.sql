-- One-shot PFE close record correction. Exchange GET /um/order 678821 returned
-- FILLED, executedQty 3.28, avgPrice 30.94; 2026-11-05 02:02:30 UTC journald
-- recorded emergency stop loss and closed_qty 3.28. Recheck live evidence first.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $repair$
DECLARE changed_rows integer;
BEGIN
    IF (SELECT count(*) FROM trading.execution_fact_fill
        WHERE account_id='default' AND symbol='PFEUSDT' AND order_id=678821) <> 2
       OR (SELECT count(*) FROM trading.execution_fact_fill
           WHERE account_id='default' AND symbol='PFEUSDT' AND order_id=678821
             AND trade_id IN (58293,58294) AND side='BUY') <> 2
       OR (SELECT count(DISTINCT trade_id) FROM trading.execution_fact_fill
           WHERE account_id='default' AND symbol='PFEUSDT' AND order_id=678821) <> 2
       OR (SELECT sum(qty) FROM trading.execution_fact_fill
           WHERE account_id='default' AND symbol='PFEUSDT' AND order_id=678821) IS DISTINCT FROM 3.28
       OR (SELECT min(price) FROM trading.execution_fact_fill
           WHERE account_id='default' AND symbol='PFEUSDT' AND order_id=678821) IS DISTINCT FROM 30.94
       OR (SELECT max(price) FROM trading.execution_fact_fill
           WHERE account_id='default' AND symbol='PFEUSDT' AND order_id=678821) IS DISTINCT FROM 30.94
       OR (SELECT sum(realized_pnl) FROM trading.execution_fact_fill
           WHERE account_id='default' AND symbol='PFEUSDT' AND order_id=678821) IS DISTINCT FROM -1.72949999
    THEN
        RAISE EXCEPTION 'PFE fill evidence changed; no close record correction';
    END IF;
    UPDATE trading.trade_records
       SET status='FILLED', quantity=3.28, price=30.94,
           close_reason='STOP_LOSS'
     WHERE id=4672 AND account_id='default' AND strategy='new_coin'
       AND symbol='PFEUSDT' AND side='BUY' AND order_id='678821'
       AND order_type='MARKET' AND status='NEW' AND quantity=0 AND price=0
       AND realized_pnl=-1.72949999 AND pnl_source='exchange_fill'
       AND close_reason IS NULL;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RAISE EXCEPTION 'PFE close record preimage changed; no correction';
    END IF;
END;
$repair$;
COMMIT;
