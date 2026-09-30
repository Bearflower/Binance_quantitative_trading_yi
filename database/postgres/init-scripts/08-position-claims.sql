-- ============================================
-- 币种开仓占用表（R07：跨策略开仓预占互斥）
--   归属权威仍是 trading.trade_records（未平开仓单）；本表仅为「开仓窗口期预占」，
--   以部分唯一索引保证同一 symbol 至多一条「有效占用」，实现跨策略并发互斥。
--   文档依据：docs/plans/fix-2026-09-29-p0-r01-r08-architecture.md（§11.2）
-- ============================================
CREATE SCHEMA IF NOT EXISTS trading;
GRANT ALL PRIVILEGES ON SCHEMA trading TO trading_user;

CREATE TABLE IF NOT EXISTS trading.position_claims (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    strategy        VARCHAR(50) NOT NULL,
    trade_intent_id VARCHAR(64) NOT NULL,          -- 一次交易决策的稳定标识
    claim_state     VARCHAR(16) NOT NULL DEFAULT 'PENDING',  -- PENDING/ACTIVE/RELEASED
    created_at      TIMESTAMP   NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMP   NOT NULL,          -- TTL：超时后可由清理任务回收
    released_at     TIMESTAMP,
    reason          VARCHAR(64)
);

-- 部分唯一索引：同一 symbol 至多一条「有效占用」（PENDING/ACTIVE）→ 并发互斥
CREATE UNIQUE INDEX IF NOT EXISTS uq_position_claims_active_symbol
    ON trading.position_claims(symbol)
    WHERE claim_state IN ('PENDING','ACTIVE');

-- 过期清理扫描索引
CREATE INDEX IF NOT EXISTS idx_position_claims_expires
    ON trading.position_claims(expires_at)
    WHERE claim_state IN ('PENDING','ACTIVE');

-- 策略维度查询索引
CREATE INDEX IF NOT EXISTS idx_position_claims_strategy
    ON trading.position_claims(strategy, symbol);

-- 授权：给交易连接用户读写权限（与 07-market-circuit-breaker.sql 同风格）
GRANT SELECT, INSERT, UPDATE, DELETE ON trading.position_claims TO trading_user;
GRANT USAGE, SELECT ON SEQUENCE trading.position_claims_id_seq TO trading_user;

-- 输出创建结果
DO $$
BEGIN
    RAISE NOTICE '币种开仓占用表创建完成: trading.position_claims';
END $$;