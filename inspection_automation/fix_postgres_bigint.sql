-- ============================================
-- PostgreSQL BigInt Out of Range 错误修复脚本
-- ============================================

-- 问题：trades 表的 create_time 和 update_time 字段类型为 INTEGER
--      但存储的是毫秒时间戳（约 1.7 万亿），超出 INTEGER 范围（±21 亿）
-- 解决：将字段类型改为 BIGINT

-- ============================================
-- 第一步：检查当前字段类型
-- ============================================

SELECT 
    column_name,
    data_type,
    udt_name,
    numeric_precision
FROM information_schema.columns
WHERE table_schema = 'schema_bianace'
    AND table_name = 'trades'
    AND column_name IN ('create_time', 'update_time', 'transaction_id', 'order_id')
ORDER BY ordinal_position;

-- ============================================
-- 第二步：修复 trades 表的字段类型
-- ============================================

-- 修改 create_time 为 BIGINT
ALTER TABLE schema_bianace.trades 
    ALTER COLUMN create_time TYPE BIGINT;

-- 修改 update_time 为 BIGINT
ALTER TABLE schema_bianace.trades 
    ALTER COLUMN update_time TYPE BIGINT;

-- 修改 transaction_id 为 BIGINT（如果有）
ALTER TABLE schema_bianace.trades 
    ALTER COLUMN transaction_id TYPE BIGINT;

-- ============================================
-- 第三步：修复其他表的类似问题
-- ============================================

-- 修复 positions 表
ALTER TABLE schema_bianace.positions 
    ALTER COLUMN last_update_time TYPE BIGINT;

-- 修复 account_transfers 表
ALTER TABLE schema_bianace.account_transfers 
    ALTER COLUMN create_time TYPE BIGINT;
ALTER TABLE schema_bianace.account_transfers 
    ALTER COLUMN tran_id TYPE BIGINT;

-- 修复 closed_positions 表
ALTER TABLE schema_bianace.closed_positions 
    ALTER COLUMN open_time TYPE BIGINT;
ALTER TABLE schema_bianace.closed_positions 
    ALTER COLUMN close_time TYPE BIGINT;

-- 修复 tp_sl_triggers 表
ALTER TABLE schema_bianace.tp_sl_triggers 
    ALTER COLUMN trigger_time TYPE BIGINT;

-- 修复 monitoring_logs 表（如果有时间戳字段）
ALTER TABLE schema_bianace.monitoring_logs 
    ALTER COLUMN check_time TYPE TIMESTAMP;

-- ============================================
-- 第四步：验证修复结果
-- ============================================

SELECT 
    column_name,
    data_type,
    udt_name
FROM information_schema.columns
WHERE table_schema = 'schema_bianace'
    AND table_name = 'trades'
    AND column_name IN ('create_time', 'update_time')
ORDER BY ordinal_position;

-- 应该显示 data_type='bigint', udt_name='int8'

-- ============================================
-- 第五步：检查是否还有其他 INTEGER 类型的时间字段
-- ============================================

SELECT 
    table_name,
    column_name,
    data_type,
    udt_name
FROM information_schema.columns
WHERE table_schema = 'schema_bianace'
    AND column_name IN ('create_time', 'update_time', 'last_update_time', 'trigger_time', 'check_time')
ORDER BY table_name, ordinal_position;

-- ============================================
-- 修复完成！
-- ============================================
