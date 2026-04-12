#!/bin/bash
# K 线数据同步定时监控脚本（简化版）

CONTAINER_NAME="stockfilter-app"
CHECK_INTERVAL=600
LOG_FILE="/root/stockfilter/logs/sync_monitor.log"
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/955aced6-5b07-42a6-a714-4c5f4726b003"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

send_alert() {
    local msg="$1"
    log "发送飞书告警：$msg"
    curl -s -X POST "$FEISHU_WEBHOOK" -H "Content-Type: application/json" \
        -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$msg\"}}" > /dev/null
}

log "=========================================="
log "监控服务启动"
send_alert "监控服务已启动 - $(date '+%H:%M:%S')"

while true; do
    log "检查进度..."
    
    # 获取股票数量
    COUNT=$(docker exec $CONTAINER_NAME python3 -c "
from data.database import DatabaseManager
db = DatabaseManager()
import pandas as pd
df = pd.read_sql('SELECT COUNT(DISTINCT code) as cnt FROM klines', db.conn)
print(int(df.iloc[0,0]))
db.close()
" 2>&1 | tail -1)
    
    log "有数据的股票：$COUNT 只"
    
    # 检查容器
    STATUS=$(docker ps -f name=$CONTAINER_NAME --format '{{.Status}}')
    if echo "$STATUS" | grep -q "Up"; then
        log "容器状态：$STATUS ✅"
    else
        log "容器状态异常：$STATUS ❌"
        send_alert "容器异常：$STATUS"
    fi
    
    # 检查最近日志（检查批次完成或同步成功）
    RECENT=$(docker logs $CONTAINER_NAME --since 5m 2>&1 | grep -cE '同步成功 | 批次完成 | 处理批次' || echo "0")
    if [ "$RECENT" -gt 0 ]; then
        log "最近 5 分钟：成功 $RECENT 次 ✅"
    else
        log "最近 5 分钟：无成功记录 ⚠️"
        send_alert "同步可能已停止 - 当前 $COUNT 只股票"
    fi
    
    sleep $CHECK_INTERVAL
done
