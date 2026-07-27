#!/bin/bash
# ============================================================
# K线数据清理脚本
# 清理 binance_data 数据库中超出保留期限的K线数据
# 建议每天凌晨 3:00 执行一次
# ============================================================
set -e

DB_USER="binance"
DB_NAME="binance_data"
CONTAINER="common_service_postgres"

# 各周期保留行数（基于HRS策略需求并留有冗余）
# 1h: HRS需要168根，保留336根（14天冗余）
# 4h: HRS需要50根，保留100根（~16天冗余）
# 15m: 保留384根（4天）
# 5m: 保留576根（2天）
# 1m: 保留1440根（1天）
declare -A RETENTION
RETENTION["1h"]=336
RETENTION["4h"]=100
RETENTION["15m"]=384
RETENTION["5m"]=576
RETENTION["1m"]=1440

LOG_FILE="/var/log/kline_cleanup.log"

echo "========================================" | tee -a "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') 开始K线清理" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"

TOTAL_DELETED=0

for INTERVAL in "${!RETENTION[@]}"; do
    KEEP="${RETENTION[$INTERVAL]}"
    
    echo "" | tee -a "$LOG_FILE"
    echo "--- 清理周期: ${INTERVAL}, 保留: ${KEEP} 行 ---" | tee -a "$LOG_FILE"
    
    # 获取所有该周期的K线表
    TABLES=$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -c \
        "SELECT relname FROM pg_stat_user_tables WHERE relname LIKE 'kline_%_${INTERVAL}' ORDER BY relname;")
    
    if [ -z "$TABLES" ]; then
        echo "  无 ${INTERVAL} 表，跳过" | tee -a "$LOG_FILE"
        continue
    fi
    
    for TABLE in $TABLES; do
        # 获取当前行数
        CURRENT=$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -c \
            "SELECT COUNT(*) FROM \"$TABLE\";")
        
        if [ "$CURRENT" -le "$KEEP" ]; then
            continue
        fi
        
        DELETE_COUNT=$((CURRENT - KEEP))
        
        # 删除超出保留期限的数据（保留最新的N条）
        docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c \
            "DELETE FROM \"$TABLE\" WHERE open_time NOT IN (
                SELECT open_time FROM \"$TABLE\" ORDER BY open_time DESC LIMIT $KEEP
            );" > /dev/null 2>&1
        
        ACTUAL_DELETED=$((CURRENT - $(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -c "SELECT COUNT(*) FROM \"$TABLE\";")))
        
        if [ "$ACTUAL_DELETED" -gt 0 ]; then
            echo "  ${TABLE}: ${CURRENT} -> $((CURRENT - ACTUAL_DELETED)) 行 (删除 ${ACTUAL_DELETED})" | tee -a "$LOG_FILE"
            TOTAL_DELETED=$((TOTAL_DELETED + ACTUAL_DELETED))
        fi
    done
    
    # VACUUM 该周期的表回收空间
    echo "  VACUUM ${INTERVAL} 表..." | tee -a "$LOG_FILE"
    for TABLE in $TABLES; do
        docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c "VACUUM \"$TABLE\";" > /dev/null 2>&1
    done
done

echo "" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') 清理完成，共删除 ${TOTAL_DELETED} 行" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"