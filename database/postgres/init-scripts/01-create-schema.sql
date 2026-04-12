-- =====================================================
-- 数据库初始化脚本
-- 创建 Schema、用户和权限配置
-- =====================================================

-- 创建各应用 schema
CREATE SCHEMA IF NOT EXISTS schema_bianace;
CREATE SCHEMA IF NOT EXISTS schema_grid;
CREATE SCHEMA IF NOT EXISTS schema_short_selling;
CREATE SCHEMA IF NOT EXISTS schema_stockfilter;

-- 创建应用用户
CREATE USER bianace_user WITH PASSWORD 'Bianace@2024';
CREATE USER grid_user WITH PASSWORD 'Grid@2024';
CREATE USER short_selling_user WITH PASSWORD 'ShortSell@2024';
CREATE USER stockfilter_user WITH PASSWORD 'Stock@2024';

-- 授予 schema 使用权
GRANT ALL PRIVILEGES ON SCHEMA schema_bianace TO bianace_user;
GRANT ALL PRIVILEGES ON SCHEMA schema_grid TO grid_user;
GRANT ALL PRIVILEGES ON SCHEMA schema_short_selling TO short_selling_user;
GRANT ALL PRIVILEGES ON SCHEMA schema_stockfilter TO stockfilter_user;

-- 授予默认表权限（未来创建的表会自动授权）
ALTER DEFAULT PRIVILEGES IN SCHEMA schema_bianace GRANT ALL ON TABLES TO bianace_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA schema_grid GRANT ALL ON TABLES TO grid_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA schema_short_selling GRANT ALL ON TABLES TO short_selling_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA schema_stockfilter GRANT ALL ON TABLES TO stockfilter_user;

-- 授予序列权限（用于自增主键）
ALTER DEFAULT PRIVILEGES IN SCHEMA schema_bianace GRANT ALL ON SEQUENCES TO bianace_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA schema_grid GRANT ALL ON SEQUENCES TO grid_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA schema_short_selling GRANT ALL ON SEQUENCES TO short_selling_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA schema_stockfilter GRANT ALL ON SEQUENCES TO stockfilter_user;

-- 授予主用户所有权限
GRANT ALL PRIVILEGES ON DATABASE trading_platform TO trading_user;
GRANT ALL PRIVILEGES ON SCHEMA schema_bianace TO trading_user;
GRANT ALL PRIVILEGES ON SCHEMA schema_grid TO trading_user;
GRANT ALL PRIVILEGES ON SCHEMA schema_short_selling TO trading_user;
GRANT ALL PRIVILEGES ON SCHEMA schema_stockfilter TO trading_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_bianace TO trading_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_grid TO trading_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_short_selling TO trading_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA schema_stockfilter TO trading_user;

-- 授予连接权限
GRANT CONNECT ON DATABASE trading_platform TO bianace_user;
GRANT CONNECT ON DATABASE trading_platform TO grid_user;
GRANT CONNECT ON DATABASE trading_platform TO short_selling_user;
GRANT CONNECT ON DATABASE trading_platform TO stockfilter_user;

-- 显示当前配置
\echo '================================'
\echo '数据库初始化完成！'
\echo 'Schema 创建：schema_bianace, schema_grid, schema_short_selling, schema_stockfilter'
\echo '用户创建：bianace_user, grid_user, short_selling_user, stockfilter_user'
\echo '================================'
