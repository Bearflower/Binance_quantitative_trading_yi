-- =====================================================
-- Grid_Trading 项目表结构
-- Schema: schema_grid
-- =====================================================

SET search_path TO schema_grid;

-- 交易记录表
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    trade_id TEXT UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    fee DOUBLE PRECISION,
    fee_asset TEXT,
    timestamp TIMESTAMP NOT NULL,
    grid_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 网格历史表
CREATE TABLE IF NOT EXISTS grid_history (
    id BIGSERIAL PRIMARY KEY,
    grid_id TEXT UNIQUE,
    symbol TEXT NOT NULL,
    upper_price DOUBLE PRECISION NOT NULL,
    lower_price DOUBLE PRECISION NOT NULL,
    grid_count INTEGER NOT NULL,
    investment DOUBLE PRECISION NOT NULL,
    state TEXT NOT NULL,
    market_state TEXT,
    created_at TIMESTAMP NOT NULL,
    terminated_at TIMESTAMP,
    pnl DOUBLE PRECISION
);

-- 系统状态表
CREATE TABLE IF NOT EXISTS system_status (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL,
    market_state TEXT NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    atr DOUBLE PRECISION NOT NULL,
    adx DOUBLE PRECISION NOT NULL,
    ema_fast DOUBLE PRECISION NOT NULL,
    ema_slow DOUBLE PRECISION NOT NULL,
    total_pnl DOUBLE PRECISION,
    account_balance DOUBLE PRECISION
);

-- 风险事件表
CREATE TABLE IF NOT EXISTS risk_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    trigger_price DOUBLE PRECISION,
    trigger_pnl DOUBLE PRECISION,
    action_taken TEXT,
    timestamp TIMESTAMP NOT NULL,
    details TEXT
);

-- 网格参数调整历史表
CREATE TABLE IF NOT EXISTS grid_parameter_adjustments (
    id BIGSERIAL PRIMARY KEY,
    grid_id TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    parameter_name TEXT NOT NULL,
    old_value DOUBLE PRECISION,
    new_value DOUBLE PRECISION,
    trigger_reason TEXT,
    market_state TEXT,
    atr_value DOUBLE PRECISION,
    details TEXT,
    adjustment_type TEXT DEFAULT 'SWITCH'
);

-- 移动止盈状态表
CREATE TABLE IF NOT EXISTS trailing_profit_state (
    id BIGSERIAL PRIMARY KEY,
    grid_id TEXT NOT NULL,
    activated_at TIMESTAMP,
    peak_price DOUBLE PRECISION,
    peak_pnl_percent DOUBLE PRECISION,
    current_stop_price DOUBLE PRECISION,
    last_updated TIMESTAMP NOT NULL,
    UNIQUE(grid_id)
);

-- 创建索引
CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE INDEX idx_trades_timestamp ON trades(timestamp);
CREATE INDEX idx_grid_history_state ON grid_history(state);
CREATE INDEX idx_system_status_timestamp ON system_status(timestamp);
CREATE INDEX idx_risk_events_type ON risk_events(event_type);
CREATE INDEX idx_grid_adjustments_grid_id ON grid_parameter_adjustments(grid_id);
CREATE INDEX idx_trailing_profit_grid_id ON trailing_profit_state(grid_id);

\echo '✅ Grid_Trading 表结构创建完成'
