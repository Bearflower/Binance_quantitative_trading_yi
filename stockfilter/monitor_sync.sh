#!/bin/bash
# K 线数据同步定时监控脚本
# 功能：每 10 分钟检查一次同步进度，异常时发送飞书告警

set -e

# 配置
CONTAINER_NAME="stockfilter-app"
CHECK_INTERVAL=600  # 10 分钟
LOG_FILE="/root/stockfilter/logs/sync_monitor.log"
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/955aced6-5b07-42a6-a714-4c5f4726b003"

# 创建日志目录
mkdir -p "$(dirname "$LOG_FILE")"

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# 飞书告警函数
send_feishu_alert() {
    local message="$1"
    local level="${2:-WARNING}"  # WARNING, ERROR, CRITICAL
    
    log "🚨 飞书告警：$message"
    
    # 设置消息颜色
    local color="red"
    case "$level" in
        "WARNING") color="orange" ;;
        "ERROR") color="red" ;;
        "CRITICAL") color="purple" ;;
        "INFO") color="blue" ;;
    esac
    
    # 构建飞书消息
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
                "content": "🔔 K 线同步监控告警",
                "tag": "plain_text"
            }
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "**告警级别：** $level\n\n$message\n\n**时间：** $(date '+%Y-%m-%d %H:%M:%S')\n**服务器：** 43.156.242.184"
            }
        ]
    }
}
EOF
)
    
    # 发送请求
    curl -s -X POST "$FEISHU_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "$payload" > /dev/null
    
    if [ $? -eq 0 ]; then
        log "✅ 飞书告警发送成功"
    else
        log "❌ 飞书告警发送失败"
    fi
}

# 发送进度报告
send_progress_report() {
    local stocks_count="$1"
    local error_rate="$2"
    
    log "📊 发送进度报告：$stocks_count 只股票，失败率 ${error_rate}%"
    
    local payload=$(cat <<EOF
{
    "msg_type": "interactive",
    "card": {
        "config": {
            "wide_screen_mode": true
        },
        "header": {
            "template": "green",
            "title": {
                "content": "📈 K 线同步进度报告",
                "tag": "plain_text"
            }
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "**当前进度：** $stocks_count 只股票已有数据\n\n**失败率：** ${error_rate}%\n\n**状态：** 同步任务正常运行\n\n**时间：** $(date '+%Y-%m-%d %H:%M:%S')"
            }
        ]
    }
}
EOF
)
    
    curl -s -X POST "$FEISHU_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "$payload" > /dev/null
}

# 检查进度
check_progress() {
    log "检查同步进度..."
    
    # 获取当前股票数量（直接在服务器上执行）
    CURRENT_COUNT=$(docker exec $CONTAINER_NAME python3 -c "
from data.database import DatabaseManager
db = DatabaseManager()
import pandas as pd
df = pd.read_sql('SELECT COUNT(DISTINCT code) as cnt FROM klines', db.conn)
print(df.iloc[0,0])
db.close()
" 2>/dev/null || echo "0")
    
    log "当前有 K 线数据的股票：$CURRENT_COUNT 只"
    
    # 获取最新日志
    LATEST_LOG=$(docker logs $CONTAINER_NAME --since 5m 2>&1 | tail -10)
    
    # 检查是否有进度日志（同步成功、批次完成、同步完成等都表示任务在运行）
    if echo "$LATEST_LOG" | grep -qE "同步成功 | 批次完成 | 同步完成 | 处理批次"; then
        log "✅ 同步任务正常运行"
    else
        log "⚠️  警告：最近 5 分钟没有成功记录"
        send_feishu_alert "同步任务可能已停止（最近 5 分钟无成功记录）\n\n当前有数据的股票：$CURRENT_COUNT 只" "WARNING"
    fi
    
    # 检查错误率
    RECENT_ERRORS=$(docker logs $CONTAINER_NAME --since 30m 2>&1 | grep -c '同步失败' || echo "0")
    RECENT_SUCCESS=$(docker logs $CONTAINER_NAME --since 30m 2>&1 | grep -c '同步成功' || echo "0")
    
    if [ "$RECENT_SUCCESS" -gt 0 ]; then
        TOTAL=$((RECENT_ERRORS + RECENT_SUCCESS))
        ERROR_RATE=$((RECENT_ERRORS * 100 / TOTAL))
        log "最近 30 分钟：成功 $RECENT_SUCCESS 只，失败 $RECENT_ERRORS 只，失败率 ${ERROR_RATE}%"
        
        if [ "$ERROR_RATE" -gt 10 ]; then
            send_feishu_alert "失败率过高：${ERROR_RATE}%（阈值：10%）\n\n成功：$RECENT_SUCCESS 只\n失败：$RECENT_ERRORS 只" "WARNING"
        fi
    fi
    
    # 检查容器状态
    CONTAINER_STATUS=$(docker ps -f name=$CONTAINER_NAME --format '{{.Status}}')
    
    if echo "$CONTAINER_STATUS" | grep -q "Up"; then
        log "✅ 容器状态正常：$CONTAINER_STATUS"
    else
        log "🚨 容器状态异常：$CONTAINER_STATUS"
        send_feishu_alert "容器状态异常：$CONTAINER_STATUS\n\n请立即检查服务器！" "CRITICAL"
    fi
    
    echo ""
}

# 主循环
log "=========================================="
log "启动 K 线数据同步监控"
log "检查间隔：$((CHECK_INTERVAL / 60)) 分钟"
log "飞书告警：已启用"
log "=========================================="
echo ""

# 发送启动通知
send_feishu_alert "监控服务已启动\n\n检查间隔：$((CHECK_INTERVAL / 60)) 分钟\n服务器：43.156.242.184" "INFO"

while true; do
    check_progress
    
    NEXT_CHECK=$(date -d "+$CHECK_INTERVAL seconds" '+%H:%M:%S')
    log "下次检查时间：$NEXT_CHECK"
    echo ""
    
    sleep $CHECK_INTERVAL
done
