#!/bin/bash
# 独立监控脚本（部署在服务器宿主机上，不在容器内）

CONTAINER_NAME="stockfilter-app"
CHECK_INTERVAL=3600  # 60 分钟
LOG_FILE="/root/stockfilter/logs/monitor.log"
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/955aced6-5b07-42a6-a714-4c5f4726b003"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# 发送飞书消息
send_message() {
    local title="$1"
    local content="$2"
    
    log "发送飞书消息：$title"
    
    local message="${title}\n\n${content}\n\n时间：$(date '+%Y-%m-%d %H:%M:%S')\n服务器：43.156.242.184"
    
    curl -s -X POST "$FEISHU_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"${message}\"}}" > /dev/null
    
    if [ $? -eq 0 ]; then
        log "✅ 飞书消息发送成功"
    else
        log "❌ 飞书消息发送失败"
    fi
}

# 检查容器状态
check_container() {
    local status=$(docker ps -f name=$CONTAINER_NAME --format '{{.Status}}')
    
    if echo "$status" | grep -q "Up"; then
        log "✅ 容器状态正常：$status"
        return 0
    else
        log "🚨 容器状态异常：$status"
        send_message "🔴 容器异常告警" "容器状态异常：$status\n\n请立即检查服务器！"
        return 1
    fi
}

# 检查同步进度
check_progress() {
    log "=========================================="
    log "检查同步进度..."
    
    # 获取股票数量
    local count=$(docker exec $CONTAINER_NAME python3 -c "
from data.database import DatabaseManager
db = DatabaseManager()
import pandas as pd
df = pd.read_sql('SELECT COUNT(DISTINCT code) as cnt FROM klines', db.conn)
print(int(df.iloc[0,0]))
db.close()
" 2>&1 | tail -1)
    
    log "有数据的股票：$count 只"
    
    # 获取最近 1 小时成功次数（检查批次完成或同步成功）
    local recent_success=$(docker logs $CONTAINER_NAME --since 1h 2>&1 | grep -cE '同步成功 | 批次完成' || echo "0")
    log "最近 1 小时：成功 $recent_success 次"
    
    # 检查容器状态
    if ! check_container; then
        return
    fi
    
    # 计算进度
    local total_stocks=3026
    local need_sync=2052
    local progress=$((count * 100 / total_stocks))
    
    # 发送进度报告
    local content="当前进度：${count}/${total_stocks} 只 (${progress}%)\n\n需要同步：${need_sync} 只\n最近 1 小时：成功 ${recent_success} 只\n\n状态：同步任务正常运行"
    
    send_message "📈 K 线同步进度报告" "$content"
    
    log "下次检查时间：$(date -d "+$CHECK_INTERVAL seconds" '+%H:%M:%S')"
    echo ""
}

# 主程序
log "=========================================="
log "启动 K 线数据同步监控（独立部署）"
log "检查间隔：$((CHECK_INTERVAL / 60)) 分钟"
log "监控方式：宿主机独立进程"
log "=========================================="

# 发送启动通知
send_message "🔔 监控服务已启动（独立部署）" "监控服务已启动\n\n检查间隔：$((CHECK_INTERVAL / 60)) 分钟\n每小时推送一次进度报告\n\n**优势：** 与同步任务隔离，即使容器卡住也能正常告警"

# 等待 2 分钟后发送第一次报告
sleep 120
check_progress

# 进入主循环
while true; do
    sleep $CHECK_INTERVAL
    check_progress
done
