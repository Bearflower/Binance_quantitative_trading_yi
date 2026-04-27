-- 网格交易信号灯系统 V2.0 数据库初始化脚本（修正版）
-- 创建时间：2026-04-24

-- ==================== 1. 信号推送历史表 ====================
CREATE TABLE IF NOT EXISTS grid_signals (
    id SERIAL PRIMARY KEY,
    signal_time TIMESTAMP NOT NULL,
    market_state VARCHAR(20) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    grid_params JSONB NOT NULL,
    is_pushed BOOLEAN DEFAULT FALSE,
    pushed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_signal_time ON grid_signals(signal_time);
CREATE INDEX IF NOT EXISTS idx_symbol_state ON grid_signals(symbol, market_state);

COMMENT ON TABLE grid_signals IS '信号推送历史记录';

-- ==================== 2. 市场状态历史表 ====================
CREATE TABLE IF NOT EXISTS market_states (
    id SERIAL PRIMARY KEY,
    check_time TIMESTAMP NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    state VARCHAR(20) NOT NULL,
    adx DECIMAL(5, 2),
    adx_4h DECIMAL(5, 2),
    ema_fast DECIMAL(12, 2),
    ema_slow DECIMAL(12, 2),
    trend_strength DECIMAL(5, 4),
    confidence DECIMAL(5, 4),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_check_time ON market_states(check_time);
CREATE INDEX IF NOT EXISTS idx_symbol ON market_states(symbol);

COMMENT ON TABLE market_states IS '市场状态历史记录';

-- ==================== 3. 网格参数历史表 ====================
CREATE TABLE IF NOT EXISTS grid_parameters (
    id SERIAL PRIMARY KEY,
    create_time TIMESTAMP NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    market_state VARCHAR(20) NOT NULL,
    upper_price DECIMAL(12, 2) NOT NULL,
    lower_price DECIMAL(12, 2) NOT NULL,
    grid_count INTEGER NOT NULL,
    grid_type VARCHAR(20) NOT NULL,
    grid_direction VARCHAR(20) NOT NULL,
    leverage INTEGER NOT NULL,
    total_investment DECIMAL(12, 2) NOT NULL,
    stop_upper_price DECIMAL(12, 2),
    stop_lower_price DECIMAL(12, 2),
    terminate_upper_price DECIMAL(12, 2) NOT NULL,
    terminate_lower_price DECIMAL(12, 2) NOT NULL,
    atr_value DECIMAL(12, 2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_create_time ON grid_parameters(create_time);
CREATE INDEX IF NOT EXISTS idx_symbol ON grid_parameters(symbol);

COMMENT ON TABLE grid_parameters IS '网格参数历史记录';

-- ==================== 4. 触发事件记录表 ====================
CREATE TABLE IF NOT EXISTS trigger_events (
    id SERIAL PRIMARY KEY,
    trigger_time TIMESTAMP NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    trigger_type VARCHAR(50) NOT NULL,
    description TEXT NOT NULL,
    severity DECIMAL(3, 2),
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trigger_time ON trigger_events(trigger_time);
CREATE INDEX IF NOT EXISTS idx_symbol_type ON trigger_events(symbol, trigger_type);

COMMENT ON TABLE trigger_events IS '触发事件记录';

-- ==================== 5. 推送记录表 ====================
CREATE TABLE IF NOT EXISTS notification_logs (
    id SERIAL PRIMARY KEY,
    push_time TIMESTAMP NOT NULL,
    signal_id INTEGER REFERENCES grid_signals(id),
    notification_type VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_push_time ON notification_logs(push_time);
CREATE INDEX IF NOT EXISTS idx_status ON notification_logs(status);

COMMENT ON TABLE notification_logs IS '推送记录';

-- ==================== 创建视图 ====================

CREATE OR REPLACE VIEW v_latest_market_state AS
SELECT 
    symbol,
    state,
    adx,
    adx_4h,
    trend_strength,
    confidence,
    check_time
FROM market_states
WHERE check_time = (
    SELECT MAX(check_time) FROM market_states ms2 
    WHERE ms2.symbol = market_states.symbol
);

CREATE OR REPLACE VIEW v_latest_grid_params AS
SELECT 
    symbol,
    market_state,
    upper_price,
    lower_price,
    grid_count,
    grid_type,
    grid_direction,
    leverage,
    total_investment,
    atr_value,
    create_time
FROM grid_parameters
WHERE create_time = (
    SELECT MAX(create_time) FROM grid_parameters gp2 
    WHERE gp2.symbol = grid_parameters.symbol
);

-- ==================== 完成提示 ====================
SELECT 'Database initialization completed successfully!' AS status;
