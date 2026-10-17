-- ============================================================================
-- 已迁入 init-scripts/10-decision-evidence.sql，init-scripts 为权威执行路径（2026-10-15）。
-- ============================================================================
-- C1「决策与特征证据契约」—— 幂等建表 DDL
-- 作者：AI engineer（2026-10-15）
-- ============================================================================
-- 部署方式说明（必读）：
--   本文件位于 migrations/ 目录，**服务器部署不会自动执行**。
--   原因：当前 CI 部署链路仅自动执行 database/postgres/init-scripts/ 下的脚本，
--   migrations/ 是手动迁移目录。
--   后续须确认消费方（如某个容器启动时需要 auto-apply）后，
--   再迁入 database/postgres/init-scripts/ 才会随部署自动生效。
--
-- 幂等键语义（避免重复写入、支持重试）：
--   1) trading.decision_evidence.decision_id —— 意图键哈希（32 位 hex），
--      同 account_id + strategy_id + symbol + direction + entry_mode + cycle_id 唯一；
--      重试写入时应用层可用 ON CONFLICT (decision_id) DO UPDATE 覆盖快照、
--      不新增行。score_components / thresholds / planned_qty 等会漂移的字段
--      应由生产者每次完整 UPDATE，覆盖旧值。
--   2) trading.decision_feature_evidence —— UNIQUE NULLS NOT DISTINCT
--      (decision_id, name, source)，同一决策、同一特征名、同一来源唯一；
--      ON CONFLICT 时可更新 value / available_at / quality_flag
--      / fallback_reason 等，避免多进程并发写同一特征导致重复行。
--
-- 注意：表名用 trading.decision_evidence（架构稿最初写的 decision_evidence_tasks
-- 是无意义后缀，笔误，已在 SQL 中改为 decision_evidence）。
--
-- 重要：**所有表的 updated_at 均无触发器**。
--   生产者每次 UPDATE 时必须显式写 `updated_at = now()`，
--   否则该字段将停留在建行时的默认值。
-- ============================================================================

BEGIN;

-- 决策级证据快照表
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

-- 特征级证据快照表（每个决策可有多条特征）
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
    -- PostgreSQL 15+ 语法：NULL 也参与去重，避免 source=NULL 时 UNIQUE 约束失效
    UNIQUE NULLS NOT DISTINCT (decision_id, name, source)
    -- 注：已删除冗余索引 idx_decision_feat_decision，
    -- 其 (decision_id) 前缀列已被上述 UNIQUE 约束隐式覆盖。
);

COMMIT;
