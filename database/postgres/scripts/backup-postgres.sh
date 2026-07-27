#!/bin/bash

# ============================================
# PostgreSQL 数据库自动备份脚本
# ============================================

set -e

# 配置参数
BACKUP_DIR="/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="trading_platform_${TIMESTAMP}.sql.gz"
RETENTION_DAYS=7

# 数据库连接信息
DB_HOST="postgres"
DB_PORT="5432"
DB_NAME="trading_platform"
DB_USER="trading_user"

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# 创建备份目录
mkdir -p "$BACKUP_DIR"

log "开始备份数据库: $DB_NAME"

# 执行备份
docker exec postgres-db pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    | gzip > "${BACKUP_DIR}/${BACKUP_FILE}"

# 检查备份是否成功
if [ $? -eq 0 ]; then
    BACKUP_SIZE=$(ls -lh "${BACKUP_DIR}/${BACKUP_FILE}" | awk '{print $5}')
    log "备份成功: ${BACKUP_FILE} (大小: ${BACKUP_SIZE})"
else
    log "备份失败!"
    exit 1
fi

# 清理旧备份文件
log "清理 ${RETENTION_DAYS} 天前的旧备份..."
find "$BACKUP_DIR" -name "trading_platform_*.sql.gz" -type f -mtime +${RETENTION_DAYS} -delete

# 统计备份文件数量
BACKUP_COUNT=$(find "$BACKUP_DIR" -name "trading_platform_*.sql.gz" -type f | wc -l)
log "当前备份文件数量: ${BACKUP_COUNT}"

log "备份完成!"
