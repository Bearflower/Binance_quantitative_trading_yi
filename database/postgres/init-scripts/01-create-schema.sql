-- ============================================
-- PostgreSQL 数据库 Schema 初始化脚本
-- 创建各个策略的独立 Schema
-- ============================================

-- 创建 btc_eth 策略 Schema
CREATE SCHEMA IF NOT EXISTS btc_eth;

-- 创建 new_coin 策略 Schema
CREATE SCHEMA IF NOT EXISTS new_coin;

-- 创建 grid 策略 Schema
CREATE SCHEMA IF NOT EXISTS grid;

-- 创建 trading 统一交易记录 Schema
CREATE SCHEMA IF NOT EXISTS trading;

-- 授权 trading_user 访问所有 Schema
GRANT ALL PRIVILEGES ON SCHEMA btc_eth TO trading_user;
GRANT ALL PRIVILEGES ON SCHEMA new_coin TO trading_user;
GRANT ALL PRIVILEGES ON SCHEMA grid TO trading_user;
GRANT ALL PRIVILEGES ON SCHEMA trading TO trading_user;

-- 设置默认搜索路径
ALTER USER trading_user SET search_path TO btc_eth, new_coin, grid, trading, public;

-- 输出创建结果
DO $$
BEGIN
    RAISE NOTICE 'Schema 创建完成: btc_eth, new_coin, grid, trading';
END $$;
