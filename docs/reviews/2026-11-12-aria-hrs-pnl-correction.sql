-- ARIAUSDT HRS position opened 2026-11-12 01:29:18 UTC and closed
-- 2026-11-12 02:30:33 UTC. Exchange userTrades showed exactly two fills:
-- SELL order 1865744451, trade 206788811, qty 31, realizedPnl 0;
-- BUY order 1866457728, trade 206830350, qty 31, realizedPnl 0.17484000.
-- The BUY order is actualOrderId of HRS TP algo 2000001653867823.
-- REALIZED_PNL income tranId 102461206830350, tradeId 206830350,
-- amount 0.17484000 USDT, matches that fill. No other ARIAUSDT fills or
-- REALIZED_PNL income occurred in this UTC window.
-- The HRS conditional-order row stores algoId but not its actualOrderId;
-- the current live-PnL matcher checks trade_records.order_id only, so it
-- fell back to theory. The HRS entry path did not call reservation reconcile
-- after this position closed. These producer gaps require a separate code
-- change; this script corrects only this independently proven position.
-- Original row: id 4726, realized_pnl 0.60636000,
-- pnl_source theoretical_estimate. Reversal: restore those two values only
-- after rechecking no later dependent change. Do not subtract fees here:
-- REALIZED_PNL and the HRS PNL_SUMMARY contract record gross trade PnL.

BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '15s';
DO $$
DECLARE
    prior trading.trade_records%ROWTYPE;
    summary_count integer;
BEGIN
    SELECT * INTO prior FROM trading.trade_records WHERE id = 4726 FOR UPDATE;
    IF NOT FOUND OR prior.account_id IS DISTINCT FROM 'default'
       OR prior.strategy IS DISTINCT FROM 'hrs'
       OR prior.symbol IS DISTINCT FROM 'ARIAUSDT'
       OR prior.side IS DISTINCT FROM 'BUY'
       OR prior.order_type IS DISTINCT FROM 'PNL_SUMMARY'
       OR prior.status IS DISTINCT FROM 'FILLED'
       OR prior.executed_at IS DISTINCT FROM timestamp '2026-11-12 10:30:40.134899'
       OR prior.realized_pnl IS DISTINCT FROM 0.60636000
       OR prior.pnl_source IS DISTINCT FROM 'theoretical_estimate'
       OR prior.close_reason IS NOT NULL THEN
        RAISE EXCEPTION 'ARIA HRS PNL_SUMMARY precondition drift';
    END IF;

    SELECT count(*) INTO summary_count
      FROM trading.trade_records
     WHERE account_id = 'default' AND strategy = 'hrs'
       AND symbol = 'ARIAUSDT' AND side = 'BUY'
       AND order_type = 'PNL_SUMMARY'
       AND executed_at >= timestamp '2026-11-12 09:00:00'
       AND executed_at < timestamp '2026-11-12 11:00:00';
    IF summary_count <> 1 THEN
        RAISE EXCEPTION 'ARIA HRS summary cardinality drift: %', summary_count;
    END IF;

    UPDATE trading.trade_records
       SET realized_pnl = 0.17484000,
           pnl_source = 'exchange_order_matched'
     WHERE id = 4726;
END
$$;

SELECT id, account_id, strategy, symbol, realized_pnl, pnl_source
  FROM trading.trade_records WHERE id = 4726;
COMMIT;
