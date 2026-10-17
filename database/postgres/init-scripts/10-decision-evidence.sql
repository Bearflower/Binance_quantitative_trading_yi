-- ============================================
-- C1 决策与特征证据契约表（幂等）
--   trading.decision_evidence
--   trading.decision_feature_evidence
--
--   稳定标识口径：
--     decision_id = sha256("account|strategy|symbol|direction|entry_mode|cycle_id")[:32]
--     cycle_id    = sha256("strategy|symbol|direction|entry_mode|cycle_anchor")[:16]
--     cycle_anchor= 周期固定锚点（该周期 K 线窗口起点毫秒，非 now）
--
--   冲突幂等：
--     - decision_evidence：ON CONFLICT (decision_id) DO UPDATE
--     - decision_feature_evidence：UNIQUE NULLS NOT DISTINCT (decision_id, name, source)
--       避免 source=NULL 时 UNIQUE 约束失效
--
--   注意：所有表的 updated_at 均无触发器，UPDATE 时必须显式写 now()。
-- ============================================

CREATE SCHEMA IF NOT EXISTS trading;
GRANT ALL PRIVILEGES ON SCHEMA trading TO trading_user;

-- ── 决策级证据快照 ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.decision_evidence (
    account_id           TEXT        NOT NULL,
    strategy_id          TEXT        NOT NULL,
    decision_id          TEXT        PRIMARY KEY,
    cycle_id             TEXT        NOT NULL,
    decision_time_utc    TIMESTAMPTZ NOT NULL,
    code_hash            TEXT,
    config_hash          TEXT,
    entry_mode           TEXT,
    universe_snapshot_id TEXT,
    symbol               TEXT        NOT NULL,
    direction            TEXT        NOT NULL CHECK (direction IN ('LONG','SHORT','BOTH','FLAT')),
    score_components     JSONB,
    thresholds           JSONB,
    planned_qty          NUMERIC(20,8),
    planned_notional     NUMERIC(20,8),
    margin_limit         NUMERIC(20,8),
    margin_limit_source  TEXT,
    accepted             BOOLEAN     NOT NULL,
    rejection_reason     TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decision_evt_strategy_time
    ON trading.decision_evidence(strategy_id, decision_time_utc);

CREATE INDEX IF NOT EXISTS idx_decision_evt_symbol_dir
    ON trading.decision_evidence(symbol, direction);

CREATE INDEX IF NOT EXISTS idx_decision_evt_cycle
    ON trading.decision_evidence(cycle_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON trading.decision_evidence TO trading_user;

-- ── 特征级证据快照（每个决策可有多条） ──────────────────────────
CREATE TABLE IF NOT EXISTS trading.decision_feature_evidence (
    id              BIGSERIAL   NOT NULL,
    decision_id     TEXT        NOT NULL REFERENCES trading.decision_evidence(decision_id) ON DELETE CASCADE,
    name            TEXT        NOT NULL,
    value           TEXT        NOT NULL,
    unit            TEXT,
    event_time      TIMESTAMPTZ,
    available_at    TIMESTAMPTZ,
    availability    TEXT        NOT NULL DEFAULT 'unknown'
                    CHECK (availability IN ('live','lagging','unknown')),
    fetched_at      TIMESTAMPTZ,
    source          TEXT,
    quality_flag    TEXT,
    fallback_reason TEXT,
    lookback_actual TEXT,
    -- PostgreSQL 15+：NULL 也参与去重，避免 source=NULL 时 UNIQUE 约束失效
    UNIQUE NULLS NOT DISTINCT (decision_id, name, source)
    -- 注：已删除冗余索引 idx_decision_feat_decision，
    -- 其 (decision_id) 前缀列已被上述 UNIQUE 约束隐式覆盖。
);

GRANT SELECT, INSERT, UPDATE, DELETE ON trading.decision_feature_evidence TO trading_user;
GRANT USAGE, SELECT ON SEQUENCE trading.decision_feature_evidence_id_seq TO trading_user;

DO $$
BEGIN
    RAISE NOTICE '决策与特征证据表创建完成: trading.decision_evidence / decision_feature_evidence';
END $$;
