-- Explicit per-order provenance for the dedicated PM grid account.
-- NULL client IDs on legacy strategies remain unrestricted.
ALTER TABLE trading.trade_records
    ADD COLUMN IF NOT EXISTS client_order_id TEXT;
ALTER TABLE trading.trade_records
    ADD COLUMN IF NOT EXISTS grid_epoch_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_grid_pm_order_provenance
    ON trading.trade_records (account_id, strategy, client_order_id)
    WHERE client_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_grid_pm_epoch
    ON trading.trade_records (account_id, grid_epoch_id)
    WHERE grid_epoch_id IS NOT NULL;
