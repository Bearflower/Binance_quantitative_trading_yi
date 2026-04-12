#!/bin/bash
# =====================================================
# PostgreSQL 恢复脚本
# 使用方法：./restore-postgres.sh <备份文件>
# =====================================================

if [ -z "$1" ]; then
    echo "错误：请指定备份文件路径"
    echo "用法：$0 <备份文件.dump.gz>"
    exit 1
fi

BACKUP_FILE=$1
CONTAINER_NAME="postgres-db"
DB_USER="trading_user"
DB_NAME="trading_platform"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${RED}================================${NC}"
echo -e "${RED}⚠️  警告：此操作将恢复数据库！${NC}"
echo -e "${RED}================================${NC}"
echo -e "${YELLOW}备份文件：$BACKUP_FILE${NC}"
echo -e "${YELLOW}目标数据库：$DB_NAME${NC}"
echo ""

read -p "确定要继续吗？(yes/no): " -r
echo
if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    echo -e "${YELLOW}操作已取消${NC}"
    exit 0
fi

# 检查备份文件是否存在
if [ ! -f "$BACKUP_FILE" ]; then
    echo -e "${RED}❌ 备份文件不存在：$BACKUP_FILE${NC}"
    exit 1
fi

# 解压文件（如果是 gzip 格式）
if [[ $BACKUP_FILE == *.gz ]]; then
    echo -e "${YELLOW}正在解压备份文件...${NC}"
    TEMP_FILE="/tmp/restore_backup.dump"
    gunzip -c $BACKUP_FILE > $TEMP_FILE
    BACKUP_FILE=$TEMP_FILE
fi

# 恢复数据库
echo -e "${YELLOW}正在恢复数据库...${NC}"
docker exec -i $CONTAINER_NAME pg_restore -U $DB_USER -d $DB_NAME --clean --if-exists < $BACKUP_FILE

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ 数据库恢复完成！${NC}"
    echo -e "${YELLOW}建议：重启应用容器以确保连接正常${NC}"
else
    echo -e "${RED}❌ 数据库恢复失败！${NC}"
    exit 1
fi

# 清理临时文件
if [ -f "$TEMP_FILE" ]; then
    rm -f $TEMP_FILE
fi

echo -e "${GREEN}================================${NC}"
