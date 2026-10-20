-- =====================================================================
-- 12. MTPCS 系信号持久化：补 dedup_key 列 + 唯一索引
-- ---------------------------------------------------------------------
-- 对 btc_eth.trade_signals 和 btc_eth_aggressive.trade_signals 两张表，
-- 幂等补齐：
--   - dedup_key VARCHAR(64)  （同小时同币同方向视为同一决策的幂等键）
--   - uq_<schema>_signals_dedup 唯一索引 （支撑 ON CONFLICT DO UPDATE）
-- IF NOT EXISTS 保证在生产已部署的旧表上可重复执行，无副作用。
-- =====================================================================

-- ---------------- btc_eth ----------------
ALTER TABLE btc_eth.trade_signals
    ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS uq_btc_eth_signals_dedup
    ON btc_eth.trade_signals(dedup_key);

-- ---------------- btc_eth_aggressive ----------------
ALTER TABLE btc_eth_aggressive.trade_signals
    ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS uq_btc_eth_aggressive_signals_dedup
    ON btc_eth_aggressive.trade_signals(dedup_key);
