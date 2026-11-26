-- Run after PM writers have stopped: legacy binaries still target their former conflict indexes.
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE UNIQUE INDEX IF NOT EXISTS idx_co_scoped_algo ON btc_eth.condition_orders
  (account_id,strategy_name,symbol,algo_id) NULLS NOT DISTINCT WHERE algo_id IS NOT NULL AND status='OPEN';
CREATE UNIQUE INDEX IF NOT EXISTS idx_co_scoped_order ON btc_eth.condition_orders
  (account_id,strategy_name,symbol,order_id) NULLS NOT DISTINCT WHERE order_id IS NOT NULL AND status='OPEN';
DROP INDEX IF EXISTS btc_eth.idx_co_algo_unique;
DROP INDEX IF EXISTS btc_eth.idx_co_order_unique;
COMMIT;
