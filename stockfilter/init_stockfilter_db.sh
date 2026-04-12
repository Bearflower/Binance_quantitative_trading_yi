#!/bin/bash
# 在 postgres-db 容器中初始化 stockfilter 数据库

set -e

echo "============================================="
echo "初始化 stockfilter 数据库"
echo "============================================="

# 创建数据库和用户
docker exec postgres-db bash -c 'psql -U trading_user -d trading_platform -c "CREATE DATABASE stockfilter;"' 2>/dev/null || echo "数据库可能已存在"
docker exec postgres-db bash -c 'psql -U trading_user -d trading_platform -c "CREATE USER stockfilter_user WITH PASSWORD '\"'\"'Stock@2024'\"'\"';"' 2>/dev/null || echo "用户可能已存在"
docker exec postgres-db bash -c 'psql -U trading_user -d trading_platform -c "GRANT ALL PRIVILEGES ON DATABASE stockfilter TO stockfilter_user;"'

# 创建 schema
docker exec postgres-db bash -c 'psql -U stockfilter_user -d stockfilter -c "CREATE SCHEMA IF NOT EXISTS schema_stockfilter AUTHORIZATION stockfilter_user;"'
docker exec postgres-db bash -c 'psql -U stockfilter_user -d stockfilter -c "GRANT ALL ON SCHEMA schema_stockfilter TO stockfilter_user;"'

echo ""
echo "============================================="
echo "数据库初始化完成！"
echo "============================================="
echo "数据库名：stockfilter"
echo "用户名：stockfilter_user"
echo "密码：Stock@2024"
echo "Schema: schema_stockfilter"
echo "============================================="

# 验证
docker exec postgres-db bash -c 'psql -U stockfilter_user -d stockfilter -c "\\dn"'
