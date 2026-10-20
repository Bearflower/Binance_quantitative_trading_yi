-- ============================================
-- 研究用（影子仿真/回放）Schema 初始化脚本
-- 所有表仅限 research 前缀；不授予 trading.* 的新增写权限
-- 幂等、自包含、无 BEGIN/COMMIT，与 01–11 脚本同风格
-- ============================================

-- 创建 research Schema
CREATE SCHEMA IF NOT EXISTS research;

-- 授权 trading_user 访问 research Schema
GRANT ALL PRIVILEGES ON SCHEMA research TO trading_user;

-- ============================================
-- 影子运行元数据表（每观察周期一行）
-- ============================================
CREATE TABLE IF NOT EXISTS research.shadow_runs (
    run_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active',
    initial_virtual_equity_usdt NUMERIC(20,8) NOT NULL,
    git_commit TEXT,
    git_sha TEXT,
    config_hashes JSONB,
    data_cutoff_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 索引：按 instance_id + started_at 查历史
CREATE INDEX IF NOT EXISTS idx_shadow_runs_instance_symbol
    ON research.shadow_runs (instance_id, symbol, started_at DESC);

-- ============================================
-- 影子决策事件表（不可变）
-- 唯一键保证调度重试幂等
-- ============================================
CREATE TABLE IF NOT EXISTS research.shadow_decision_events (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES research.shadow_runs(run_id),
    profile_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    decision TEXT NOT NULL,
    direction TEXT,
    grade TEXT,
    score NUMERIC(12,4),
    reference_price NUMERIC(24,8),
    intended_order_type TEXT,
    intended_entry_price NUMERIC(24,8),
    stop_loss_rule TEXT,
    tp1_rule TEXT,
    tp2_rule TEXT,
    trailing_rule TEXT,
    time_stop_rule TEXT,
    feature_snapshot JSONB,
    reason_codes JSONB,
    data_quality_reason TEXT,
    config_hash TEXT NOT NULL,
    git_commit TEXT,
    source_kline_close_times JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, profile_id, symbol, scheduled_at)
);

-- 索引：按 run + profile 查全量决策
CREATE INDEX IF NOT EXISTS idx_shadow_decision_run_profile
    ON research.shadow_decision_events (run_id, profile_id, scheduled_at);

-- 索引：按 decision 类型过滤（candidate / no_signal / analysis_error）
CREATE INDEX IF NOT EXISTS idx_shadow_decision_decision
    ON research.shadow_decision_events (run_id, decision);

-- ============================================
-- 影子回放交易明细表（本地回放器派生，经导入归档）
-- 不是实时影子持仓，由 replay 引擎生成
-- ============================================
CREATE TABLE IF NOT EXISTS research.shadow_replay_trades (
    id BIGSERIAL PRIMARY KEY,
    replay_run_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    decision_event_id BIGINT REFERENCES research.shadow_decision_events(id),
    symbol TEXT,
    direction TEXT,
    entry_time TIMESTAMPTZ,
    entry_price NUMERIC(24,8),
    filled BOOLEAN,
    unfilled_reason TEXT,
    quantity NUMERIC(30,8),
    notional NUMERIC(24,8),
    margin NUMERIC(24,8),
    leverage NUMERIC(8,2),
    stop_loss NUMERIC(24,8),
    tp1 NUMERIC(24,8),
    tp2 NUMERIC(24,8),
    trailing_stop NUMERIC(24,8),
    time_stop_close BOOLEAN DEFAULT FALSE,
    exit_price NUMERIC(24,8),
    exit_time TIMESTAMPTZ,
    exit_reason TEXT,
    realized_pnl NUMERIC(24,8),
    fee NUMERIC(24,8),
    funding_fee NUMERIC(24,8),
    slippage NUMERIC(24,8),
    net_pnl NUMERIC(24,8),
    mae NUMERIC(24,8),
    mfe NUMERIC(24,8),
    holding_duration_seconds INTEGER,
    exit_events JSONB,
    engine_version TEXT,
    input_cutoff_at TIMESTAMPTZ,
    manifest_sha256 TEXT
);

-- 索引：按 replay_run_id + profile_id 查回放结果
CREATE INDEX IF NOT EXISTS idx_shadow_replay_prof
    ON research.shadow_replay_trades (replay_run_id, profile_id);

-- ============================================
-- 权限授予（与 01–11 脚本同口径：SELECT/INSERT/UPDATE/DELETE 全覆盖）
-- trading_user 对 trading.* 的既有权限不被触及；
-- research.* 独立授权，不依赖 search_path
-- ============================================
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA research TO trading_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO trading_user;

-- 未来新建表也自动授权（幂等，重复执行不报错）
ALTER DEFAULT PRIVILEGES IN SCHEMA research
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trading_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA research
    GRANT USAGE, SELECT ON SEQUENCES TO trading_user;

-- 输出创建结果
DO $$
BEGIN
    RAISE NOTICE 'research Schema 创建完成: shadow_runs, shadow_decision_events, shadow_replay_trades';
END $$;
