#!/bin/bash
# 快速修改执行时间的脚本
# 用法：./set_schedule_time.sh <分钟数>
# 例如：./set_schedule_time.sh 05

if [ -z "$1" ]; then
    echo "❌ 请指定分钟数（0-59）"
    echo "用法：$0 <分钟数>"
    echo "例如：$0 05"
    exit 1
fi

MINUTE=$1

# 检查分钟数是否有效
if ! [[ "$MINUTE" =~ ^[0-9]+$ ]] || [ "$MINUTE" -lt 0 ] || [ "$MINUTE" -gt 59 ]; then
    echo "❌ 分钟数必须在 0-59 之间"
    exit 1
fi

echo "============================================="
echo "修改执行时间为每小时的第 ${MINUTE} 分"
echo "============================================="

# 1. 修改服务器上的配置文件
echo "📝 修改配置文件..."
sed -i "s/minute: [0-9]*/minute: ${MINUTE}/" /root/bianace_btcethbnb_trade/config/scheduler_config.yaml

# 2. 复制配置文件到容器内
echo "📦 复制配置文件到容器..."
docker cp /root/bianace_btcethbnb_trade/config/scheduler_config.yaml binance-trade-analyzer:/app/config/scheduler_config.yaml

# 3. 重启容器
echo "🔄 重启容器..."
docker restart binance-trade-analyzer

# 4. 等待容器启动
echo "⏳ 等待容器启动..."
sleep 5

# 5. 验证配置
echo "✅ 验证配置..."
docker logs binance-trade-analyzer --tail 15 | grep "每小时执行分钟数"

echo "============================================="
echo "✅ 完成！执行时间已修改为每小时的第 ${MINUTE} 分"
echo "下一次执行时间：$(date -d "+${MINUTE} minutes" '+%H:%M:00')"
echo "============================================="
