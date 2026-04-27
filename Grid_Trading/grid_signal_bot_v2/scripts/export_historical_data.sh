#!/bin/bash

# 从服务器数据库导出历史K线数据

set -e

# 配置
SERVER_IP="43.156.242.184"
SERVER_USER="root"
SYMBOL="BTCUSDT"
INTERVAL="1h"
DAYS=90
OUTPUT_FILE="historical_klines_${SYMBOL}_${INTERVAL}.json"

echo "========================================"
echo "📊 从服务器导出历史K线数据"
echo "========================================"
echo "交易对: $SYMBOL"
echo "时间间隔: $INTERVAL"
echo "天数: $DAYS"
echo "输出文件: $OUTPUT_FILE"
echo "========================================"

# 计算时间戳
END_TIMESTAMP=$(date +%s)
START_TIMESTAMP=$((END_TIMESTAMP - DAYS * 86400))

echo "开始时间: $(date -r $START_TIMESTAMP '+%Y-%m-%d %H:%M:%S')"
echo "结束时间: $(date -r $END_TIMESTAMP '+%Y-%m-%d %H:%M:%S')"

# 在服务器上执行查询并导出为JSON
echo ""
echo "📡 正在从服务器数据库导出数据..."

ssh $SERVER_USER@$SERVER_IP << ENDSSH
#!/bin/bash
set -e

# 计算时间戳
END_TIMESTAMP=\$(date +%s)
START_TIMESTAMP=\$((END_TIMESTAMP - $DAYS * 86400))

echo "服务器开始时间: \$START_TIMESTAMP"
echo "服务器结束时间: \$END_TIMESTAMP"

# SQL查询
QUERY="SELECT json_agg(
    json_build_object(
        'open_time', EXTRACT(EPOCH FROM open_time) * 1000,
        'open_price', open_price,
        'high_price', high_price,
        'low_price', low_price,
        'close_price', close_price,
        'volume', volume,
        'close_time', EXTRACT(EPOCH FROM close_time) * 1000
    )
)
FROM klines
WHERE symbol = '$SYMBOL'
  AND interval = '$INTERVAL'
  AND EXTRACT(EPOCH FROM open_time) >= \$START_TIMESTAMP
  AND EXTRACT(EPOCH FROM open_time) <= \$END_TIMESTAMP
ORDER BY open_time ASC;"

# 执行查询并保存到临时文件
docker exec common_service_postgres psql -U binance -d binance_data -t -A -c "\$QUERY" > /tmp/klines_export.json

# 检查结果
if [ -s /tmp/klines_export.json ]; then
    echo "✅ 数据导出成功"
    cat /tmp/klines_export.json | python3 -c "import sys, json; data=json.load(sys.stdin); print(f'K线数量: {len(data) if data else 0}')"
else
    echo "❌ 数据导出失败"
    exit 1
fi
ENDSSH

# 从服务器下载文件
echo ""
echo "📥 正在下载数据到本地..."
scp $SERVER_USER@$SERVER_IP:/tmp/klines_export.json $OUTPUT_FILE

# 检查本地文件
if [ -s $OUTPUT_FILE ]; then
    echo ""
    echo "========================================"
    echo "✅ 数据下载成功！"
    echo "========================================"
    echo "文件路径: $OUTPUT_FILE"
    echo "文件大小: $(du -h $OUTPUT_FILE | cut -f1)"
    echo "K线数量: $(python3 -c "import json; data=json.load(open('$OUTPUT_FILE')); print(len(data) if data else 0)")"
    echo "========================================"
else
    echo "❌ 数据下载失败"
    exit 1
fi
