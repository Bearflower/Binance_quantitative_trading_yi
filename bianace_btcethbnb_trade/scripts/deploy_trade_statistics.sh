#!/bin/bash

# ============================================
# 部署交易统计修复模块到服务器
# ============================================

set -e

SERVER_IP="43.156.242.184"
CONTAINER_NAME="binance-trade-analyzer"

echo "============================================="
echo "部署交易统计修复模块"
echo "============================================="
echo ""

# 步骤 1: 上传 trade_statistics.py
echo "📤 上传 trade_statistics.py..."
scp -o StrictHostKeyChecking=no \
    /Users/yl/vscode/bianace_btcethbnb_trade/services/trade_statistics.py \
    root@$SERVER_IP:/tmp/trade_statistics.py

# 步骤 2: 复制到容器
echo "📋 复制到容器..."
ssh -o StrictHostKeyChecking=no root@$SERVER_IP \
    "docker cp /tmp/trade_statistics.py $CONTAINER_NAME:/app/services/trade_statistics.py"

# 步骤 3: 验证文件
echo "✅ 验证文件..."
ssh -o StrictHostKeyChecking=no root@$SERVER_IP \
    "docker exec $CONTAINER_NAME ls -la /app/services/trade_statistics.py"

# 步骤 4: 测试导入
echo "🧪 测试导入..."
ssh -o StrictHostKeyChecking=no root@$SERVER_IP \
    "docker exec $CONTAINER_NAME python3 -c \"from services.trade_statistics import get_trade_statistics_manager; print('✅ 导入成功')\""

# 步骤 5: 重启容器
echo "🔄 重启容器..."
ssh -o StrictHostKeyChecking=no root@$SERVER_IP \
    "docker restart $CONTAINER_NAME"

echo ""
echo "============================================="
echo "部署完成！"
echo "============================================="
echo ""
echo "下一步："
echo "1. 观察容器日志：ssh root@$SERVER_IP 'docker logs -f $CONTAINER_NAME'"
echo "2. 等待平仓发生，查看统计更新"
echo "3. 明天查看交易日报，验证胜率"
