-- =====================================================
-- bianace_newtrade_trade (short_selling) 项目表结构
-- Schema: schema_short_selling
-- =====================================================

SET search_path TO schema_short_selling;

-- 信号记录表
CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    signal_id TEXT UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    entry_price DOUBLE PRECISION,
    stop_loss_price DOUBLE PRECISION,
    take_profit_price DOUBLE PRECISION,
    position_size DOUBLE PRECISION,
    leverage INTEGER,
    status TEXT DEFAULT 'PENDING',
    expire_time TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 交易记录表
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL UNIQUE,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    position_side TEXT NOT NULL,
    type TEXT NOT NULL,
    quantity NUMERIC(20, 8) NOT NULL,
    price NUMERIC(20, 8),
    avg_price NUMERIC(20, 8),
    executed_qty NUMERIC(20, 8),
    cum_quote NUMERIC(20, 8),
    status TEXT NOT NULL,
    create_time BIGINT NOT NULL,
    update_time BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 持仓记录表
CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    position_side TEXT NOT NULL,
    position_amt NUMERIC(20, 8) NOT NULL,
    entry_price NUMERIC(20, 8) NOT NULL,
    mark_price NUMERIC(20, 8),
    unrealized_profit NUMERIC(20, 8),
    leverage INTEGER NOT NULL,
    liquidation_price NUMERIC(20, 8),
    margin_type TEXT NOT NULL,
    last_update_time BIGINT NOT NULL,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, position_side)
);

-- 监控日志表
CREATE TABLE IF NOT EXISTS monitoring_logs (
    id BIGSERIAL PRIMARY KEY,
    check_time TIMESTAMP NOT NULL,
    symbol TEXT NOT NULL,
    position_side TEXT,
    position_amt NUMERIC(20, 8),
    entry_price NUMERIC(20, 8),
    current_price NUMERIC(20, 8),
    unrealized_profit NUMERIC(20, 8),
    unrealized_profit_rate NUMERIC(10, 4),
    action_taken TEXT,
    remark TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 风控事件表
CREATE TABLE IF NOT EXISTS risk_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    symbol TEXT,
    trigger_price NUMERIC(20, 8),
    trigger_value NUMERIC(20, 8),
    action_taken TEXT,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建索引
CREATE INDEX idx_signals_status ON signals(status);
CREATE INDEX idx_signals_score ON signals(score);
CREATE INDEX idx_trades_order_id ON trades(order_id);
CREATE INDEX idx_trades_signal_id ON trades(signal_id);
CREATE INDEX idx_positions_symbol ON positions(symbol);
CREATE INDEX idx_monitoring_check_time ON monitoring_logs(check_time);
CREATE INDEX idx_risk_events_type ON risk_events(event_type);

\echo '✅ bianace_newtrade_trade 表结构创建完成'
