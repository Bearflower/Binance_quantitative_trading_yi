-- ============================================
-- HRS 策略 Schema 初始化脚本
-- 用于生产环境 DBA 预创建 hrs schema 并授予 trading_user 必要权限，
-- 避免应用账号因缺少库级 CREATE 权限导致 _ensure_db_schema 失败。
-- 幂等、自包含、无 BEGIN/COMMIT，与 12-research-schema.sql 同风格
-- ============================================

-- 创建 hrs Schema（若已存在则跳过）
CREATE SCHEMA IF NOT EXISTS hrs;

-- 授权 trading_user 对 hrs Schema 的整体访问
GRANT ALL PRIVILEGES ON SCHEMA hrs TO trading_user;

-- 未来在 hrs schema 中新建的表自动授予 trading_user 读写权限
ALTER DEFAULT PRIVILEGES IN SCHEMA hrs
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trading_user;

-- 未来在 hrs schema 中新建的序列（如 BIGSERIAL 生成的 id）自动授权 USAGE/SELECT
-- 与 12-research-schema.sql 口径一致；hrs_signals.id 是 BIGSERIAL 生成
ALTER DEFAULT PRIVILEGES IN SCHEMA hrs
    GRANT USAGE, SELECT ON SEQUENCES TO trading_user;
