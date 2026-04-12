#!/bin/bash

# ============================================
# 容器状态检查脚本
# ============================================

SERVER_IP="43.156.242.184"

echo "============================================="
echo "服务器容器状态检查"
echo "服务器：$SERVER_IP"
echo "============================================="
echo ""

# 1. 显示所有容器状态
echo "📊 所有容器状态:"
echo "---------------------------------------------"
ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'"
echo ""

# 2. 检查 binance-trade-analyzer 容器
echo "🔍 binance-trade-analyzer 容器详情:"
echo "---------------------------------------------"
ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker inspect binance-trade-analyzer --format '{{.State.Status}}' | xargs -I {} echo '容器状态：{}'"
ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker inspect binance-trade-analyzer --format '{{.State.Health.Status}}' | xargs -I {} echo '健康状态：{}'"
echo ""

# 3. 检查最近的错误日志
echo "⚠️  最近 100 行日志中的错误:"
echo "---------------------------------------------"
ERROR_COUNT=$(ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker logs --tail 100 binance-trade-analyzer 2>&1 | grep -c ERROR" || echo "0")
ERROR_COUNT=$(echo $ERROR_COUNT | tr -d '[:space:]')
echo "错误数量：$ERROR_COUNT"

if [ "$ERROR_COUNT" != "0" ] && [ -n "$ERROR_COUNT" ]; then
    echo ""
    echo "错误详情:"
    ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker logs --tail 100 binance-trade-analyzer 2>&1 | grep ERROR"
else
    echo "✅ 没有发现错误！"
fi
echo ""

# 4. 检查特定的修复
echo "✅ 修复验证:"
echo "---------------------------------------------"

# 检查 JSON 提取器
JSON_EXTRACTOR_ISSUES=$(ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker logs --tail 100 binance-trade-analyzer 2>&1 | grep -c '未能从报告中提取'" || echo "0")
JSON_EXTRACTOR_ISSUES=$(echo $JSON_EXTRACTOR_ISSUES | tr -d '[:space:]')
if [ "$JSON_EXTRACTOR_ISSUES" != "0" ] && [ -n "$JSON_EXTRACTOR_ISSUES" ]; then
    echo "❌ JSON 提取器问题：发现 $JSON_EXTRACTOR_ISSUES 个错误"
else
    echo "✅ JSON 提取器：正常"
fi

# 检查飞书通知
LARK_ISSUES=$(ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker logs --tail 100 binance-trade-analyzer 2>&1 | grep -c 'Invalid URL'" || echo "0")
LARK_ISSUES=$(echo $LARK_ISSUES | tr -d '[:space:]')
if [ "$LARK_ISSUES" != "0" ] && [ -n "$LARK_ISSUES" ]; then
    echo "❌ 飞书通知问题：发现 $LARK_ISSUES 个错误"
else
    echo "✅ 飞书通知：正常"
fi

# 检查持仓量数据
OPEN_INTEREST_ISSUES=$(ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker logs --tail 100 binance-trade-analyzer 2>&1 | grep -c '持仓量数据失败'" || echo "0")
OPEN_INTEREST_ISSUES=$(echo $OPEN_INTEREST_ISSUES | tr -d '[:space:]')
if [ "$OPEN_INTEREST_ISSUES" != "0" ] && [ -n "$OPEN_INTEREST_ISSUES" ]; then
    echo "❌ 持仓量数据获取：发现 $OPEN_INTEREST_ISSUES 个错误"
else
    echo "✅ 持仓量数据获取：正常"
fi

echo ""

# 5. 显示调度器状态
echo "⏰ 调度器状态:"
echo "---------------------------------------------"
ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker logs --tail 20 binance-trade-analyzer 2>&1 | grep 'scheduler'"
echo ""

# 6. 资源使用情况
echo "💾 资源使用情况:"
echo "---------------------------------------------"
ssh -o StrictHostKeyChecking=no root@$SERVER_IP "docker stats binance-trade-analyzer --no-stream"
echo ""

echo "============================================="
echo "检查完成!"
echo "============================================="
echo ""
echo "📝 快捷命令:"
echo "  查看实时日志：ssh root@$SERVER_IP 'docker logs -f binance-trade-analyzer'"
echo "  重启容器：ssh root@$SERVER_IP 'docker restart binance-trade-analyzer'"
echo "  进入容器：ssh root@$SERVER_IP 'docker exec -it binance-trade-analyzer /bin/bash'"
echo ""
