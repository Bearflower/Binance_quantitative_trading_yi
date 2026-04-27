-- 网格交易信号灯系统 V2.0 数据库初始化脚本
-- 创建时间：2026-04-23

-- ==================== 创建 Schema ====================
CREATE SCHEMA IF NOT EXISTS grid_signal;

-- 设置默认搜索路径
SET search_path TO grid_signal, public;

-- ==================== 1. 信号推送历史表 ====================
CREATE TABLE IF NOT EXISTS grid_signals (
    id SERIAL PRIMARY KEY,
    signal_time TIMESTAMP NOT NULL,
    market_state VARCHAR(20) NOT NULL,  -- ranging, uptrend, downtrend, strong_trend
    symbol VARCHAR(20) NOT NULL,
    grid_params JSONB NOT NULL,  -- 网格参数（JSON 格式）
    is_pushed BOOLEAN DEFAULT FALSE,
    pushed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- 索引
    INDEX idx_signal_time (signal_time),
    INDEX idx_symbol_state (symbol, market_state)
);

COMMENT ON TABLE grid_signals IS '信号推送历史记录';
COMMENT ON COLUMN grid_signals.signal_time IS '信号生成时间';
COMMENT ON COLUMN grid_signals.market_state IS '市场状态：ranging/uptrend/downtrend/strong_trend';
COMMENT ON COLUMN grid_signals.grid_params IS '网格参数（JSON 格式）';
COMMENT ON COLUMN grid_signals.is_pushed IS '是否已推送';

-- ==================== 2. 市场状态历史表 ====================
CREATE TABLE IF NOT EXISTS market_states (
    id SERIAL PRIMARY KEY,
    check_time TIMESTAMP NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    state VARCHAR(20) NOT NULL,  -- ranging, uptrend, downtrend, strong_trend
    adx DECIMAL(5, 2),  -- 1H ADX 值
    adx_4h DECIMAL(5, 2),  -- 4H ADX 值
    ema_fast DECIMAL(12, 2),  -- 快线 EMA
    ema_slow DECIMAL(12, 2),  -- 慢线 EMA
    trend_strength DECIMAL(5, 4),  -- 趋势强度系数 (0-0.5)
    confidence DECIMAL(5, 4),  -- 置信度 (0-1)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- 索引
    INDEX idx_check_time (check_time),
    INDEX idx_symbol (symbol)
);

COMMENT ON TABLE market_states IS '市场状态历史记录';
COMMENT ON COLUMN market_states.check_time IS '检查时间';
COMMENT ON COLUMN market_states.state IS '市场状态';
COMMENT ON COLUMN market_states.trend_strength IS '趋势强度系数 k = min(0.5, max(0, (ADX-25)/30))';

-- ==================== 3. 网格参数历史表 ====================
CREATE TABLE IF NOT EXISTS grid_parameters (
    id SERIAL PRIMARY KEY,
    create_time TIMESTAMP NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    market_state VARCHAR(20) NOT NULL,
    
    -- 网格基本参数
    upper_price DECIMAL(12, 2) NOT NULL,  -- 上边界
    lower_price DECIMAL(12, 2) NOT NULL,  -- 下边界
    grid_count INTEGER NOT NULL,  -- 网格数量
    grid_type VARCHAR(20) NOT NULL,  -- arithmetic, geometric
    grid_direction VARCHAR(20) NOT NULL,  -- LONG, SHORT, NEUTRAL
    
    -- 资金配置
    leverage INTEGER NOT NULL,  -- 杠杆倍数
    total_investment DECIMAL(12, 2) NOT NULL,  -- 总投资金额
    
    -- 止盈止损参数
    stop_upper_price DECIMAL(12, 2),  -- 停止上移价格
    stop_lower_price DECIMAL(12, 2),  -- 停止下移价格
    terminate_upper_price DECIMAL(12, 2) NOT NULL,  -- 终止最高价格
    terminate_lower_price DECIMAL(12, 2) NOT NULL,  -- 终止最低价格
    
    -- 技术指标
    atr_value DECIMAL(12, 2) NOT NULL,  -- ATR 值
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- 索引
    INDEX idx_create_time (create_time),
    INDEX idx_symbol (symbol)
);

COMMENT ON TABLE grid_parameters IS '网格参数历史记录';
COMMENT ON COLUMN grid_parameters.grid_type IS '网格类型：arithmetic(等差) / geometric(等比)';
COMMENT ON COLUMN grid_parameters.grid_direction IS '网格方向：LONG(做多) / SHORT(做空) / NEUTRAL(中性)';

-- ==================== 4. 触发事件记录表 ====================
CREATE TABLE IF NOT EXISTS trigger_events (
    id SERIAL PRIMARY KEY,
    trigger_time TIMESTAMP NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    trigger_type VARCHAR(50) NOT NULL,  -- GRID_WIDTH_CHANGE, GRID_COUNT_CHANGE, ATR_CHANGE, etc.
    description TEXT NOT NULL,
    severity DECIMAL(3, 2),  -- 严重程度 (0-1)
    details JSONB,  -- 详细信息
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- 索引
    INDEX idx_trigger_time (trigger_time),
    INDEX idx_symbol_type (symbol, trigger_type)
);

COMMENT ON TABLE trigger_events IS '触发事件记录';
COMMENT ON COLUMN trigger_events.trigger_type IS '触发类型';
COMMENT ON COLUMN trigger_events.severity IS '严重程度 (0-1)';

-- ==================== 5. 推送记录表 ====================
CREATE TABLE IF NOT EXISTS notification_logs (
    id SERIAL PRIMARY KEY,
    push_time TIMESTAMP NOT NULL,
    signal_id INTEGER REFERENCES grid_signals(id),
    notification_type VARCHAR(20) NOT NULL,  -- feishu, dingding, telegram
    status VARCHAR(20) NOT NULL,  -- success, failed
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- 索引
    INDEX idx_push_time (push_time),
    INDEX idx_status (status)
);

COMMENT ON TABLE notification_logs IS '推送记录';
COMMENT ON COLUMN notification_logs.status IS '推送状态：success / failed';

-- ==================== 创建视图 ====================

-- 最新市场状态视图
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

-- 最新网格参数视图
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

-- ==================== 插入测试数据 ====================

-- 插入测试市场状态
INSERT INTO market_states (check_time, symbol, state, adx, adx_4h, trend_strength, confidence)
VALUES 
    (NOW() - INTERVAL '2 hours', 'BTCUSDT', 'ranging', 18.5, 19.2, 0.0, 0.6),
    (NOW() - INTERVAL '1 hour', 'BTCUSDT', 'uptrend', 28.3, 27.5, 0.11, 0.75),
    (NOW(), 'BTCUSDT', 'uptrend', 30.1, 29.8, 0.17, 0.82);

-- 插入测试网格参数
INSERT INTO grid_parameters (
    create_time, symbol, market_state,
    upper_price, lower_price, grid_count, grid_type, grid_direction,
    leverage, total_investment,
    stop_upper_price, stop_lower_price,
    terminate_upper_price, terminate_lower_price,
    atr_value
) VALUES 
    (
        NOW() - INTERVAL '2 hours', 'BTCUSDT', 'ranging',
        71000.00, 68000.00, 30, 'arithmetic', 'NEUTRAL',
        10, 500.00,
        NULL, NULL,
        72500.00, 66500.00,
        750.00
    ),
    (
        NOW(), 'BTCUSDT', 'uptrend',
        73500.00, 68500.00, 30, 'arithmetic', 'LONG',
        10, 500.00,
        74250.00, NULL,
        75000.00, 67000.00,
        800.00
    );

-- ==================== 授权 ====================
-- 如果需要创建专用用户，取消以下注释
-- CREATE USER grid_user WITH PASSWORD 'your_password_here';
-- GRANT ALL PRIVILEGES ON SCHEMA grid_signal TO grid_user;
-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA grid_signal TO grid_user;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA grid_signal TO grid_user;

-- ==================== 完成提示 ====================
SELECT 'Database initialization completed successfully!' AS status;
