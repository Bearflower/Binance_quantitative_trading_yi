-- ============================================
-- 统一交易记录表初始化脚本
-- 所有策略的交易记录统一存储在 trading Schema
-- ============================================

-- 创建 trading Schema（如果不存在）
CREATE SCHEMA IF NOT EXISTS trading;

-- 授权 trading_user 访问 trading Schema
GRANT ALL PRIVILEGES ON SCHEMA trading TO trading_user;

-- ============================================
-- 统一交易记录表
-- 所有策略的每笔成交订单都记录在此表中
-- executed_at 存储的是北京时间（UTC+8）
-- ============================================
CREATE TABLE IF NOT EXISTS trading.trade_records (
    id SERIAL PRIMARY KEY,
    strategy VARCHAR(50) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    order_id VARCHAR(100),
    side VARCHAR(10) NOT NULL,
    order_type VARCHAR(20) NOT NULL,
    quantity DECIMAL(20,8) NOT NULL DEFAULT 0,
    price DECIMAL(20,8) NOT NULL DEFAULT 0,
    commission DECIMAL(20,8) NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'NEW',
    executed_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- 策略+日期联合索引（日报按策略按日期查询）
CREATE INDEX IF NOT EXISTS idx_trade_records_strategy_date 
    ON trading.trade_records(strategy, executed_at);

-- 日期索引（日报按日期跨策略汇总）
CREATE INDEX IF NOT EXISTS idx_trade_records_date 
    ON trading.trade_records(executed_at);

-- 输出创建结果
DO $$
BEGIN
    RAISE NOTICE '统一交易记录表创建完成: trading.trade_records';
END $$;