#!/bin/bash

# 服务器自动化巡检脚本（已改造为使用通用通知服务）
# 每日 07:30 执行
# 检查项目运行状态、服务器资源使用、Docker 日志错误等
# 通过通用通知服务发送巡检结果

# ============================================
# 配置部分
# ============================================

# 通用通知服务配置
NOTIFICATION_SERVICE_URL="http://43.156.242.184:8766/api/v1"
NOTIFICATION_PROJECT="inspection"  # 项目标识

# Docker 容器监控配置
DOCKER_CONTAINER_PATTERN=""  # 空字符串表示监控所有容器

# 资源使用阈值
CPU_THRESHOLD=80      # CPU 使用率阈值 (%)
MEMORY_THRESHOLD=80   # 内存使用率阈值 (%)
DISK_THRESHOLD=80     # 磁盘使用率阈值 (%)

# Docker 清理配置
DOCKER_CLEANUP_ENABLED=true
DOCKER_CLEANUP_SCHEDULE="weekly"
DOCKER_CLEANUP_THRESHOLD=70
DOCKER_CLEANUP_TYPE="builder_only"

# SSH 免密登录检查配置
SSH_CHECK_ENABLED=true
SSH_HOST="43.156.242.184"
SSH_USER="root"
SSH_IDENTITY_FILE="/Users/yl/vscode/inspection_automation/docs/only.pem"

# ============================================
# 初始化变量
# ============================================
CHECK_RESULT=""
ALERT_COUNT=0

# ============================================
# 核心函数
# ============================================

# 函数：发送通知到通用服务（使用 Python 避免 JSON 转义问题）
send_notification() {
    local message="$1"
    local level="${2:-info}"  # info, warning, error
    
    echo "发送通知到通用服务..."
    
    # 使用 Python 发送（推荐，避免 JSON 转义问题）
    python3 << PYEOF
import requests
import json

url = "${NOTIFICATION_SERVICE_URL}/send"
data = {
    "project": "${NOTIFICATION_PROJECT}",
    "message": """${message}""",
    "type": "text",
    "level": "${level}"
}

try:
    response = requests.post(url, json=data, timeout=10)
    print(f"通知服务响应：{response.text}")
    if response.status_code == 200:
        print("✅ 发送成功")
    else:
        print(f"⚠️  状态码：{response.status_code}")
except Exception as e:
    print(f"❌ 发送失败：{e}")
PYEOF
}

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
    
    echo "================================================"
}

# 函数：检查服务器资源使用情况
check_resource_usage() {
    echo "检查服务器资源使用情况..."
    
    # 检查 CPU 使用率
    if command -v top &> /dev/null; then
        CPU_USAGE=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}' | cut -d'%' -f1 2>/dev/null || echo "0")
        # 如果 CPU_USAGE 为空或不是数字，尝试另一种方法
        if [[ -z "$CPU_USAGE" || ! "$CPU_USAGE" =~ ^[0-9.]+$ ]]; then
            CPU_USAGE=$(ps aux | awk '{sum+=$3} END {print sum/NR}' 2>/dev/null || echo "0")
        fi
        
        # 检查是否为有效数字
        if [[ "$CPU_USAGE" =~ ^[0-9.]+$ ]]; then
            CPU_INT=${CPU_USAGE%.*}
            if (( $(echo "$CPU_USAGE > $CPU_THRESHOLD" | bc -l 2>/dev/null || echo 0) )); then
                record_result "ERROR" "CPU 使用率过高：${CPU_USAGE}% (阈值：${CPU_THRESHOLD}%)"
            else
                record_result "OK" "CPU 使用率正常：${CPU_USAGE}%"
            fi
        else
            record_result "ERROR" "无法获取 CPU 使用率"
        fi
    fi
    
    # 检查内存使用率
    if command -v free &> /dev/null; then
        MEMORY_USAGE=$(free | grep Mem | awk '{printf("%.1f", $3/$2 * 100.0)}' 2>/dev/null || echo "0")
        
        if [[ "$MEMORY_USAGE" =~ ^[0-9.]+$ ]]; then
            MEMORY_INT=${MEMORY_USAGE%.*}
            if (( $(echo "$MEMORY_USAGE > $MEMORY_THRESHOLD" | bc -l 2>/dev/null || echo 0) )); then
                record_result "ERROR" "内存使用率过高：${MEMORY_USAGE}% (阈值：${MEMORY_THRESHOLD}%)"
            else
                record_result "OK" "内存使用率正常：${MEMORY_USAGE}%"
            fi
        else
            record_result "ERROR" "无法获取内存使用率"
        fi
    fi
    
    # 检查磁盘使用率
    if command -v df &> /dev/null; then
        DISK_USAGE=$(df -h / | tail -1 | awk '{print $5}' | sed 's/%//')
        
        if [[ "$DISK_USAGE" =~ ^[0-9]+$ ]]; then
            if (( DISK_USAGE > DISK_THRESHOLD )); then
                record_result "ERROR" "磁盘使用率过高：${DISK_USAGE}% (阈值：${DISK_THRESHOLD}%)"
            else
                record_result "OK" "磁盘使用率正常：${DISK_USAGE}%"
            fi
        else
            record_result "ERROR" "无法获取磁盘使用率"
        fi
    fi
    
    echo "================================================"
}

# 函数：检查 Docker 日志错误
check_docker_logs() {
    echo "检查 Docker 日志错误..."
    
    if command -v docker &> /dev/null; then
        # 检查所有容器的日志
        if [[ -z "$DOCKER_CONTAINER_PATTERN" ]]; then
            CONTAINERS=$(docker ps --format "{{.Names}}" 2>/dev/null)
        else
            CONTAINERS=$(docker ps --format "{{.Names}}" 2>/dev/null | grep -i "$DOCKER_CONTAINER_PATTERN")
        fi
        
        ERROR_COUNT=0
        while IFS= read -r CONTAINER_NAME; do
            # 检查最近 100 行日志中的错误
            CONTAINER_ERRORS=$(docker logs --tail 100 "$CONTAINER_NAME" 2>&1 | grep -ci "error" || echo 0)
            if (( CONTAINER_ERRORS > 0 )); then
                ERROR_COUNT=$((ERROR_COUNT + CONTAINER_ERRORS))
                record_result "ERROR" "容器 $CONTAINER_NAME 日志中发现 ${CONTAINER_ERRORS} 个错误"
            fi
        done <<< "$CONTAINERS"
        
        if (( ERROR_COUNT == 0 )); then
            record_result "OK" "所有容器日志无错误"
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
    
    echo "================================================"
}

# 函数：专业运维角度的额外巡检项
check_additional_items() {
    echo "执行额外巡检项..."
    
    # 检查网络连接状态
    if command -v ping &> /dev/null; then
        if ping -c 3 baidu.com &> /dev/null; then
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
    
    echo "================================================"
}

# 函数：检查并清理 Docker 空间
check_and_cleanup_docker() {
    if [[ "$DOCKER_CLEANUP_ENABLED" != "true" ]]; then
        echo "Docker 自动清理已禁用，跳过"
        return 0
    fi
    
    echo "检查 Docker 空间清理..."
    
    # 检查磁盘使用率
    DISK_USAGE=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
    
    # 检查是否达到清理阈值
    if (( DISK_USAGE > DOCKER_CLEANUP_THRESHOLD )); then
        echo "磁盘使用率 ${DISK_USAGE}% > ${DOCKER_CLEANUP_THRESHOLD}%，触发清理"
        
        # 执行清理
        if command -v docker &> /dev/null; then
            echo "清理 Docker 构建缓存..."
            docker builder prune -af 2>/dev/null
            
            if [[ "$DOCKER_CLEANUP_TYPE" == "builder_and_images" ]]; then
                echo "清理悬空镜像..."
                docker image prune -af 2>/dev/null
            fi
            
            echo "清理已停止的容器..."
            docker container prune -f 2>/dev/null
            
            record_result "OK" "Docker 空间清理完成"
        else
            record_result "ERROR" "Docker 命令不可用，无法清理"
        fi
    else
        echo "磁盘使用率 ${DISK_USAGE}% < ${DOCKER_CLEANUP_THRESHOLD}%，跳过清理"
        record_result "OK" "Docker 空间充足，无需清理"
    fi
}

# 函数：生成并发送报告
send_report() {
    echo "生成并发送巡检报告..."
    
    # 构建报告内容
    CURRENT_TIME=$(date '+%Y-%m-%d %H:%M:%S')
    ERROR_COUNT=$(echo "$CHECK_RESULT" | grep -c "❌")
    OK_COUNT=$(echo "$CHECK_RESULT" | grep -c "✅")
    
    # 构建详细报告
    if (( ERROR_COUNT > 0 )); then
        # 有错误，发送详细报告
        REPORT="🚨 服务器巡检报告

时间：${CURRENT_TIME}
检查项总数：$((ERROR_COUNT + OK_COUNT))
正常项：${OK_COUNT}
异常项：${ERROR_COUNT}

检查详情：
${CHECK_RESULT}
"
        # 发送告警通知（error 级别）
        send_notification "$REPORT" "error"
    else
        # 一切正常，发送简洁报告
        REPORT="✅ 服务器巡检报告 (${CURRENT_TIME})

检查项总数：$((ERROR_COUNT + OK_COUNT))
检查结果：全部正常

服务器运行状态良好✨"
        
        # 发送正常通知（info 级别）
        send_notification "$REPORT" "info"
    fi
    
    echo "报告发送完成"
}

# ============================================
# 主程序
# ============================================
main() {
    echo "================================================"
    echo "服务器自动化巡检开始"
    echo "时间：$(date '+%Y-%m-%d %H:%M:%S')"
    echo "================================================"
    
    # 执行各项检查
    check_project_status
    check_resource_usage
    check_docker_logs
    check_additional_items
    
    # 发送报告
    send_report
    
    echo "================================================"
    echo "服务器自动化巡检完成"
    echo "================================================"
}

# 执行主程序
main
