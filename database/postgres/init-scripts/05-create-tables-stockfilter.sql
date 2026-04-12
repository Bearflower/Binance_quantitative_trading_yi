-- =====================================================
-- stockfilter 项目表结构
-- Schema: schema_stockfilter
-- =====================================================

SET search_path TO schema_stockfilter;

-- 股票列表表
CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,
    name TEXT,
    symbol TEXT,
    list_date DATE,
    sector TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- K 线数据表
CREATE TABLE IF NOT EXISTS klines (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL,
    date DATE NOT NULL,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume BIGINT,
    amount DOUBLE PRECISION,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(code, date)
);

-- 筛选结果表
CREATE TABLE IF NOT EXISTS scan_results (
    id BIGSERIAL PRIMARY KEY,
    scan_date DATE NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    score DOUBLE PRECISION,
    surge_date DATE,
    support_level DOUBLE PRECISION,
    current_close DOUBLE PRECISION,
    drop_rate DOUBLE PRECISION,
    min_vol_ratio DOUBLE PRECISION,
    surge_price DOUBLE PRECISION,
    surge_volume_ratio DOUBLE PRECISION,
    surge_pct DOUBLE PRECISION,
    low_after_surge DOUBLE PRECISION,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 持仓记录表
CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    entry_date DATE,
    entry_price DOUBLE PRECISION,
    position_size DOUBLE PRECISION,
    support_level DOUBLE PRECISION,
    stop_loss_price DOUBLE PRECISION,
    status TEXT DEFAULT 'open',
    exit_date DATE,
    exit_price DOUBLE PRECISION,
    pnl DOUBLE PRECISION,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 推送历史记录表
CREATE TABLE IF NOT EXISTS push_history (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL,
    push_date DATE NOT NULL,
    push_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(code, push_date)
);

-- 创建索引
CREATE INDEX idx_klines_code ON klines(code);
CREATE INDEX idx_klines_date ON klines(date);
CREATE INDEX idx_klines_code_date ON klines(code, date);
CREATE INDEX idx_scan_results_scan_date ON scan_results(scan_date);
CREATE INDEX idx_scan_results_code ON scan_results(code);
CREATE INDEX idx_positions_status ON positions(status);
CREATE INDEX idx_push_history_code_date ON push_history(code, push_date);

\echo '✅ stockfilter 表结构创建完成'
