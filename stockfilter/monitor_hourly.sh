#!/bin/bash
# K 线数据同步监控脚本（每小时推送版）

CONTAINER_NAME="stockfilter-app"
CHECK_INTERVAL=3600  # 60 分钟
LOG_FILE="/root/stockfilter/logs/sync_monitor.log"
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/955aced6-5b07-42a6-a714-4c5f4726b003"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# 发送飞书消息
send_message() {
    local title="$1"
    local content="$2"
    local color="${3:-blue}"
    
    log "发送飞书消息：$title"
    
    local payload=$(cat <<EOF
{
    "msg_type": "interactive",
    "card": {
        "config": {
            "wide_screen_mode": true
        },
        "header": {
            "template": "$color",
            "title": {
                "content": "$title",
                "tag": "plain_text"
            }
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "$content\n\n**时间：** $(date '+%Y-%m-%d %H:%M:%S')\n**服务器：** 43.156.242.184"
            }
        ]
    }
}
EOF
)
    
    curl -s -X POST "$FEISHU_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "$payload" > /dev/null
    
    if [ $? -eq 0 ]; then
        log "✅ 飞书消息发送成功"
    else
        log "❌ 飞书消息发送失败"
    fi
}

# 检查进度
check_progress() {
    log "=========================================="
    log "检查同步进度..."
    
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
    
    # 获取最近 1 小时成功次数（检查批次完成或同步成功）
    RECENT_SUCCESS=$(docker logs $CONTAINER_NAME --since 1h 2>&1 | grep -cE '同步成功 | 批次完成' || echo "0")
    log "最近 1 小时：成功 $RECENT_SUCCESS 次"
    
    # 检查容器状态
    STATUS=$(docker ps -f name=$CONTAINER_NAME --format '{{.Status}}')
    if echo "$STATUS" | grep -q "Up"; then
        log "容器状态：$STATUS ✅"
    else
        log "容器状态异常：$STATUS ❌"
        send_message "🔴 容器异常告警" "**容器状态异常：** $STATUS\n\n请立即检查服务器！" "red"
        return
    fi
    
    # 计算进度
    TOTAL_STOCKS=5324
    PROGRESS=$((COUNT * 100 / TOTAL_STOCKS))
    
    # 发送进度报告
    local content="**当前进度：** $COUNT / $TOTAL_STOCKS 只 ($PROGRESS%)\n\n**最近 1 小时：** 成功 $RECENT_SUCCESS 只股票\n\n**状态：** 同步任务正常运行\n\n**数据完整性：**\n• 完整 (≥200 天): 待统计\n• 较完整 (100-199 天): 待统计\n• 较少 (<100 天): 待统计"
    
    send_message "📈 K 线同步进度报告" "$content" "green"
    
    log "下次检查时间：$(date -d "+$CHECK_INTERVAL seconds" '+%H:%M:%S')"
    echo ""
}

# 主程序
log "=========================================="
log "启动 K 线数据同步监控（每小时推送）"
log "检查间隔：$((CHECK_INTERVAL / 60)) 分钟"
log "=========================================="

# 发送启动通知
send_message "🔔 监控服务已启动" "**监控服务已启动**\n\n检查间隔：$((CHECK_INTERVAL / 60)) 分钟\n每小时推送一次进度报告" "blue"

# 等待 2 分钟后发送第一次报告，让用户确认收到消息
sleep 120
check_progress

# 进入主循环
while true; do
    sleep $CHECK_INTERVAL
    check_progress
done
