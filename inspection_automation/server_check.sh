#!/bin/bash

# 服务器自动化巡检脚本
# 每日 07:30 执行
# 检查项目运行状态、服务器资源使用、Docker 日志错误等
# 通过飞书 webhook 发送巡检结果

# 配置部分
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/94ab2c34-52e3-4737-9e2f-c8cd8235e8e7"
DOCKER_CONTAINER_PATTERN=""
CPU_THRESHOLD=80  # CPU 使用率阈值 (%)
MEMORY_THRESHOLD=80  # 内存使用率阈值 (%)
DISK_THRESHOLD=80  # 磁盘使用率阈值 (%)

# Docker 清理配置
DOCKER_CLEANUP_ENABLED=true  # 是否启用 Docker 自动清理
DOCKER_CLEANUP_SCHEDULE="weekly"  # 清理计划：weekly(每周日) | daily(每天) | threshold(按阈值)
DOCKER_CLEANUP_THRESHOLD=70  # 磁盘使用率达到此阈值时触发清理 (%)
DOCKER_CLEANUP_TYPE="builder_only"  # 清理类型：builder_only(仅缓存) | builder_and_images(缓存 + 镜像)

# SSH 免密登录检查配置
SSH_CHECK_ENABLED=true  # 是否启用 SSH 免密登录检查
SSH_HOST="43.156.242.184"
SSH_USER="root"
SSH_IDENTITY_FILE="/Users/yl/vscode/inspection_automation/docs/only.pem"

# 初始化巡检结果变量
CHECK_RESULT=""
ALERT_COUNT=0

# 函数：记录检查结果
record_result() {
    local status="$1"
    local message="$2"
    
    # 输出到标准输出（会被重定向到日志文件）
    if [[ "$status" == "ERROR" ]]; then
        echo "❌ $message"
        ALERT_COUNT=$((ALERT_COUNT + 1))
        CHECK_RESULT="${CHECK_RESULT}❌ $message
"
    else
        echo "✅ $message"
        CHECK_RESULT="${CHECK_RESULT}✅ $message
"
    fi
}

# 函数：检查项目运行状态
check_project_status() {
    echo "检查项目运行状态..."
    
    # 检查 Docker 容器是否运行
    if command -v docker &> /dev/null; then
        # 获取所有容器（如果配置了模式则过滤）
        if [[ -z "$DOCKER_CONTAINER_PATTERN" ]]; then
            CONTAINERS=$(docker ps -a --format "{{.Names}}" 2>/dev/null)
        else
            CONTAINERS=$(docker ps -a --format "{{.Names}}" 2>/dev/null | grep -i "$DOCKER_CONTAINER_PATTERN")
        fi
        
        if [[ -z "$CONTAINERS" ]]; then
            if [[ -n "$DOCKER_CONTAINER_PATTERN" ]]; then
                record_result "ERROR" "未找到包含 '$DOCKER_CONTAINER_PATTERN' 的 Docker 容器"
            else
                record_result "ERROR" "未找到任何 Docker 容器"
            fi
        else
            # 检查每个匹配的容器
            while IFS= read -r CONTAINER_NAME; do
                CONTAINER_STATUS=$(docker ps -a --filter "name=$CONTAINER_NAME" --format "{{.Status}}" 2>/dev/null)
                CONTAINER_HEALTH=$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "unknown")
                
                if [[ "$CONTAINER_STATUS" == *"Up"* ]]; then
                    if [[ "$CONTAINER_HEALTH" == "healthy" || "$CONTAINER_HEALTH" == "unknown" ]]; then
                        record_result "OK" "Docker 容器运行正常：$CONTAINER_NAME"
                    elif [[ "$CONTAINER_HEALTH" == "unhealthy" ]]; then
                        record_result "ERROR" "Docker 容器运行但状态异常：$CONTAINER_NAME (状态：unhealthy)"
                    else
                        record_result "ERROR" "Docker 容器运行但状态未知：$CONTAINER_NAME (状态：$CONTAINER_HEALTH)"
                    fi
                    
                    # 检查容器内进程
                    CONTAINER_RUNNING=$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)
                    if [[ "$CONTAINER_RUNNING" == "true" ]]; then
                        record_result "OK" "容器 $CONTAINER_NAME 内进程运行正常"
                    else
                        record_result "ERROR" "容器 $CONTAINER_NAME 内进程异常"
                    fi
                else
                    record_result "ERROR" "Docker 容器未运行：$CONTAINER_NAME (状态：$CONTAINER_STATUS)"
                fi
            done <<< "$CONTAINERS"
        fi
    else
        record_result "ERROR" "Docker 命令不可用"
        
        # 检查本地进程
        if ps aux | grep -i "$DOCKER_CONTAINER_PATTERN" | grep -v grep &> /dev/null; then
            record_result "OK" "项目进程运行正常"
        else
            record_result "ERROR" "项目进程未运行"
        fi
    fi
}

# 函数：检查服务器 CPU 和内存占用率
check_server_resources() {
    echo "检查服务器资源使用情况..."
    
    # 检查 CPU 使用率
    if command -v top &> /dev/null; then
        # Linux 系统 CPU 使用率检查
        CPU_USAGE=$(top -bn1 | grep "Cpu(s)" | sed "s/.*, *\([0-9.]*\)%* id.*/\1/" | awk '{print 100 - $1}')
        CPU_USAGE=$(printf "%.2f" "$CPU_USAGE")
        CPU_USAGE=${CPU_USAGE:-0}
        
        if (( $(echo "$CPU_USAGE > $CPU_THRESHOLD" | bc -l) )); then
            record_result "ERROR" "CPU 使用率过高：${CPU_USAGE}% (阈值：${CPU_THRESHOLD}%)"
        else
            record_result "OK" "CPU 使用率正常：${CPU_USAGE}%"
        fi
    else
        record_result "ERROR" "无法获取 CPU 使用率"
    fi
    
    # 检查内存使用率
    if command -v free &> /dev/null; then
        MEM_TOTAL=$(free -m | awk '/Mem:/ {print $2}')
        MEM_USED=$(free -m | awk '/Mem:/ {print $3}')
        if [[ -n "$MEM_TOTAL" && "$MEM_TOTAL" != "0" ]]; then
            MEM_USAGE=$(echo "scale=2; $MEM_USED * 100 / $MEM_TOTAL" | bc)
            
            if (( $(echo "$MEM_USAGE > $MEMORY_THRESHOLD" | bc -l) )); then
                record_result "ERROR" "内存使用率过高：${MEM_USAGE}% (阈值：${MEMORY_THRESHOLD}%)"
            else
                record_result "OK" "内存使用率正常：${MEM_USAGE}%"
            fi
        else
            record_result "ERROR" "无法获取内存使用率"
        fi
    else
        record_result "ERROR" "无法获取内存使用率"
    fi
    
    # 检查磁盘使用率
    if command -v df &> /dev/null; then
        DISK_USAGE=$(df -h | grep -E '^/dev/' | grep -v tmpfs | awk '{print $5}' | sed 's/%//' | sort -nr | head -1)
        DISK_USAGE=${DISK_USAGE:-0}
        
        if (( DISK_USAGE > DISK_THRESHOLD )); then
            record_result "ERROR" "磁盘使用率过高：${DISK_USAGE}% (阈值：${DISK_THRESHOLD}%)"
        else
            record_result "OK" "磁盘使用率正常：${DISK_USAGE}%"
        fi
    else
        record_result "ERROR" "无法获取磁盘使用率"
    fi
}

# 函数：检查 Docker 日志错误
check_docker_logs() {
    echo "检查 Docker 日志错误..."
    
    if command -v docker &> /dev/null; then
        # 获取所有容器（如果配置了模式则过滤）
        if [[ -z "$DOCKER_CONTAINER_PATTERN" ]]; then
            CONTAINERS=$(docker ps -a --format "{{.Names}}" 2>/dev/null)
        else
            CONTAINERS=$(docker ps -a --format "{{.Names}}" 2>/dev/null | grep -i "$DOCKER_CONTAINER_PATTERN")
        fi
        
        if [[ -z "$CONTAINERS" ]]; then
            if [[ -n "$DOCKER_CONTAINER_PATTERN" ]]; then
                record_result "ERROR" "未找到包含 '$DOCKER_CONTAINER_PATTERN' 的 Docker 容器，无法检查日志"
            else
                record_result "ERROR" "未找到任何 Docker 容器，无法检查日志"
            fi
        else
            # 检查每个匹配容器的日志
            while IFS= read -r CONTAINER_NAME; do
                # 检查容器是否存在
                if docker ps -a --filter "name=$CONTAINER_NAME" | grep -q "$CONTAINER_NAME"; then
                    # 检查最近 24 小时的错误日志
                    ERROR_COUNT=$(docker logs --since 24h "$CONTAINER_NAME" 2>&1 | grep -i "error" | wc -l)
                    
                    if (( ERROR_COUNT > 0 )); then
                        # 获取最近 5 条错误日志
                        RECENT_ERRORS=$(docker logs --since 24h "$CONTAINER_NAME" 2>&1 | grep -i "error" | tail -5 | tr '\n' '; ')
                        record_result "ERROR" "容器 $CONTAINER_NAME 日志中发现 ${ERROR_COUNT} 个错误：${RECENT_ERRORS}"
                    else
                        record_result "OK" "容器 $CONTAINER_NAME 日志无错误"
                    fi
                else
                    record_result "ERROR" "容器 $CONTAINER_NAME 不存在，无法检查日志"
                fi
            done <<< "$CONTAINERS"
        fi
    else
        record_result "ERROR" "Docker 命令不可用，无法检查日志"
        
        # 检查本地日志文件
        if [[ -d "logs" ]]; then
            ERROR_COUNT=$(grep -r "error" logs/ --include="*.log" | wc -l)
            if (( ERROR_COUNT > 0 )); then
                record_result "ERROR" "本地日志中发现 ${ERROR_COUNT} 个错误"
            else
                record_result "OK" "本地日志无错误"
            fi
        else
            record_result "ERROR" "日志目录不存在"
        fi
    fi
}

# 函数：检查 SSH 免密登录状态（服务器端自检）
check_ssh_keyless_login() {
    echo "检查 SSH 免密登录状态..."
    
    # 注意：此检查项已废弃，因为：
    # 1. SSH 密钥存储在客户端（你的 Mac），而不是服务器
    # 2. 服务器上只需要 authorized_keys（公钥），不需要私钥
    # 3. 正确的检查方式是在本地运行 check_ssh_from_local.sh 脚本
    # 
    # 如果你想监控 SSH 密钥状态，请在本地定期运行：
    # ./check_ssh_from_local.sh
    
    echo "ℹ️  注：SSH 密钥检查已在本地执行，服务器端自检已跳过"
    echo "   如需检查 SSH 密钥状态，请在本地运行：./check_ssh_from_local.sh"
    record_result "OK" "SSH 配置检查已跳过（使用本地检查脚本）"
    return 0
}

# 函数：专业运维角度的额外巡检项
check_additional_items() {
    echo "执行额外巡检项..."
    
    # 检查 SSH 免密登录状态
    check_ssh_keyless_login
    echo "================================================"
    
    # 检查网络连接状态
    if command -v ping &> /dev/null; then
        if ping -c 3 google.com &> /dev/null; then
            record_result "OK" "网络连接正常"
        else
            record_result "ERROR" "网络连接异常"
        fi
    else
        record_result "ERROR" "无法检查网络连接"
    fi
    
    # 检查系统负载
    if command -v uptime &> /dev/null; then
        LOAD_AVERAGE=$(uptime | awk -F'load average:' '{print $2}')
        record_result "OK" "系统负载：${LOAD_AVERAGE}"
    else
        record_result "ERROR" "无法获取系统负载"
    fi
    
    # 检查系统服务状态
    if command -v systemctl &> /dev/null; then
        CRITICAL_SERVICES=("sshd" "docker" "NetworkManager")
        for SERVICE in "${CRITICAL_SERVICES[@]}"; do
            if systemctl is-active --quiet "$SERVICE"; then
                record_result "OK" "服务 $SERVICE 运行正常"
            else
                record_result "ERROR" "服务 $SERVICE 未运行"
            fi
        done
    fi
    
    # 检查系统更新状态
    if command -v apt &> /dev/null; then
        UPDATE_COUNT=$(apt list --upgradable 2>/dev/null | grep -v "Listing..." | wc -l)
        if (( UPDATE_COUNT > 0 )); then
            record_result "OK" "系统有 ${UPDATE_COUNT} 个更新可用"
        else
            record_result "OK" "系统已更新到最新版本"
        fi
    elif command -v yum &> /dev/null; then
        UPDATE_COUNT=$(yum check-update 2>/dev/null | grep -v "Loaded plugins" | grep -v "Updated Packages" | wc -l)
        if (( UPDATE_COUNT > 0 )); then
            record_result "OK" "系统有 ${UPDATE_COUNT} 个更新可用"
        else
            record_result "OK" "系统已更新到最新版本"
        fi
    fi
    
    # Docker 空间清理（智能触发）
    check_and_cleanup_docker
}

# 函数：检查并清理 Docker 空间
check_and_cleanup_docker() {
    if [[ "$DOCKER_CLEANUP_ENABLED" != "true" ]]; then
        echo "Docker 自动清理已禁用，跳过"
        return 0
    fi
    
    echo "检查 Docker 空间使用情况..."
    
    # 统计悬空镜像信息（总是统计，用于报告）
    DANGLING_IMAGES_COUNT=$(docker images --filter "dangling=true" -q 2>/dev/null | wc -l)
    DANGLING_IMAGES_SIZE=$(docker images --filter "dangling=true" 2>/dev/null | awk 'NR>1 {sum+=$4} END {print sum}')
    
    if (( DANGLING_IMAGES_COUNT > 0 )); then
        record_result "OK" "发现 ${DANGLING_IMAGES_COUNT} 个悬空镜像，占用空间：${DANGLING_IMAGES_SIZE:-0}MB"
        record_result "OK" "悬空镜像清理提示：运行 /root/inspection/cleanup_dangling_images.sh 可清理悬空镜像"
    else
        record_result "OK" "无悬空镜像"
    fi
    
    # 检查是否需要自动清理构建缓存
    local should_cleanup=false
    
    # 方案 1：按周计划（每周日清理）
    if [[ "$DOCKER_CLEANUP_SCHEDULE" == "weekly" ]]; then
        DAY_OF_WEEK=$(date +%u)  # 1-7 (Monday-Sunday)
        if (( DAY_OF_WEEK == 7 )); then
            echo "今天是周日，执行定期构建缓存清理..."
            should_cleanup=true
        else
            echo "今天不是周日，跳过定期清理"
        fi
    fi
    
    # 方案 2：按阈值清理
    if [[ "$DOCKER_CLEANUP_SCHEDULE" == "threshold" ]]; then
        DISK_USAGE=$(df -h | grep '^/dev/' | awk '{print $5}' | sed 's/%//')
        DISK_USAGE=${DISK_USAGE:-0}
        
        echo "当前磁盘使用率：${DISK_USAGE}%"
        
        if (( DISK_USAGE > DOCKER_CLEANUP_THRESHOLD )); then
            echo "磁盘使用率超过阈值 (${DISK_USAGE}% > ${DOCKER_CLEANUP_THRESHOLD}%)，执行清理..."
            should_cleanup=true
        fi
    fi
    
    # 方案 3：每天清理
    if [[ "$DOCKER_CLEANUP_SCHEDULE" == "daily" ]]; then
        echo "每日清理模式，执行构建缓存清理..."
        should_cleanup=true
    fi
    
    # 执行清理
    if [[ "$should_cleanup" == "true" ]]; then
        # 清理 Docker 构建缓存
        echo "清理 Docker 构建缓存..."
        if docker builder prune -f &> /dev/null; then
            record_result "OK" "已清理 Docker 构建缓存（定期清理）"
        else
            record_result "ERROR" "Docker 构建缓存清理失败"
        fi
        
        # 如果配置了清理镜像（不推荐）
        if [[ "$DOCKER_CLEANUP_TYPE" == "builder_and_images" ]]; then
            echo "清理悬空镜像..."
            if docker image prune -f &> /dev/null; then
                record_result "OK" "已清理悬空镜像"
            else
                record_result "ERROR" "悬空镜像清理失败"
            fi
        fi
    else
        # 检查今天是否是周日，且清理日志是今天的
        if [[ "$DOCKER_CLEANUP_SCHEDULE" == "weekly" ]]; then
            DAY_OF_WEEK=$(date +%u)  # 1-7 (Monday-Sunday)
            if (( DAY_OF_WEEK == 7 )); then
                # 检查清理日志文件的修改时间
                CLEANUP_LOG="/root/inspection/cleanup.log"
                if [[ -f "$CLEANUP_LOG" ]]; then
                    LOG_DATE=$(date -r "$CLEANUP_LOG" +%Y-%m-%d)
                    TODAY=$(date +%Y-%m-%d)
                    if [[ "$LOG_DATE" == "$TODAY" ]]; then
                        record_result "OK" "今日已执行 Docker 构建缓存清理（03:00）"
                    fi
                fi
            fi
        fi
    fi
}

# 函数：发送飞书消息（修复版本）
send_feishu_message() {
    local message="$1"
    local ssh_status="$2"
    
    echo "发送飞书消息..."
    
    # 构建消息内容
    if (( ALERT_COUNT > 0 )); then
        title="🚨 服务器巡检报告 (发现 ${ALERT_COUNT} 个问题)"
    else
        title="✅ 服务器巡检报告 (一切正常)"
    fi
    
    # 构建完整消息
    CURRENT_TIME=$(date '+%Y-%m-%d %H:%M:%S')
    ERROR_COUNT=$(echo "$CHECK_RESULT" | grep -c "❌")
    OK_COUNT=$(echo "$CHECK_RESULT" | grep -c "✅")
    
    # 发送到飞书
    if command -v curl &> /dev/null; then
        if (( ERROR_COUNT > 0 )); then
            # 构建并发送多条消息
            
            # 第一条消息：基本信息（包含 SSH 状态）
            if [[ -n "$ssh_status" ]]; then
                MESSAGE1="服务器巡检发现问题
时间：${CURRENT_TIME}
共发现 ${ERROR_COUNT} 个问题
${ssh_status}

开始发送问题详情..."
            else
                MESSAGE1="服务器巡检发现问题
时间：${CURRENT_TIME}
共发现 ${ERROR_COUNT} 个问题

开始发送问题详情..."
            fi
            
            # 使用 jq 构建 JSON，避免转义问题
            if command -v jq &> /dev/null; then
                PAYLOAD1=$(jq -n --arg text "$MESSAGE1" '{"msg_type":"text","content":{"text":$text}}')
            else
                # 如果没有 jq，使用 Python 来构建 JSON
                PAYLOAD1=$(python3 -c "import json; print(json.dumps({'msg_type':'text','content':{'text':'''$MESSAGE1'''}}))" 2>/dev/null || echo '{"msg_type":"text","content":{"text":"'"$MESSAGE1"'"}}')
            fi
            
            response1=$(curl -s -X POST "$FEISHU_WEBHOOK" \
                -H "Content-Type: application/json" \
                -d "$PAYLOAD1"
            )
            
            echo "第一条消息响应：$response1"
            
            # 短暂延迟
            sleep 1
            
            # 发送详细巡检结果（所有检查项）
            if (( ERROR_COUNT > 0 || OK_COUNT > 0 )); then
                # 构建包含所有检查结果的消息
                DETAIL_MESSAGE="问题详情

"
                
                # 添加每个检查项
                while IFS= read -r ITEM; do
                    if [[ -n "$ITEM" ]]; then
                        # 替换 emoji 为文本标记
                        FORMATTED_ITEM=$(echo "$ITEM" | sed 's/❌/- /g' | sed 's/✅/✓ /g')
                        DETAIL_MESSAGE="${DETAIL_MESSAGE}${FORMATTED_ITEM}
"
                    fi
                done <<< "$CHECK_RESULT"
                
                # 发送问题详情消息
                if command -v jq &> /dev/null; then
                    PAYLOAD_DETAIL=$(jq -n --arg text "$DETAIL_MESSAGE" '{"msg_type":"text","content":{"text":$text}}')
                else
                    PAYLOAD_DETAIL=$(python3 -c "import json; print(json.dumps({'msg_type':'text','content':{'text':'''$DETAIL_MESSAGE'''}}))" 2>/dev/null || echo '{"msg_type":"text","content":{"text":"'"$DETAIL_MESSAGE"'"}}')
                fi
                
                echo "发送问题详情消息..."
                response_detail=$(curl -s -X POST "$FEISHU_WEBHOOK" \
                    -H "Content-Type: application/json" \
                    -d "$PAYLOAD_DETAIL"
                )
                
                echo "问题详情消息响应：$response_detail"
                
                # 短暂延迟
                sleep 1
            else
                echo "没有检查结果可发送"
            fi
            
            # 最后一条消息：总结
            MESSAGE2="问题详情发送完成

请登录服务器查看完整的巡检日志以获取所有信息。"
            
            if command -v jq &> /dev/null; then
                PAYLOAD2=$(jq -n --arg text "$MESSAGE2" '{"msg_type":"text","content":{"text":$text}}')
            else
                PAYLOAD2=$(python3 -c "import json; print(json.dumps({'msg_type':'text','content':{'text':'''$MESSAGE2'''}}))" 2>/dev/null || echo '{"msg_type":"text","content":{"text":"'"$MESSAGE2"'"}}')
            fi
            
            response2=$(curl -s -X POST "$FEISHU_WEBHOOK" \
                -H "Content-Type: application/json" \
                -d "$PAYLOAD2"
            )
            
            echo "总结消息响应：$response2"
            
            # 检查至少有一条消息发送成功
            if echo "$response1" | grep -q '"code":0' || echo "$response1" | grep -q '"StatusCode":0'; then
                echo "飞书消息发送成功（分批发送）"
                return 0
            else
                echo "飞书消息发送失败"
                return 1
            fi
        else
            # 构建正常消息（包含 SSH 状态）
            if [[ -n "$ssh_status" ]]; then
                MESSAGE1="服务器巡检正常
时间：${CURRENT_TIME}
所有检查项都正常
${ssh_status}

开始发送详细检查结果..."
            else
                MESSAGE1="服务器巡检正常
时间：${CURRENT_TIME}
所有检查项都正常

开始发送详细检查结果..."
            fi
            
            # 使用 jq 构建 JSON，避免转义问题
            if command -v jq &> /dev/null; then
                PAYLOAD1=$(jq -n --arg text "$MESSAGE1" '{"msg_type":"text","content":{"text":$text}}')
            else
                # 如果没有 jq，使用 Python 来构建 JSON
                PAYLOAD1=$(python3 -c "import json; print(json.dumps({'msg_type':'text','content':{'text':'''$MESSAGE1'''}}))" 2>/dev/null || echo '{"msg_type":"text","content":{"text":"'"$MESSAGE1"'"}}')
            fi
            
            response1=$(curl -s -X POST "$FEISHU_WEBHOOK" \
                -H "Content-Type: application/json" \
                -d "$PAYLOAD1"
            )
            
            echo "第一条消息响应：$response1"
            
            # 短暂延迟
            sleep 1
            
            # 发送详细巡检结果（所有检查项）
            if (( ERROR_COUNT > 0 || OK_COUNT > 0 )); then
                # 构建包含所有检查结果的消息
                DETAIL_MESSAGE="详细检查结果

"
                
                # 添加每个检查项
                while IFS= read -r ITEM; do
                    if [[ -n "$ITEM" ]]; then
                        # 替换 emoji 为文本标记
                        FORMATTED_ITEM=$(echo "$ITEM" | sed 's/❌/- /g' | sed 's/✅/✓ /g')
                        DETAIL_MESSAGE="${DETAIL_MESSAGE}${FORMATTED_ITEM}
"
                    fi
                done <<< "$CHECK_RESULT"
                
                # 发送详细检查结果消息
                if command -v jq &> /dev/null; then
                    PAYLOAD_DETAIL=$(jq -n --arg text "$DETAIL_MESSAGE" '{"msg_type":"text","content":{"text":$text}}')
                else
                    PAYLOAD_DETAIL=$(python3 -c "import json; print(json.dumps({'msg_type':'text','content':{'text':'''$DETAIL_MESSAGE'''}}))" 2>/dev/null || echo '{"msg_type":"text","content":{"text":"'"$DETAIL_MESSAGE"'"}}')
                fi
                
                echo "发送详细检查结果消息..."
                response_detail=$(curl -s -X POST "$FEISHU_WEBHOOK" \
                    -H "Content-Type: application/json" \
                    -d "$PAYLOAD_DETAIL"
                )
                
                echo "详细检查结果消息响应：$response_detail"
                
                # 短暂延迟
                sleep 1
            else
                echo "没有检查结果可发送"
            fi
            
            # 最后一条消息：总结
            MESSAGE2="详细检查结果发送完成

服务器运行状态良好！"
            
            if command -v jq &> /dev/null; then
                PAYLOAD2=$(jq -n --arg text "$MESSAGE2" '{"msg_type":"text","content":{"text":$text}}')
            else
                PAYLOAD2=$(python3 -c "import json; print(json.dumps({'msg_type':'text','content':{'text':'''$MESSAGE2'''}}))" 2>/dev/null || echo '{"msg_type":"text","content":{"text":"'"$MESSAGE2"'"}}')
            fi
            
            response2=$(curl -s -X POST "$FEISHU_WEBHOOK" \
                -H "Content-Type: application/json" \
                -d "$PAYLOAD2"
            )
            
            echo "总结消息响应：$response2"
            
            # 检查至少有一条消息发送成功
            if echo "$response1" | grep -q '"code":0' || echo "$response1" | grep -q '"StatusCode":0'; then
                echo "飞书消息发送成功（分批发送）"
                return 0
            else
                echo "飞书消息发送失败"
                return 1
            fi
        fi
    else
        echo "curl 命令不可用，无法发送飞书消息"
        return 1
    fi
}

# 主执行函数
main() {
    echo "开始服务器巡检..."
    echo "================================================"
    
    # 执行各项检查
    check_project_status
    echo "================================================"
    check_server_resources
    echo "================================================"
    check_docker_logs
    echo "================================================"
    check_additional_items
    echo "================================================"
    
    # 生成并发送巡检报告
    echo "生成巡检报告..."
    
    # 在巡检报告中添加 SSH 检查统计
    SSH_CHECK_STATUS=""
    if [[ "$SSH_CHECK_ENABLED" == "true" ]]; then
        if echo "$CHECK_RESULT" | grep -q "SSH 免密登录正常"; then
            SSH_CHECK_STATUS="✅ SSH 免密登录：正常"
        elif echo "$CHECK_RESULT" | grep -q "SSH 免密登录失败"; then
            SSH_CHECK_STATUS="❌ SSH 免密登录：失败"
        fi
    fi
    
    send_feishu_message "$CHECK_RESULT" "$SSH_CHECK_STATUS"
    
    echo "巡检完成！"
    
    # 如果有错误，返回错误码
    if (( ALERT_COUNT > 0 )); then
        return 1
    else
        return 0
    fi
}

# 执行主函数
main

# 退出状态码
exit $?
