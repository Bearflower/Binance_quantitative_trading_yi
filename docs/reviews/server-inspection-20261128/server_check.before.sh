#!/bin/bash

# 服务器自动化巡检脚本
# 每日 07:30 执行
# 检查项目运行状态、服务器资源使用、Docker 日志错误等
# 通过飞书 webhook 发送巡检结果

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 加载配置文件
CONFIG_FILE="${SCRIPT_DIR}/server_check.conf"
if [[ -f "$CONFIG_FILE" ]]; then
    source "$CONFIG_FILE"
    echo "已加载配置文件：$CONFIG_FILE"
else
    echo "❌ 错误：配置文件不存在，请创建配置文件："
    echo "   1. 复制配置模板：cp ${CONFIG_FILE}.example ${CONFIG_FILE}"
    echo "   2. 编辑配置文件：vim ${CONFIG_FILE}"
    echo "   3. 参考文档：docs/巡检脚本配置说明.md"
    exit 1
fi

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
    elif [[ "$status" == "WARN" ]]; then
        # 提醒项：不阻断、不影响退出码，仅在报告中标注偏高
        echo "⚠️ $message"
        CHECK_RESULT="${CHECK_RESULT}⚠️ $message
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
        # 仅排除完整身份和停止状态均已核验的退役实例；失败时明确告警。
        if ! CONTAINERS=$(python3 /root/trading_system/ai_tuner/ops/container_inventory.py --names); then
            record_result "ERROR" "容器清单核验失败，不能确认当前服务状态"
            return 1
        fi
        if [[ -n "$DOCKER_CONTAINER_PATTERN" ]]; then
            CONTAINERS=$(printf '%s\n' "$CONTAINERS" | grep -i "$DOCKER_CONTAINER_PATTERN")
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
                CONTAINER_STATUS=$(docker inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null)
                CONTAINER_HEALTH=$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "unknown")
                
                if [[ "$CONTAINER_STATUS" == "running" ]]; then
                    if [[ "$CONTAINER_NAME" == "trading_system-hrs-research-observer" && "$CONTAINER_HEALTH" != "unhealthy" ]]; then
                        if timeout 10s /usr/bin/python3 -c 'import sys; sys.path.insert(0, "/root/trading_system/ai_tuner/ops"); import hrs_observation as h; _, issues = h.quotes(h.dt.datetime.now(h.digest_outbox.TZ)); sys.exit(bool(issues))' >/dev/null 2>&1; then
                            record_result "OK" "HRS 盘口观察器读库心跳正常：$CONTAINER_NAME"
                        else
                            record_result "ERROR" "HRS 盘口观察器心跳或待采状态异常：$CONTAINER_NAME"
                        fi
                    elif [[ "$CONTAINER_HEALTH" == "healthy" || "$CONTAINER_HEALTH" == "unknown" ]]; then
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
    
    # 检查内存使用率（增强版）
    if command -v free &> /dev/null; then
        check_memory_enhanced
    else
        record_result "ERROR" "无法获取内存使用率"
    fi
    
    # 检查磁盘使用率（遍历块设备分区，按精确占用率取最高的一个分区）
    if command -v df &> /dev/null; then
        DISK_USAGE="0"
        DISK_MOUNT=""
        DISK_FREE_GB="0"
        while read -r DF_FS DF_TOTAL_KB DF_USED_KB DF_AVAIL_KB DF_MOUNT; do
            [[ "$DF_TOTAL_KB" =~ ^[0-9]+$ ]] || continue
            (( DF_TOTAL_KB > 0 )) || continue
            PCT=$(awk -v u="$DF_USED_KB" -v t="$DF_TOTAL_KB" 'BEGIN{printf "%.1f", u * 100 / t}')
            if (( $(echo "$PCT > $DISK_USAGE" | bc -l) )); then
                DISK_USAGE="$PCT"
                DISK_MOUNT="$DF_MOUNT"
                DISK_FREE_GB=$(awk -v a="$DF_AVAIL_KB" 'BEGIN{printf "%.0f", a / 1048576}')
            fi
        done < <(df -P 2>/dev/null | awk 'NR > 1 && $1 ~ /^\/dev\// && $1 !~ /tmpfs|devtmpfs/ {print $1, $2, $3, $4, $6}')

        DISK_WARN_LEVEL=${DISK_WARN_THRESHOLD:-70}
        if (( $(echo "$DISK_USAGE >= $DISK_THRESHOLD" | bc -l) )); then
            record_result "ERROR" "磁盘使用率过高：${DISK_USAGE}% (${DISK_MOUNT}，剩余 ${DISK_FREE_GB}GB，阈值 ${DISK_THRESHOLD}%)"
        elif (( $(echo "$DISK_USAGE >= $DISK_WARN_LEVEL" | bc -l) )); then
            record_result "WARN" "磁盘使用率偏高：${DISK_USAGE}% (${DISK_MOUNT}，剩余 ${DISK_FREE_GB}GB，预警阈值 ${DISK_WARN_LEVEL}%，告警阈值 ${DISK_THRESHOLD}%)"
        else
            record_result "OK" "磁盘使用率正常：${DISK_USAGE}% (${DISK_MOUNT}，剩余 ${DISK_FREE_GB}GB)"
        fi
    else
        record_result "ERROR" "无法获取磁盘使用率"
    fi
}

# 函数：增强版内存检查（包含趋势分析、Top 进程、泄漏检测）
check_memory_enhanced() {
    echo "执行增强版内存检查..."
    
    # 获取当前内存使用情况
    MEM_TOTAL=$(free -m | awk '/Mem:/ {print $2}')
    MEM_USED=$(free -m | awk '/Mem:/ {print $3}')
    MEM_FREE=$(free -m | awk '/Mem:/ {print $4}')
    MEM_AVAILABLE=$(free -m | awk '/Mem:/ {print $7}')
    MEM_BUFFERS=$(free -m | awk '/Mem:/ {print $6}')
    
    if [[ -z "$MEM_TOTAL" || "$MEM_TOTAL" == "0" ]]; then
        record_result "ERROR" "无法获取内存使用率"
        return 1
    fi
    
    # 计算使用率
    MEM_USAGE=$(echo "scale=2; $MEM_USED * 100 / $MEM_TOTAL" | bc)
    MEM_USAGE_PERCENT=$(printf "%.2f" "$MEM_USAGE")
    
    # 1. 基础内存检查
    if (( $(echo "$MEM_USAGE > $MEMORY_THRESHOLD" | bc -l) )); then
        record_result "ERROR" "内存使用率过高：${MEM_USAGE_PERCENT}% (阈值：${MEMORY_THRESHOLD}%)"
        record_result "ERROR" "内存详情：总计 ${MEM_TOTAL}MB | 已用 ${MEM_USED}MB | 空闲 ${MEM_FREE}MB | 可用 ${MEM_AVAILABLE}MB | 缓存 ${MEM_BUFFERS}MB"
    else
        record_result "OK" "内存使用率正常：${MEM_USAGE_PERCENT}%"
        record_result "OK" "内存详情：总计 ${MEM_TOTAL}MB | 已用 ${MEM_USED}MB | 空闲 ${MEM_FREE}MB | 可用 ${MEM_AVAILABLE}MB"
    fi
    
    # 2. 记录内存使用历史（用于趋势分析）
    record_memory_history "$MEM_TOTAL" "$MEM_USED" "$MEM_USAGE_PERCENT"
    
    # 3. 显示占用内存最大的前 5 个进程
    show_top_memory_processes
    
    # 4. 内存泄漏检测
    if [[ "$MEMORY_LEAK_CHECK_ENABLED" == "true" ]]; then
        check_memory_leak
    fi
    
    # 5. 内存使用趋势分析
    analyze_memory_trend
}

# 函数：记录内存使用历史
record_memory_history() {
    local total="$1"
    local used="$2"
    local usage="$3"
    
    # 尝试创建历史记录目录，如果失败则使用临时目录
    if ! mkdir -p "$MEMORY_HISTORY_DIR" 2>/dev/null; then
        # 如果无法创建系统目录，使用临时目录
        MEMORY_HISTORY_DIR="/tmp/memory_monitor"
        mkdir -p "$MEMORY_HISTORY_DIR" 2>/dev/null
        if [[ $? -ne 0 ]]; then
            echo "警告：无法创建历史记录目录，内存历史记录功能将被禁用"
            return 1
        fi
        echo "注：使用备用目录存储内存历史：$MEMORY_HISTORY_DIR"
    fi
    
    local history_file="$MEMORY_HISTORY_DIR/memory_$(date +%Y%m%d).csv"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    # 首次创建时添加表头
    if [[ ! -f "$history_file" ]]; then
        echo "timestamp,total_mb,used_mb,usage_percent,available_mb" > "$history_file"
        chmod 644 "$history_file" 2>/dev/null
    fi
    
    # 记录当前数据
    echo "${timestamp},${total},${used},${usage},${MEM_AVAILABLE}" >> "$history_file"
    
    # 清理旧数据（超过保留天数的文件）
    find "$MEMORY_HISTORY_DIR" -name "memory_*.csv" -mtime +$MEMORY_HISTORY_DAYS -delete 2>/dev/null
}

# 函数：显示占用内存最大的前 5 个进程
show_top_memory_processes() {
    echo "获取内存占用 Top 5 进程..."
    
    if command -v ps &> /dev/null; then
        # 获取 Top 5 内存占用进程
        TOP_PROCESSES=$(ps aux --sort=-%mem 2>/dev/null | head -n 6 | tail -n 5)
        
        if [[ -n "$TOP_PROCESSES" ]]; then
            local process_info="内存占用 Top 5 进程：\n"
            local rank=1
            
            while IFS= read -r line; do
                if [[ -n "$line" ]]; then
                    local pid=$(echo "$line" | awk '{print $2}')
                    local mem_percent=$(echo "$line" | awk '{print $4}')
                    local mem_rss=$(echo "$line" | awk '{print $6}')
                    local command=$(echo "$line" | awk '{for(i=11;i<=NF;i++) printf $i" "; print ""}')
                    
                    # 转换 RSS 为 MB
                    local mem_mb=$(echo "scale=2; $mem_rss / 1024" | bc)
                    
                    process_info="${process_info}${rank}. PID: ${pid} | 内存：${mem_percent}% (${mem_mb}MB) | 命令：${command}\n"
                    rank=$((rank + 1))
                fi
            done <<< "$TOP_PROCESSES"
            
            record_result "OK" "$process_info"
        else
            record_result "OK" "无法获取进程内存占用信息"
        fi
    fi
}

# 函数：内存泄漏检测
check_memory_leak() {
    echo "执行内存泄漏检测..."
    
    local history_file="$MEMORY_HISTORY_DIR/memory_$(date +%Y%m%d).csv"
    
    if [[ ! -f "$history_file" ]]; then
        record_result "OK" "内存泄漏检测：暂无历史数据（首次运行）"
        return 0
    fi
    
    # 读取最近的内存使用记录（最近 10 次）
    local recent_records=$(tail -n 11 "$history_file" | head -n 10)
    local record_count=$(echo "$recent_records" | wc -l)
    
    if (( record_count < 5 )); then
        record_result "OK" "内存泄漏检测：数据不足（需要至少 5 次记录）"
        return 0
    fi
    
    # 分析内存使用趋势
    local prev_usage=0
    local continuous_increase=0
    local total_increase=0
    local first_usage=0
    local last_usage=0
    local increase_count=0
    
    while IFS=',' read -r timestamp total used usage available; do
        # 跳过表头
        if [[ "$timestamp" == "timestamp" ]]; then
            continue
        fi
        
        # 转换为整数比较
        usage_int=${usage%.*}
        
        if (( first_usage == 0 )); then
            first_usage=$usage_int
        fi
        
        last_usage=$usage_int
        
        if (( prev_usage > 0 )); then
            local diff=$(echo "$usage - $prev_usage" | bc)
            
            # 检查是否增长
            if (( $(echo "$diff > 0" | bc -l) )); then
                continuous_increase=$((continuous_increase + 1))
                increase_count=$((increase_count + 1))
                total_increase=$(echo "$total_increase + $diff" | bc)
                
                # 检查是否超过阈值百分比
                if (( $(echo "$diff > $MEMORY_LEAK_PERCENT_THRESHOLD" | bc -l) )); then
                    record_result "ERROR" "内存泄漏警告：内存使用率单次增长过大 (${diff}%) - 时间：${timestamp}"
                fi
            else
                continuous_increase=0
            fi
        fi
        
        prev_usage=$usage_int
    done <<< "$recent_records"
    
    # 判断是否存在内存泄漏
    local leak_detected=false
    local leak_reason=""
    
    # 条件 1：连续增长超过阈值次数
    if (( continuous_increase >= MEMORY_LEAK_THRESHOLD )); then
        leak_detected=true
        leak_reason="内存使用率连续 ${continuous_increase} 次增长"
    fi
    
    # 条件 2：总体增长趋势明显（10 次记录内增长超过 10%）
    local total_growth=$((last_usage - first_usage))
    if (( total_growth > 10 )); then
        leak_detected=true
        leak_reason="${leak_reason:+$leak_reason; }10 次记录内内存增长 ${total_growth}%"
    fi
    
    # 条件 3：增长频率过高
    if (( increase_count >= 8 && record_count >= 10 )); then
        leak_detected=true
        leak_reason="${leak_reason:+$leak_reason; }10 次记录中 ${increase_count} 次增长（增长频率过高）"
    fi
    
    if [[ "$leak_detected" == "true" ]]; then
        record_result "ERROR" "⚠️  内存泄漏检测：发现疑似内存泄漏！\n   原因：${leak_reason}\n   建议：检查应用内存使用情况，重启相关服务"
    else
        record_result "OK" "内存泄漏检测：未发现明显内存泄漏（连续增长：${continuous_increase}次，总增长：${total_growth}%）"
    fi
}

# 函数：内存使用趋势分析
analyze_memory_trend() {
    echo "分析内存使用趋势..."
    
    local today_file="$MEMORY_HISTORY_DIR/memory_$(date +%Y%m%d).csv"
    local yesterday=$(date -d "yesterday" +%Y%m%d 2>/dev/null || date -v-1d +%Y%m%d 2>/dev/null)
    local yesterday_file="$MEMORY_HISTORY_DIR/memory_${yesterday}.csv"
    
    local trend_info=""
    
    # 今日趋势分析
    if [[ -f "$today_file" ]]; then
        local today_records=$(tail -n +2 "$today_file" | wc -l)
        
        if (( today_records >= 2 )); then
            local today_first=$(tail -n +2 "$today_file" | head -n 1 | cut -d',' -f4)
            local today_last=$(tail -n 1 "$today_file" | cut -d',' -f4)
            local today_change=$(echo "$today_last - $today_first" | bc)
            local today_change_percent=$(printf "%.2f" "$today_change")
            
            if (( $(echo "$today_change > 0" | bc -l) )); then
                trend_info="今日趋势：上升 ${today_change_percent}%\n"
            elif (( $(echo "$today_change < 0" | bc -l) )); then
                trend_info="今日趋势：下降 ${today_change_percent#-}%\n"
            else
                trend_info="今日趋势：平稳\n"
            fi
            
            # 计算今日平均值
            local today_avg=$(tail -n +2 "$today_file" | cut -d',' -f4 | awk '{sum+=$1} END {if(NR>0) printf "%.2f", sum/NR; else print "0"}')
            trend_info="${trend_info}今日平均使用率：${today_avg}%\n"
            
            # 计算今日峰值
            local today_max=$(tail -n +2 "$today_file" | cut -d',' -f4 | sort -n | tail -1)
            trend_info="${trend_info}今日峰值：${today_max}%"
        else
            trend_info="今日数据不足，无法分析趋势"
        fi
        
        record_result "OK" "📊 内存趋势分析：\n   ${trend_info}"
    fi
    
    # 与昨日对比
    if [[ -f "$yesterday_file" ]]; then
        local yesterday_avg=$(tail -n +2 "$yesterday_file" | cut -d',' -f4 | awk '{sum+=$1} END {if(NR>0) printf "%.2f", sum/NR; else print "0"}')
        local today_avg=$(tail -n +2 "$today_file" | cut -d',' -f4 | awk '{sum+=$1} END {if(NR>0) printf "%.2f", sum/NR; else print "0"}')
        local day_over_day_change=$(echo "$today_avg - $yesterday_avg" | bc)
        local day_over_day_percent=$(printf "%.2f" "$day_over_day_change")
        
        if (( $(echo "$day_over_day_change > 0" | bc -l) )); then
            record_result "OK" "环比昨日：上升 ${day_over_day_percent}%（昨日平均：${yesterday_avg}%）"
        elif (( $(echo "$day_over_day_change < 0" | bc -l) )); then
            record_result "OK" "环比昨日：下降 ${day_over_day_percent#-}%（昨日平均：${yesterday_avg}%）"
        else
            record_result "OK" "环比昨日：持平（昨日平均：${yesterday_avg}%）"
        fi
    fi
}

# 只忽略传输层 DEBUG 级 -2013 查单记录。最终失败仍由调用方 ERROR/锁定告警报告。
# 按行首级别和具体事件限定，不能按错误码全局过滤，也不能隐藏引用此记录的 ERROR。
filter_expected_order_lookup_debug() {
    grep -vE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} \[debug[[:space:]]*\][[:space:]]+遇到不可重试的错误，立即抛出[[:space:]]+error=\[-2013\][[:space:]]+Order does not exist\.[[:space:]]+error_code=-2013[[:space:]]+function=_request_once([[:space:]]|$)'
}

# 函数：检查 Docker 日志错误
check_docker_logs() {
    echo "检查 Docker 日志错误..."
    
    if command -v docker &> /dev/null; then
        # 获取所有容器（如果配置了模式则过滤）
        # 仅排除完整身份和停止状态均已核验的退役实例；失败时明确告警。
        if ! CONTAINERS=$(python3 /root/trading_system/ai_tuner/ops/container_inventory.py --names); then
            record_result "ERROR" "容器清单核验失败，不能确认当前服务状态"
            return 1
        fi
        if [[ -n "$DOCKER_CONTAINER_PATTERN" ]]; then
            CONTAINERS=$(printf '%s\n' "$CONTAINERS" | grep -i "$DOCKER_CONTAINER_PATTERN")
        fi
        
        if [[ -z "$CONTAINERS" ]]; then
            if [[ -n "$DOCKER_CONTAINER_PATTERN" ]]; then
                record_result "ERROR" "未找到包含 '$DOCKER_CONTAINER_PATTERN' 的 Docker 容器，无法检查日志"
            else
                record_result "ERROR" "未找到任何 Docker 容器，无法检查日志"
            fi
        else
            # ANSI 颜色码前缀：ai-tuner 等容器使用 structlog 彩色控制台渲染器，
            # 原始日志字节形如 "^[[36merror^[[0m=^[[35m0"，会同时污染关键字匹配与告警内容，故统一先剥离
            local ESC_CHAR=$'\033'
            # 兼容两套生产 PostgreSQL 日志前缀以及默认的时间戳和 PID 前缀。
            local pg_timestamp='^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:.]+ [^[:space:]]+ \[[0-9]+\] '
            local pg_session='[^[:space:]]*@[^[:space:]]*[[:space:]]+'
            local pg_remote='(\[local\]|[^[:space:]]+\([0-9]+\))[[:space:]]+'
            local pg_prefix="${pg_timestamp}(${pg_session}(${pg_remote})?)?"
            local known_errors='column "date" does not exist|column "started_at" does not exist|column "finished_at" does not exist|column "trade_date" does not exist|relation "klines" does not exist|relation "stock_pool" does not exist|relation "index_klines" does not exist|column reference "code" is ambiguous|aggregate functions are not allowed in GROUP BY|column "[a-z0-9]+%%" does not exist|column "d" does not exist|column "is_active" does not exist|column "active" does not exist|must be superuser or a member of the pg_write_server_files role to COPY to a file|syntax error at or near "|error=[0-9]+( |$)|error=(TimeoutError|ReadTimeout|ConnectTimeout|ConnectError|ConnectionError|ClientConnectorError|ServerDisconnectedError)( |$)|"level": ?"info".*"error_code"|"error_code".*"level": ?"info"|level=info.*error_code|error_code.*level=info'
            local container_image ignore_pattern
            # 检查每个匹配容器的日志
            while IFS= read -r CONTAINER_NAME; do
                # 检查容器是否存在
                if docker ps -a --filter "name=$CONTAINER_NAME" | grep -q "$CONTAINER_NAME"; then
                    # 检查最近 24 小时的错误日志
                    # 已知无害错误的过滤清单：
                    # ① 手工 psql 会话（结构性过滤）：连接标记为「用户@应用名 [local]」，
                    #    [local]=unix socket，通常由容器内的 psql 诊断产生；
                    #    应用容器走 TCP，日志远端字段是「IP(端口)」（如 172.18.0.9(42898)）而非 [local]。
                    #    手工排查时猜错列名（perf_window_start/intent_id/entry_price/day 等）或误用窗口函数
                    #    产生的普通 ERROR 按连接来源过滤，FATAL/PANIC 保留。
                    #    ⚠️ 本条依赖目标 postgres 的 log_line_prefix 含 %u/%d 与 %h：2026-10-19 发现
                    #    common_service_postgres 原为默认「%m [%p]」，ERROR 行不含 [local] 致本条形同失效，
                    #    已 ALTER SYSTEM 设为 '%m [%p] %q%u@%d/%a %h '（context=sighup，pg_reload_conf 生效；
                    #    回退：ALTER SYSTEM RESET log_line_prefix + pg_reload_conf）。
                    # ② 历史枚举项：上述结构性过滤之前逐条加入的列名/关系名，保留以兼容 TCP 来源的已知无害项。
                    # ③ PostgreSQL 按行首日志级别筛选，SQL、参数和提示里的错误字样不计入。
                    # error=<数字> 是 structlog 结构化日志的计数键（如「周度调优完成 ... error=0」info 级汇总行），
                    # 并非错误事件；真实异常日志的 error= 值恒为 str(异常)（带引号），故不会误伤
                    # ④ info 级且仅通过结构化字段 error_code 命中「error」的行一律忽略：项目内 info 级
                    #    error_code 均为「幂等/良性竞态」语义（如 -2011 入场订单已不存在、ReduceOnly 被拒
                    #    说明已无持仓），真实异常统一走 warning/error 级，故按 level 判定而非枚举错误码。
                    #    同时兼容两种渲染：JSON（"level": "info"，JSONRenderer）与控制台（level=info，ConsoleRenderer）
                    # ⑤ 仅含「裸类名」的瞬时网络异常字段 error=<类名>（如 error=TimeoutError，loguru 控制台
                    #    「获取市值网络异常，准备重试」warning 行）：值为类名时无消息体，属可重试的临时网络抖动；
                    #    真实异常会渲染为 error=类名('具体消息')，带括号不会被本条命中，故不会漏报
                    container_image=$(docker inspect --format '{{.Config.Image}}' "$CONTAINER_NAME" 2>/dev/null)
                    case "${container_image##*/}" in
                        postgres|postgres:*|postgres@*)
                            ignore_pattern="${pg_timestamp}${pg_session}\[local\][[:space:]]+ERROR:|${pg_prefix}ERROR:.*(${known_errors})"
                            if ! ALL_ERRORS=$(
                                docker logs --since 24h "$CONTAINER_NAME" 2>&1 | sed "s/${ESC_CHAR}\[[0-9;]*[a-zA-Z]//g" | grep -E "${pg_prefix}(ERROR|FATAL|PANIC):[[:space:]]"
                                status=("${PIPESTATUS[@]}")
                                (( status[0] == 0 && status[1] == 0 && status[2] <= 1 ))
                            ); then
                                record_result "ERROR" "容器 $CONTAINER_NAME Docker 日志读取失败"
                                continue
                            fi
                            ;;
                        *)
                            ignore_pattern='@[^[:space:]]+ \[local\] |LOG:  (statement|duration|execute|parameters|disconnection|connection authorized)|'"$known_errors"
                            # HTTP access records use status codes. URL text such as error.aspx is not severity.
                            # Include all HTTP 5xx responses even when their URL contains no error keyword.
                            ALL_ERRORS=$(docker logs --since 24h "$CONTAINER_NAME" 2>&1 | sed "s/${ESC_CHAR}\[[0-9;]*[a-zA-Z]//g" | grep -Ei 'error|^INFO:[[:space:]]+[^[:space:]]+[[:space:]]+-[[:space:]]+"[^"]* HTTP/[0-9.]+"[[:space:]]+5[0-9]{2}([[:space:]]|$)' | grep -vE '^INFO:[[:space:]]+[^[:space:]]+[[:space:]]+-[[:space:]]+"[^"]* HTTP/[0-9.]+"[[:space:]]+[1-4][0-9]{2}([[:space:]]|$)')
                            ;;
                    esac
                    if [[ -n "$ALL_ERRORS" ]]; then
                        # 过滤已知无害错误
                        RELEVANT_ERRORS=$(echo "$ALL_ERRORS" | grep -viE "$ignore_pattern")
                        RELEVANT_ERRORS=$(echo "$RELEVANT_ERRORS" | filter_expected_order_lookup_debug)
                        # 警告保留为提醒；异常类名中的 Error 不提升事件级别。
                        local classified_errors classified_warnings
                        if classified_errors=$(printf '%s\n' "$RELEVANT_ERRORS" | python3 /root/trading_system/ai_tuner/ops/inspection_log_severity.py --level error) &&
                           classified_warnings=$(printf '%s\n' "$RELEVANT_ERRORS" | python3 /root/trading_system/ai_tuner/ops/inspection_log_severity.py --level warning); then
                            RELEVANT_ERRORS="$classified_errors"
                            if [[ -n "$classified_warnings" ]]; then
                                record_result "WARN" "容器 $CONTAINER_NAME 近24小时有 $(printf '%s\n' "$classified_warnings" | grep -c .) 条警告（与错误分列）：$(printf '%s\n' "$classified_warnings" | tail -3 | tr '\n' ';')"
                            fi
                        else
                            record_result "ERROR" "容器 $CONTAINER_NAME 日志级别解析失败，保留原始错误候选"
                        fi
                        local resolved_position_lock_count=0
                        if [[ "$CONTAINER_NAME" == "trading_system-grid-executor" ]]; then
                            local position_lock_line="ERROR:__main__:grid executor locked: position does not match journal fills"
                            local position_lock_count
                            position_lock_count=$(printf '%s\n' "$RELEVANT_ERRORS" | grep -Fxc "$position_lock_line" || true)
                            if (( position_lock_count > 0 )) && python3 /root/inspection/grid_recovered_lock.py "$position_lock_count"; then
                                RELEVANT_ERRORS=$(printf '%s\n' "$RELEVANT_ERRORS" | grep -Fxv "$position_lock_line" || true)
                                resolved_position_lock_count=$position_lock_count
                                record_result "OK" "容器 $CONTAINER_NAME 有 $position_lock_count 条经核验已恢复的仓位对账锁定记录"
                            fi
                        fi
                        ALL_ERROR_COUNT=$(echo "$ALL_ERRORS" | grep -c . )
                        RELEVANT_ERROR_COUNT=$(echo "$RELEVANT_ERRORS" | grep -c . )
                    else
                        local resolved_position_lock_count=0
                        RELEVANT_ERRORS=""
                        ALL_ERROR_COUNT=0
                        RELEVANT_ERROR_COUNT=0
                    fi

                    if (( RELEVANT_ERROR_COUNT > 0 )); then
                        # 获取最近 5 条相关错误日志
                        RECENT_ERRORS=$(echo "$RELEVANT_ERRORS" | tail -5 | tr '\n' '; ')
                        record_result "ERROR" "容器 $CONTAINER_NAME 日志中发现 ${RELEVANT_ERROR_COUNT} 个错误(共${ALL_ERROR_COUNT})：${RECENT_ERRORS}"
                    elif (( resolved_position_lock_count == 0 )); then
                        if (( ALL_ERROR_COUNT > 0 )); then
                            record_result "OK" "容器 $CONTAINER_NAME 日志候选 ${ALL_ERROR_COUNT} 条；无 ERROR 级事件，警告已单独列出"
                        else
                            record_result "OK" "容器 $CONTAINER_NAME 日志无错误"
                        fi
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
    echo "================================================"
    
    # Docker + 磁盘深度巡检（磁盘占用分解、大日志、无用卷）
    check_disk_docker_deep
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

        # 磁盘水位超过阈值时自动清理悬空镜像（兜底：部署 workflow 已内置部署后清理）
        # 只删 <none> 悬空镜像（docker image prune 不带 -a），有 tag 的镜像保留用于回滚
        local disk_usage_now
        disk_usage_now=$(df -h / | awk 'NR==2 {gsub(/%/,""); print $5}')
        if (( disk_usage_now > DANGLING_IMAGE_CLEANUP_DISK_THRESHOLD )); then
            echo "磁盘使用率 ${disk_usage_now}% 超过阈值 ${DANGLING_IMAGE_CLEANUP_DISK_THRESHOLD}%，自动清理悬空镜像..."
            if docker image prune -f &> /dev/null; then
                record_result "OK" "磁盘 ${disk_usage_now}% 超阈值，已自动清理 ${DANGLING_IMAGES_COUNT} 个悬空镜像"
            else
                record_result "ERROR" "悬空镜像自动清理失败，请手动执行：docker image prune -f"
            fi
        else
            record_result "OK" "悬空镜像清理提示：运行 /root/inspection/cleanup_dangling_images.sh 可清理悬空镜像"
        fi
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

# 函数：Docker + 磁盘深度巡检
check_disk_docker_deep() {
    echo "执行 Docker + 磁盘深度巡检..."
    
    # ========== 1. Docker 全盘占用分解 ==========
    echo "--- Docker 全盘占用分解 ---"
    if command -v docker &> /dev/null; then
        DOCKER_DF=$(docker system df 2>/dev/null)
        if [[ -n "$DOCKER_DF" ]]; then
            # 提取 Images / Containers / Local Volumes / Build Cache 的 SIZE 和 RECLAIMABLE
            DOCKER_IMAGES_INFO=$(echo "$DOCKER_DF" | grep "^Images" | awk '{print $4 " (" $6 ")"}')
            DOCKER_VOLUMES_INFO=$(echo "$DOCKER_DF" | grep "^Local Volumes" | awk '{print $4 " (" $6 ")"}')
            DOCKER_CACHE_INFO=$(echo "$DOCKER_DF" | grep "^Build Cache" | awk '{print $4}')
            DOCKER_TOTAL_RECLAIM=$(echo "$DOCKER_DF" | grep "^Images" -A 3 | awk '{sum+=$6} END {print sum}' | grep -oE '^[0-9.]+')
            
            record_result "OK" "Docker 磁盘分解：Images ${DOCKER_IMAGES_INFO} | Volumes ${DOCKER_VOLUMES_INFO} | Build Cache ${DOCKER_CACHE_INFO}"
        fi
    fi
    
    # ========== 2. /var/lib/docker 独立占比 ==========
    echo "--- /var/lib/docker 占比 ---"
    if [[ -d /var/lib/docker ]]; then
        DOCKER_SIZE=$(du -sh /var/lib/docker 2>/dev/null | cut -f1)
        DOCKER_VAR_USAGE=$(du -sh /var 2>/dev/null | cut -f1)
        record_result "OK" "/var/lib/docker: ${DOCKER_SIZE} | /var: ${DOCKER_VAR_USAGE}"
    fi
    
    # ========== 3. 大日志检测（超过 50MB 的容器 json.log）==========
    echo "--- 大日志检测（阈值：${LOG_SIZE_WARN_THRESHOLD:-50}MB）---"
    LOG_THRESHOLD=${LOG_SIZE_WARN_THRESHOLD:-50}  # 可在配置文件中覆盖
    if command -v find &> /dev/null; then
        BIG_LOGS=$(find /var/lib/docker/containers -name "*-json.log" -size +${LOG_THRESHOLD}M -printf "%s %p\n" 2>/dev/null | sort -rn)
        if [[ -n "$BIG_LOGS" ]]; then
            while IFS=' ' read -r LOG_BYTES LOG_PATH; do
                LOG_SIZE_MB=$(echo "scale=0; $LOG_BYTES / 1048576" | bc)
                CONTAINER_ID=$(echo "$LOG_PATH" | cut -d/ -f5 | head -c 12)
                CONTAINER_NAME=$(docker ps -a --filter "id=${CONTAINER_ID}" --format "{{.Names}}" 2>/dev/null || echo "unknown")
                record_result "ERROR" "容器 ${CONTAINER_NAME}(${CONTAINER_ID}) 日志 ${LOG_SIZE_MB}MB，超过 ${LOG_THRESHOLD}MB"
            done <<< "$BIG_LOGS"
        else
            record_result "OK" "无超过 ${LOG_THRESHOLD}MB 的大日志"
        fi
    fi
    
    # ========== 4. Dangling Volumes 报告 ==========
    echo "--- Dangling Volumes ---"
    if command -v docker &> /dev/null; then
        DANGLING_VOLUMES=$(docker volume ls --filter dangling=true -q 2>/dev/null)
        [[ -n "$DANGLING_VOLUMES" ]] && DANGLING_COUNT=$(echo "$DANGLING_VOLUMES" | grep -c .) || DANGLING_COUNT=0
        
        # 可回收体积阈值（MB）：低于该阈值仅提示，不判为异常
        DANGLING_VOLUME_SIZE_THRESHOLD=${DANGLING_VOLUME_SIZE_THRESHOLD_MB:-100}
        
        if (( DANGLING_COUNT > 0 )); then
            # 估算总大小（逐个 du）
            DANGLING_SIZE_KB=0
            while IFS= read -r VOL_NAME; do
                if [[ -n "$VOL_NAME" ]]; then
                    VOL_MOUNT=$(docker volume inspect "$VOL_NAME" --format '{{.Mountpoint}}' 2>/dev/null)
                    if [[ -n "$VOL_MOUNT" && -d "$VOL_MOUNT" ]]; then
                        VOL_SIZE=$(du -sk "$VOL_MOUNT" 2>/dev/null | cut -f1)
                        VOL_SIZE=${VOL_SIZE:-0}
                        DANGLING_SIZE_KB=$((DANGLING_SIZE_KB + VOL_SIZE))
                    fi
                fi
            done <<< "$DANGLING_VOLUMES"
            
            DANGLING_SIZE_MB=$(awk -v kb="$DANGLING_SIZE_KB" 'BEGIN{printf "%.1f", kb / 1024}')
            
            if (( $(echo "$DANGLING_SIZE_MB >= $DANGLING_VOLUME_SIZE_THRESHOLD" | bc -l) )); then
                record_result "ERROR" "发现 ${DANGLING_COUNT} 个无用数据卷，可回收约 ${DANGLING_SIZE_MB}MB（超过阈值 ${DANGLING_VOLUME_SIZE_THRESHOLD}MB，运行 docker volume prune -f 清理）"
            else
                record_result "OK" "发现 ${DANGLING_COUNT} 个无用数据卷，合计约 ${DANGLING_SIZE_MB}MB（低于阈值 ${DANGLING_VOLUME_SIZE_THRESHOLD}MB，可暂不清理）"
            fi
        else
            record_result "OK" "无悬空数据卷"
        fi
    fi
}

# 函数：发送飞书消息（直连 Webhook，v2 风格报告）
send_feishu_message() {
    local message="$1"
    local ssh_status="$2"
    
    echo "发送飞书消息..."
    
    CURRENT_TIME=$(date '+%Y-%m-%d %H:%M:%S')
    ERROR_COUNT=$(echo "$CHECK_RESULT" | grep -c "❌")
    WARN_COUNT=$(echo "$CHECK_RESULT" | grep -c "⚠️")
    OK_COUNT=$(echo "$CHECK_RESULT" | grep -c "✅")
    TOTAL_COUNT=$((ERROR_COUNT + WARN_COUNT + OK_COUNT))
    
    # 构建报告标题
    if (( ERROR_COUNT > 0 )); then
        REPORT_TITLE="🚨 服务器巡检报告"
    elif (( WARN_COUNT > 0 )); then
        REPORT_TITLE="⚠️ 服务器巡检报告"
    else
        REPORT_TITLE="✅ 服务器巡检报告"
    fi
    
    # 构建完整报告
    REPORT="${REPORT_TITLE}

时间：${CURRENT_TIME}
检查项总数：${TOTAL_COUNT}
正常项：${OK_COUNT}
提醒项：${WARN_COUNT}
异常项：${ERROR_COUNT}

检查详情：
${CHECK_RESULT}"
    
    # 写入临时文件，避免 Python 字符串引用问题
    echo "$REPORT" > /tmp/feishu_report.txt
    echo "$FEISHU_WEBHOOK" > /tmp/feishu_webhook.txt
    
    # Python 读取临时文件发送
    python3 << 'PYEOF'
import json
import urllib.request

with open('/tmp/feishu_report.txt', 'r', encoding='utf-8') as f:
    report = f.read()

with open('/tmp/feishu_webhook.txt', 'r', encoding='utf-8') as f:
    webhook_url = f.read().strip()

payload = {"msg_type": "text", "content": {"text": report}}
data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"})

try:
    resp = urllib.request.urlopen(req, timeout=10)
    result = resp.read().decode()
    print(f"飞书消息响应：{result}")
    if '"code":0' in result or '"StatusCode":0' in result:
        print("✅ 飞书消息发送成功")
    else:
        print("❌ 飞书消息发送失败")
except Exception as e:
    print(f"❌ 飞书消息发送失败：{e}")
PYEOF

    # 清理临时文件
    rm -f /tmp/feishu_report.txt /tmp/feishu_webhook.txt
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
