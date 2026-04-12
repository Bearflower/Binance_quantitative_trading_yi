-- =====================================================
-- bianace_btcethbnb_trade 项目表结构
-- Schema: schema_bianace
-- =====================================================

SET search_path TO schema_bianace;

-- 交易记录表
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL UNIQUE,
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
    reduce_only BOOLEAN DEFAULT FALSE,
    time_in_force TEXT,
    client_order_id TEXT,
    tp_trigger_price NUMERIC(20, 8),
    tp_price NUMERIC(20, 8),
    sl_trigger_price NUMERIC(20, 8),
    sl_price NUMERIC(20, 8),
    create_time BIGINT NOT NULL,
    update_time BIGINT NOT NULL,
    transaction_id BIGINT,
    order_status TEXT DEFAULT 'PENDING',
    is_partial_fill BOOLEAN DEFAULT FALSE,
    retry_count INTEGER DEFAULT 0,
    timeout_status TEXT,
    remaining_qty NUMERIC(20, 8),
    fill_rate NUMERIC(5, 2),
    poll_duration REAL,
    is_timeout BOOLEAN DEFAULT FALSE,
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
    is_auto_add_margin BOOLEAN DEFAULT FALSE,
    last_update_time BIGINT NOT NULL,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, position_side)
);

-- 资金划转记录表
CREATE TABLE IF NOT EXISTS account_transfers (
    id BIGSERIAL PRIMARY KEY,
    tran_id BIGINT NOT NULL UNIQUE,
    asset TEXT NOT NULL,
    amount NUMERIC(20, 8) NOT NULL,
    type TEXT NOT NULL,
    from_account TEXT NOT NULL,
    to_account TEXT NOT NULL,
    status TEXT NOT NULL,
    create_time BIGINT NOT NULL,
    remark TEXT,
    related_order_id BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 理财赎回记录表
CREATE TABLE IF NOT EXISTS simple_earn_redemptions (
    id BIGSERIAL PRIMARY KEY,
    redeem_id BIGINT NOT NULL,
    product_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    amount NUMERIC(20, 8) NOT NULL,
    redeem_all BOOLEAN DEFAULT FALSE,
    dest_account TEXT NOT NULL,
    success BOOLEAN NOT NULL,
    create_time BIGINT NOT NULL,
    remark TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    tp_reached BOOLEAN DEFAULT FALSE,
    sl_reached BOOLEAN DEFAULT FALSE,
    liquidation_risk TEXT,
    action_taken TEXT,
    remark TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 账户余额快照表
CREATE TABLE IF NOT EXISTS account_balance_snapshot (
    id BIGSERIAL PRIMARY KEY,
    snapshot_time TIMESTAMP NOT NULL,
    account_type TEXT NOT NULL,
    asset TEXT NOT NULL,
    wallet_balance NUMERIC(20, 8),
    available_balance NUMERIC(20, 8),
    unrealized_profit NUMERIC(20, 8),
    total_margin_balance NUMERIC(20, 8),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 平仓记录表
CREATE TABLE IF NOT EXISTS closed_positions (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    position_side TEXT NOT NULL,
    open_price NUMERIC(20, 8) NOT NULL,
    close_price NUMERIC(20, 8) NOT NULL,
    quantity NUMERIC(20, 8) NOT NULL,
    open_time BIGINT NOT NULL,
    close_time BIGINT NOT NULL,
    leverage INTEGER DEFAULT 20,
    gross_pnl NUMERIC(20, 8) NOT NULL,
    commission NUMERIC(20, 8) DEFAULT 0,
    net_pnl NUMERIC(20, 8) NOT NULL,
    pnl_rate NUMERIC(10, 4) NOT NULL,
    close_reason TEXT NOT NULL,
    max_unrealized_profit NUMERIC(20, 8),
    min_unrealized_profit NUMERIC(20, 8),
    duration_seconds INTEGER,
    remark TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 止盈止损触发记录表
CREATE TABLE IF NOT EXISTS tp_sl_triggers (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    position_side TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    trigger_time BIGINT NOT NULL,
    trigger_price NUMERIC(20, 8) NOT NULL,
    target_price NUMERIC(20, 8),
    position_qty NUMERIC(20, 8),
    entry_price NUMERIC(20, 8),
    unrealized_profit NUMERIC(20, 8),
    pnl_rate NUMERIC(10, 4),
    remark TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 交易统计表
CREATE TABLE IF NOT EXISTS trade_statistics (
    id BIGSERIAL PRIMARY KEY,
    period_type TEXT NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    symbol TEXT,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    total_net_pnl NUMERIC(20, 8) DEFAULT 0,
    total_commission NUMERIC(20, 8) DEFAULT 0,
    avg_pnl_rate NUMERIC(10, 4) DEFAULT 0,
    max_pnl_rate NUMERIC(10, 4) DEFAULT 0,
    min_pnl_rate NUMERIC(10, 4) DEFAULT 0,
    win_rate NUMERIC(10, 4) DEFAULT 0,
    profit_loss_ratio NUMERIC(10, 4) DEFAULT 0,
    max_consecutive_wins INTEGER DEFAULT 0,
    max_consecutive_losses INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(period_type, period_start, period_end, symbol)
);

-- 创建索引
CREATE INDEX idx_trades_order_id ON trades(order_id);
CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE INDEX idx_trades_status ON trades(status);
CREATE INDEX idx_trades_create_time ON trades(create_time);
CREATE INDEX idx_positions_symbol ON positions(symbol);
CREATE INDEX idx_positions_update_time ON positions(last_update_time);
CREATE INDEX idx_transfers_tran_id ON account_transfers(tran_id);
CREATE INDEX idx_transfers_create_time ON account_transfers(create_time);
CREATE INDEX idx_monitoring_check_time ON monitoring_logs(check_time);
CREATE INDEX idx_monitoring_symbol ON monitoring_logs(symbol);
CREATE INDEX idx_closed_order_id ON closed_positions(order_id);
CREATE INDEX idx_closed_close_time ON closed_positions(close_time);
CREATE INDEX idx_trigger_order_id ON tp_sl_triggers(order_id);
CREATE INDEX idx_trigger_time ON tp_sl_triggers(trigger_time);
CREATE INDEX idx_stats_period ON trade_statistics(period_type, period_start);

\echo '✅ bianace_btcethbnb_trade 表结构创建完成'
