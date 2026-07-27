#!/bin/bash

# K线数据下载脚本（独立版）
# 使用币安API直接下载K线数据

set -e

# 配置
COIN_LIST="new_coin_listings.json"
OUTPUT_DIR="./klines"
INTERVALS=("1h" "15m" "5m")
BINANCE_API="https://fapi.binance.com"

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 统计信息
TOTAL_SYMBOLS=0
SUCCESS_COUNT=0
FAIL_COUNT=0

echo "============================================="
echo "K线数据下载脚本（独立版）"
echo "============================================="
echo "开始时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 检查jq是否安装
if ! command -v jq &> /dev/null; then
    echo "安装jq工具..."
    yum install -y jq > /dev/null 2>&1 || apt-get install -y jq > /dev/null 2>&1
fi

# 读取交易对列表
if [ ! -f "$COIN_LIST" ]; then
    echo "错误：找不到交易对列表文件 $COIN_LIST"
    exit 1
fi

# 解析JSON并下载
echo "开始下载K线数据..."
echo ""

# 提取交易对符号
SYMBOLS=$(jq -r '.contracts[].symbol' "$COIN_LIST" 2>/dev/null || jq -r '.[].symbol' "$COIN_LIST" 2>/dev/null)

for SYMBOL in $SYMBOLS; do
    TOTAL_SYMBOLS=$((TOTAL_SYMBOLS + 1))
    
    # 获取上线时间
    LISTING_TIME=$(jq -r --arg sym "$SYMBOL" '.contracts[] | select(.symbol == $sym) | .onboardDateStr' "$COIN_LIST" 2>/dev/null || \
                   jq -r --arg sym "$SYMBOL" '.[] | select(.symbol == $sym) | .listing_time' "$COIN_LIST" 2>/dev/null)
    
    # 转换时间格式（移除UTC后缀）
    LISTING_TIME=${LISTING_TIME// UTC/}
    
    # 转换为时间戳
    if [[ "$LISTING_TIME" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2} ]]; then
        START_TS=$(date -d "$LISTING_TIME" +%s)000
    else
        # 如果没有上线时间，默认下载最近180天的数据
        START_TS=$(date -d "180 days ago" +%s)000
    fi
    
    END_TS=$(date +%s)000
    
    echo "[$TOTAL_SYMBOLS] 下载 $SYMBOL (上线时间: $LISTING_TIME)"
    
    for INTERVAL in "${INTERVALS[@]}"; do
        OUTPUT_FILE="$OUTPUT_DIR/${SYMBOL}_${INTERVAL}.csv"
        
        # 检查文件是否已存在
        if [ -f "$OUTPUT_FILE" ]; then
            # 获取文件中最后一行的时间戳
            LAST_TS=$(tail -1 "$OUTPUT_FILE" | cut -d',' -f1)
            if [ -n "$LAST_TS" ] && [ "$LAST_TS" -gt "$START_TS" ]; then
                START_TS=$LAST_TS
                echo "  - $INTERVAL: 增量更新（从 $(date -d "@$((LAST_TS/1000))" '+%Y-%m-%d %H:%M:%S') 开始）"
            fi
        fi
        
        # 下载K线数据
        TEMP_FILE=$(mktemp)
        
        # 计算需要下载的K线数量
        case $INTERVAL in
            "1h") INTERVAL_MS=3600000 ;;
            "15m") INTERVAL_MS=900000 ;;
            "5m") INTERVAL_MS=300000 ;;
        esac
        
        TOTAL_KLINES=$(( (END_TS - START_TS) / INTERVAL_MS ))
        
        # 分批下载（每次最多1500根）
        BATCH_SIZE=1500
        CURRENT_TS=$START_TS
        
        > "$TEMP_FILE"  # 清空临时文件
        
        while [ $CURRENT_TS -lt $END_TS ]; do
            # 构建API URL
            URL="${BINANCE_API}/fapi/v1/klines?symbol=${SYMBOL}&interval=${INTERVAL}&startTime=${CURRENT_TS}&limit=${BATCH_SIZE}"
            
            # 下载数据
            HTTP_CODE=$(curl -s -w "%{http_code}" -o "${TEMP_FILE}.batch" "$URL")
            
            if [ "$HTTP_CODE" -eq "200" ]; then
                # 解析JSON并转换为CSV格式
                jq -r '.[] | [.[]] | @csv' "${TEMP_FILE}.batch" >> "$TEMP_FILE" 2>/dev/null || true
                
                # 更新时间戳
                LAST_TS_IN_BATCH=$(jq -r '.[-1][0]' "${TEMP_FILE}.batch" 2>/dev/null || echo "$CURRENT_TS")
                CURRENT_TS=$((LAST_TS_IN_BATCH + INTERVAL_MS))
                
                # 避免API限流
                sleep 0.1
            else
                echo "    警告: HTTP $HTTP_CODE"
                break
            fi
        done
        
        # 合并数据
        if [ -s "$TEMP_FILE" ]; then
            if [ -f "$OUTPUT_FILE" ]; then
                # 合并并去重
                cat "$OUTPUT_FILE" "$TEMP_FILE" | sort -u -t',' -k1,1 > "${OUTPUT_FILE}.tmp"
                mv "${OUTPUT_FILE}.tmp" "$OUTPUT_FILE"
            else
                mv "$TEMP_FILE" "$OUTPUT_FILE"
            fi
            
            KLINE_COUNT=$(wc -l < "$OUTPUT_FILE")
            echo "  - $INTERVAL: 完成 ($KLINE_COUNT 根K线)"
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            echo "  - $INTERVAL: 失败（无数据）"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
        
        rm -f "$TEMP_FILE" "${TEMP_FILE}.batch"
    done
    
    echo ""
done

echo "============================================="
echo "下载完成"
echo "============================================="
echo "结束时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "总交易对数: $TOTAL_SYMBOLS"
echo "成功数量: $SUCCESS_COUNT"
echo "失败数量: $FAIL_COUNT"
echo "数据目录: $OUTPUT_DIR"
echo "============================================="
