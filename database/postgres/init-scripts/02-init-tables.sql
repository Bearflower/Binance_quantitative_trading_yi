-- ============================================
-- PostgreSQL 数据库表结构初始化脚本
-- 为各个策略创建基础表
-- ============================================

-- ============================================
-- BTC/ETH 策略表
-- ============================================

-- 交易信号表
CREATE TABLE IF NOT EXISTS btc_eth.trade_signals (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    direction VARCHAR(10) NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    grade CHAR(1) NOT NULL CHECK (grade IN ('S', 'A', 'B', 'C')),
    score DECIMAL(5, 2) NOT NULL,
    entry_price DECIMAL(20, 8) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'executed', 'cancelled')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    executed_at TIMESTAMP,
    metadata JSONB
);

-- 持仓记录表
CREATE TABLE IF NOT EXISTS btc_eth.positions (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    position_id VARCHAR(100) UNIQUE,
    direction VARCHAR(10) NOT NULL,
    quantity DECIMAL(20, 8) NOT NULL,
    entry_price DECIMAL(20, 8) NOT NULL,
    current_price DECIMAL(20, 8),
    unrealized_pnl DECIMAL(20, 8),
    status VARCHAR(20) DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    metadata JSONB
);

-- 交易记录表
CREATE TABLE IF NOT EXISTS btc_eth.trades (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    order_id VARCHAR(100) UNIQUE,
    position_id INTEGER REFERENCES btc_eth.positions(id),
    direction VARCHAR(10) NOT NULL,
    order_type VARCHAR(20) NOT NULL,
    quantity DECIMAL(20, 8) NOT NULL,
    price DECIMAL(20, 8) NOT NULL,
    commission DECIMAL(20, 8),
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB
);

-- 创建索引
CREATE INDEX idx_btc_eth_signals_symbol ON btc_eth.trade_signals(symbol);
CREATE INDEX idx_btc_eth_signals_created ON btc_eth.trade_signals(created_at);
CREATE INDEX idx_btc_eth_positions_symbol ON btc_eth.positions(symbol);
CREATE INDEX idx_btc_eth_positions_status ON btc_eth.positions(status);
CREATE INDEX idx_btc_eth_trades_symbol ON btc_eth.trades(symbol);
CREATE INDEX idx_btc_eth_trades_executed ON btc_eth.trades(executed_at);

-- ============================================
-- 新币做空策略表
-- ============================================

-- 新币监控表
CREATE TABLE IF NOT EXISTS new_coin.coin_monitor (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL UNIQUE,
    listing_time TIMESTAMP NOT NULL,
    initial_price DECIMAL(20, 8),
    current_price DECIMAL(20, 8),
    price_change_pct DECIMAL(10, 4),
    volume_24h DECIMAL(30, 8),
    monitoring_started TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    monitoring_ended TIMESTAMP,
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'completed', 'failed')),
    metadata JSONB
);

-- 做空信号表
CREATE TABLE IF NOT EXISTS new_coin.short_signals (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    pattern_type VARCHAR(50) NOT NULL,
    confidence DECIMAL(5, 2) NOT NULL,
    entry_price DECIMAL(20, 8) NOT NULL,
    stop_loss DECIMAL(20, 8),
    take_profit DECIMAL(20, 8),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'executed', 'cancelled')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    executed_at TIMESTAMP,
    metadata JSONB
);

-- 做空持仓表
CREATE TABLE IF NOT EXISTS new_coin.short_positions (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    position_id VARCHAR(100) UNIQUE,
    quantity DECIMAL(20, 8) NOT NULL,
    entry_price DECIMAL(20, 8) NOT NULL,
    current_price DECIMAL(20, 8),
    unrealized_pnl DECIMAL(20, 8),
    liquidation_price DECIMAL(20, 8),
    status VARCHAR(20) DEFAULT 'open' CHECK (status IN ('open', 'closed', 'liquidated')),
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    metadata JSONB
);

-- 订单记录表
CREATE TABLE IF NOT EXISTS new_coin.orders (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(100) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    strategy VARCHAR(50) NOT NULL DEFAULT 'new_coin',
    side VARCHAR(10) NOT NULL CHECK (side IN ('BUY', 'SELL')),
    type VARCHAR(20) NOT NULL DEFAULT 'MARKET',
    quantity NUMERIC(20, 8) NOT NULL,
    price NUMERIC(20, 8) DEFAULT 0,
    status VARCHAR(20),
    score NUMERIC(10, 4),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建索引
CREATE INDEX idx_new_coin_monitor_symbol ON new_coin.coin_monitor(symbol);
CREATE INDEX idx_new_coin_monitor_status ON new_coin.coin_monitor(status);
CREATE INDEX idx_new_coin_short_signals_symbol ON new_coin.short_signals(symbol);
CREATE INDEX idx_new_coin_short_positions_symbol ON new_coin.short_positions(symbol);
CREATE INDEX idx_new_coin_orders_symbol ON new_coin.orders(symbol);
CREATE INDEX idx_new_coin_orders_created ON new_coin.orders(created_at);
CREATE INDEX idx_new_coin_orders_strategy ON new_coin.orders(strategy);

-- ============================================
-- 网格交易策略表
-- ============================================

-- 网格配置表
CREATE TABLE IF NOT EXISTS grid.grid_config (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    grid_type VARCHAR(20) NOT NULL CHECK (grid_type IN ('arithmetic', 'geometric')),
    upper_price DECIMAL(20, 8) NOT NULL,
    lower_price DECIMAL(20, 8) NOT NULL,
    grid_count INTEGER NOT NULL,
    grid_spacing DECIMAL(20, 8),
    total_investment DECIMAL(20, 8) NOT NULL,
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'paused', 'stopped')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB
);

-- 网格订单表
CREATE TABLE IF NOT EXISTS grid.grid_orders (
    id SERIAL PRIMARY KEY,
    config_id INTEGER REFERENCES grid.grid_config(id),
    grid_level INTEGER NOT NULL,
    buy_price DECIMAL(20, 8) NOT NULL,
    sell_price DECIMAL(20, 8) NOT NULL,
    quantity DECIMAL(20, 8) NOT NULL,
    buy_order_id VARCHAR(100),
    sell_order_id VARCHAR(100),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'buy_filled', 'sell_filled', 'completed')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB
);

-- 网格交易记录表
CREATE TABLE IF NOT EXISTS grid.grid_trades (
    id SERIAL PRIMARY KEY,
    config_id INTEGER REFERENCES grid.grid_config(id),
    grid_level INTEGER NOT NULL,
    order_id VARCHAR(100) UNIQUE,
    side VARCHAR(10) NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity DECIMAL(20, 8) NOT NULL,
    price DECIMAL(20, 8) NOT NULL,
    profit DECIMAL(20, 8),
    commission DECIMAL(20, 8),
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB
);

-- 创建索引
CREATE INDEX idx_grid_config_symbol ON grid.grid_config(symbol);
CREATE INDEX idx_grid_config_status ON grid.grid_config(status);
CREATE INDEX idx_grid_orders_config ON grid.grid_orders(config_id);
CREATE INDEX idx_grid_orders_status ON grid.grid_orders(status);
CREATE INDEX idx_grid_trades_config ON grid.grid_trades(config_id);
CREATE INDEX idx_grid_trades_executed ON grid.grid_trades(executed_at);

-- ============================================
-- 公共表
-- ============================================

-- 策略状态表
CREATE TABLE IF NOT EXISTS public.strategy_status (
    id SERIAL PRIMARY KEY,
    strategy_name VARCHAR(50) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL CHECK (status IN ('running', 'stopped', 'error')),
    last_run TIMESTAMP,
    next_run TIMESTAMP,
    error_message TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 策略状态数据表（用于存储策略的状态数据，如已知币种列表）
CREATE TABLE IF NOT EXISTS public.strategy_states (
    id SERIAL PRIMARY KEY,
    strategy_name VARCHAR(50) NOT NULL,
    state_key VARCHAR(100) NOT NULL,
    state_data JSONB NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(strategy_name, state_key)
);

-- 系统配置表
CREATE TABLE IF NOT EXISTS public.system_config (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) NOT NULL UNIQUE,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 插入初始策略状态
INSERT INTO public.strategy_status (strategy_name, status) VALUES
    ('btc_eth', 'stopped'),
    ('new_coin', 'stopped'),
    ('grid', 'stopped')
ON CONFLICT (strategy_name) DO NOTHING;

-- 输出创建结果
DO $$
BEGIN
    RAISE NOTICE '表结构初始化完成';
END $$;
