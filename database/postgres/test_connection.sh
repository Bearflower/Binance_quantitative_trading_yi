#!/bin/bash
# =====================================================
# PostgreSQL 数据库连接测试脚本
# =====================================================

echo "============================================="
echo "PostgreSQL 数据库连接测试"
echo "============================================="

# 测试 bianace 用户连接
echo -e "\n1️⃣  测试 bianace_user 连接..."
docker exec postgres-db psql -U bianace_user -d trading_platform -c "SET search_path TO schema_bianace; SELECT current_user, current_database();"

# 测试 grid 用户连接
echo -e "\n2️⃣  测试 grid_user 连接..."
docker exec postgres-db psql -U grid_user -d trading_platform -c "SET search_path TO schema_grid; SELECT current_user, current_database();"

# 测试 short_selling 用户连接
echo -e "\n3️⃣  测试 short_selling_user 连接..."
docker exec postgres-db psql -U short_selling_user -d trading_platform -c "SET search_path TO schema_short_selling; SELECT current_user, current_database();"

# 测试 stockfilter 用户连接
echo -e "\n4️⃣  测试 stockfilter_user 连接..."
docker exec postgres-db psql -U stockfilter_user -d trading_platform -c "SET search_path TO schema_stockfilter; SELECT current_user, current_database();"

# 显示所有 schema 的表数量
echo -e "\n============================================="
echo "📊 各 Schema 表数量统计"
echo "============================================="
docker exec postgres-db psql -U trading_user -d trading_platform -c "
SELECT 
    table_schema,
    COUNT(*) as table_count
FROM information_schema.tables
WHERE table_schema IN ('schema_bianace', 'schema_grid', 'schema_short_selling', 'schema_stockfilter')
GROUP BY table_schema
ORDER BY table_schema;
"

echo -e "\n============================================="
echo "✅ PostgreSQL 数据库部署完成！"
echo "============================================="
echo ""
echo "📝 连接信息:"
echo "  主机：localhost (服务器 IP)"
echo "  端口：5432"
echo "  数据库：trading_platform"
echo ""
echo "🔑 用户信息:"
echo "  - bianace_user (schema_bianace)"
echo "  - grid_user (schema_grid)"
echo "  - short_selling_user (schema_short_selling)"
echo "  - stockfilter_user (schema_stockfilter)"
echo ""
echo "📂 数据库文件位置：/root/database/postgres"
echo "📊 备份文件位置：/root/database/postgres/backups"
echo ""
