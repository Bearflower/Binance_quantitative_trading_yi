-- ============================================
-- 统一基础设施服务 - 数据库初始化脚本
-- ============================================

-- 创建扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================
-- K 线数据表（分区表）
-- ============================================

CREATE TABLE IF NOT EXISTS klines (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,          -- 币种：BTCUSDT
    interval VARCHAR(10) NOT NULL,         -- 周期：15m, 1h, 4h, 1d
    open_time TIMESTAMP WITH TIME ZONE NOT NULL,          -- 开盘时间
    close_time TIMESTAMP WITH TIME ZONE NOT NULL,         -- 收盘时间
    open NUMERIC(20, 8) NOT NULL,          -- 开盘价
    high NUMERIC(20, 8) NOT NULL,          -- 最高价
    low NUMERIC(20, 8) NOT NULL,           -- 最低价
    close NUMERIC(20, 8) NOT NULL,         -- 收盘价
    volume NUMERIC(30, 8) NOT NULL,        -- 成交量
    quote_volume NUMERIC(30, 8) NOT NULL,  -- 成交额
    trades_count INTEGER NOT NULL,         -- 交易笔数
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),    -- 入库时间
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),    -- 更新时间
    
    -- 唯一索引：防止重复数据
    CONSTRAINT unique_kline UNIQUE(symbol, interval, open_time)
) PARTITION BY LIST (symbol, interval);

-- 创建注释
COMMENT ON TABLE klines IS 'K 线数据主表（分区表）';
COMMENT ON COLUMN klines.symbol IS '交易对符号，如 BTCUSDT';
COMMENT ON COLUMN klines.interval IS 'K 线周期，如 15m, 1h, 4h, 1d';
COMMENT ON COLUMN klines.open_time IS '开盘时间（UTC）';
COMMENT ON COLUMN klines.close_time IS '收盘时间（UTC）';

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_klines_time ON klines (open_time DESC);
CREATE INDEX IF NOT EXISTS idx_klines_symbol_interval ON klines (symbol, interval);

-- ============================================
-- 创建分区表示例（按需添加）
-- ============================================

-- BTCUSDT 分区
CREATE TABLE IF NOT EXISTS klines_btcusdt_15m PARTITION OF klines
    FOR VALUES IN ('BTCUSDT', '15m');

CREATE TABLE IF NOT EXISTS klines_btcusdt_1h PARTITION OF klines
    FOR VALUES IN ('BTCUSDT', '1h');

CREATE TABLE IF NOT EXISTS klines_btcusdt_4h PARTITION OF klines
    FOR VALUES IN ('BTCUSDT', '4h');

CREATE TABLE IF NOT EXISTS klines_btcusdt_1d PARTITION OF klines
    FOR VALUES IN ('BTCUSDT', '1d');

-- ETHUSDT 分区
CREATE TABLE IF NOT EXISTS klines_ethusdt_15m PARTITION OF klines
    FOR VALUES IN ('ETHUSDT', '15m');

CREATE TABLE IF NOT EXISTS klines_ethusdt_1h PARTITION OF klines
    FOR VALUES IN ('ETHUSDT', '1h');

CREATE TABLE IF NOT EXISTS klines_ethusdt_4h PARTITION OF klines
    FOR VALUES IN ('ETHUSDT', '4h');

CREATE TABLE IF NOT EXISTS klines_ethusdt_1d PARTITION OF klines
    FOR VALUES IN ('ETHUSDT', '1d');

-- BNBUSDT 分区
CREATE TABLE IF NOT EXISTS klines_bnbusdt_15m PARTITION OF klines
    FOR VALUES IN ('BNBUSDT', '15m');

CREATE TABLE IF NOT EXISTS klines_bnbusdt_1h PARTITION OF klines
    FOR VALUES IN ('BNBUSDT', '1h');

CREATE TABLE IF NOT EXISTS klines_bnbusdt_4h PARTITION OF klines
    FOR VALUES IN ('BNBUSDT', '4h');

CREATE TABLE IF NOT EXISTS klines_bnbusdt_1d PARTITION OF klines
    FOR VALUES IN ('BNBUSDT', '1d');

-- ============================================
-- 技术指标表
-- ============================================

CREATE TABLE IF NOT EXISTS indicators (
    id BIGSERIAL PRIMARY KEY,
    kline_id BIGINT NOT NULL REFERENCES klines(id) ON DELETE CASCADE,
    symbol VARCHAR(20) NOT NULL,
    interval VARCHAR(10) NOT NULL,
    indicator_type VARCHAR(20) NOT NULL,  -- MA, EMA, RSI, MACD, ATR, BOLL
    indicator_name VARCHAR(50) NOT NULL,  -- MA5, MA10, RSI14
    indicator_value NUMERIC(30, 8) NOT NULL,
    period INTEGER,                        -- 周期参数
    calculated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- 唯一约束
    CONSTRAINT unique_indicator UNIQUE(kline_id, indicator_type, indicator_name)
);

-- 创建注释
COMMENT ON TABLE indicators IS '技术指标数据表';
COMMENT ON COLUMN indicators.kline_id IS '关联的 K 线 ID';
COMMENT ON COLUMN indicators.indicator_type IS '指标类型：MA, EMA, RSI, MACD, ATR, BOLL';
COMMENT ON COLUMN indicators.indicator_name IS '指标名称：MA5, RSI14 等';

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_indicators_kline ON indicators (kline_id);
CREATE INDEX IF NOT EXISTS idx_indicators_type ON indicators (indicator_type);
CREATE INDEX IF NOT EXISTS idx_indicators_symbol_interval ON indicators (symbol, interval);

-- ============================================
-- 数据清理函数（30 天前数据）
-- ============================================

CREATE OR REPLACE FUNCTION cleanup_old_klines()
RETURNS VOID AS $$
BEGIN
    -- 删除 30 天前的数据（做空系统）
    DELETE FROM klines
    WHERE open_time < NOW() - INTERVAL '30 days'
    AND symbol IN (
        SELECT DISTINCT symbol 
        FROM klines 
        WHERE open_time < NOW() - INTERVAL '30 days'
    );
    
    RAISE NOTICE '已清理 30 天前的 K 线数据';
END;
$$ LANGUAGE plpgsql;

-- ============================================
-- 数据清理函数（180 天前数据）
-- ============================================

CREATE OR REPLACE FUNCTION cleanup_old_klines_long()
RETURNS VOID AS $$
BEGIN
    -- 删除 180 天前的数据（其他系统）
    DELETE FROM klines
    WHERE open_time < NOW() - INTERVAL '180 days';
    
    RAISE NOTICE '已清理 180 天前的 K 线数据';
END;
$$ LANGUAGE plpgsql;

-- ============================================
-- 统计信息视图
-- ============================================

CREATE OR REPLACE VIEW klines_stats AS
SELECT 
    symbol,
    interval,
    COUNT(*) as total_klines,
    MIN(open_time) as earliest_time,
    MAX(open_time) as latest_time,
    MAX(created_at) as last_updated
FROM klines
GROUP BY symbol, interval
ORDER BY symbol, interval;

COMMENT ON VIEW klines_stats IS 'K 线数据统计信息';

-- ============================================
-- 插入测试数据（可选，用于开发环境）
-- ============================================

-- 开发环境可以取消注释插入测试数据
-- INSERT INTO klines (symbol, interval, open_time, close_time, open, high, low, close, volume, quote_volume, trades_count)
-- VALUES 
--     ('BTCUSDT', '1h', NOW() - INTERVAL '1 hour', NOW(), 95000, 95500, 94800, 95200, 1234.56, 117654321, 5678)
-- ON CONFLICT (symbol, interval, open_time) DO NOTHING;

-- ============================================
-- 完成提示
-- ============================================

SELECT '数据库初始化完成！' as status;
