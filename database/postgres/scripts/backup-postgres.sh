#!/bin/bash
# =====================================================
# PostgreSQL 备份脚本
# =====================================================

BACKUP_DIR="/backups/postgres"
DATE=$(date +%Y%m%d_%H%M%S)
CONTAINER_NAME="postgres-db"
DB_USER="trading_user"
DB_NAME="trading_platform"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}PostgreSQL 备份开始${NC}"
echo -e "${GREEN}================================${NC}"
echo -e "${YELLOW}备份时间：$(date '+%Y-%m-%d %H:%M:%S')${NC}"

# 创建备份目录
mkdir -p $BACKUP_DIR

# 全量备份
echo -e "${YELLOW}正在执行全量备份...${NC}"
docker exec $CONTAINER_NAME pg_dump -U $DB_USER -d $DB_NAME --format=custom \
  > $BACKUP_DIR/full_backup_$DATE.dump

if [ $? -eq 0 ]; then
    # 压缩备份
    echo -e "${YELLOW}正在压缩备份文件...${NC}"
    gzip $BACKUP_DIR/full_backup_$DATE.dump
    
    BACKUP_SIZE=$(ls -lh $BACKUP_DIR/full_backup_$DATE.dump.gz | awk '{print $5}')
    echo -e "${GREEN}✅ 备份完成：$BACKUP_DIR/full_backup_$DATE.dump.gz (大小：$BACKUP_SIZE)${NC}"
else
    echo -e "${RED}❌ 备份失败！${NC}"
    exit 1
fi

# 删除 30 天前的备份
echo -e "${YELLOW}清理 30 天前的旧备份...${NC}"
OLD_BACKUPS=$(find $BACKUP_DIR -name "*.dump.gz" -mtime +30 | wc -l)
if [ $OLD_BACKUPS -gt 0 ]; then
    find $BACKUP_DIR -name "*.dump.gz" -mtime +30 -delete
    echo -e "${GREEN}✅ 删除了 $OLD_BACKUPS 个旧备份文件${NC}"
else
    echo -e "${YELLOW}无需清理旧备份${NC}"
fi

# 显示备份列表
echo -e "\n${YELLOW}当前备份列表:${NC}"
ls -lht $BACKUP_DIR/*.dump.gz | head -10

echo -e "\n${GREEN}================================${NC}"
echo -e "${GREEN}备份任务完成${NC}"
echo -e "${GREEN}================================${NC}"
