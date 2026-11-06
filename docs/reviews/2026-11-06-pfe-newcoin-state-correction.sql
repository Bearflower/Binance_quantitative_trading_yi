-- Run only while trading_system-new_coin is stopped, then restart that same
-- pinned container so it loads the corrected persisted state. PFE 678821 is
-- the sole new_coin BUY close since 2026-11-05 10:02:30 Beijing time.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $repair$
DECLARE changed_rows integer;
BEGIN
    IF (SELECT count(*) FROM trading.trade_records
        WHERE id=4672 AND account_id='default' AND strategy='new_coin'
          AND symbol='PFEUSDT' AND side='BUY' AND order_id='678821'
          AND status='FILLED' AND quantity=3.28 AND price=30.94
          AND realized_pnl=-1.72949999 AND pnl_source='exchange_fill'
          AND close_reason='STOP_LOSS') <> 1
       OR EXISTS (SELECT 1 FROM trading.trade_records
           WHERE strategy='new_coin' AND side='BUY'
             AND executed_at > (SELECT executed_at FROM trading.trade_records WHERE id=4672))
       OR EXISTS (SELECT 1 FROM new_coin.short_positions
           WHERE lower(status) IS DISTINCT FROM 'closed')
    THEN
        RAISE EXCEPTION 'PFE lifecycle or later new_coin close changed; no state correction';
    END IF;
    UPDATE public.strategy_states
       SET state_data = jsonb_set(
           jsonb_set(state_data, '{consecutive_losses}', '1'::jsonb),
           '{cumulative_pnl}', to_jsonb('-1.72949999'::text)),
           updated_at = now()
     WHERE strategy_name='new_coin' AND state_key='main'
       AND state_data->'positions' = '{}'::jsonb
       AND state_data->>'consecutive_losses' = '0'
       AND state_data->>'cumulative_pnl' = '0'
       AND state_data->>'peak_pnl' = '0'
       AND state_data->>'state_version' = '2'
       AND state_data->>'pause_until' IS NULL
       AND state_data->>'drawdown_pause_until' IS NULL;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RAISE EXCEPTION 'new_coin persisted state preimage changed; no correction';
    END IF;
END;
$repair$;
COMMIT;
