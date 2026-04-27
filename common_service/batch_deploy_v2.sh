#!/bin/bash

# ============================================
# 批量部署通知服务改造版本（修复版）
# ============================================

set -e  # 遇到错误立即退出

SERVER="root@43.156.242.184"
SSH_OPTS="-o StrictHostKeyChecking=no -i /Users/yl/vscode/inspection_automation/docs/only.pem_new"

echo "============================================="
echo "批量部署通知服务改造版本（修复版）"
echo "============================================="
echo ""

# 1. 部署检查自动化系统（已完成）
echo "📋 步骤 1/5: 检查自动化系统（已完成）"
echo "   ✅ server_check.sh 已更新并运行"
echo ""

# 2. 部署股票筛选系统
echo "📊 步骤 2/5: 部署股票筛选系统..."
scp $SSH_OPTS /Users/yl/vscode/stockfilter/output/feishu_v2.py $SERVER:/root/stockfilter/output/feishu_v2.py
scp $SSH_OPTS /Users/yl/vscode/stockfilter/feishu_push_v2.py $SERVER:/root/stockfilter/feishu_push_v2.py
ssh $SSH_OPTS $SERVER "chmod +x /root/stockfilter/feishu_push_v2.py && cd /root/stockfilter/output && cp feishu.py feishu.py.backup && cp feishu_v2.py feishu.py"
echo "   ✅ 股票筛选系统部署完成"
echo ""

# 3. 部署新币做空系统
echo "🪙 步骤 3/5: 部署新币做空系统..."
scp $SSH_OPTS /Users/yl/vscode/bianace_newtrade_trade/short_selling_system/core/notifier_v2.py $SERVER:/root/short_selling_system/core/notifier_v2.py
ssh $SSH_OPTS $SERVER "cd /root/short_selling_system/core && cp notifier.py notifier.py.backup && cp notifier_v2.py notifier.py && chmod 644 notifier.py"
echo "   ✅ 新币做空系统部署完成"
echo ""

# 4. 部署网格交易系统（需要找到正确路径）
echo "📈 步骤 4/5: 部署网格交易系统..."
# 检查容器
CONTAINER_NAME=$(ssh $SSH_OPTS $SERVER "docker ps --filter 'name=binance-trade-analyzer' --format '{{.Names}}' | head -1")
if [ -n "$CONTAINER_NAME" ]; then
    echo "   ℹ️  网格容器：$CONTAINER_NAME"
    # 上传到临时位置
    scp $SSH_OPTS /Users/yl/vscode/Grid_Trading/adaptive_grid_trading/src/monitoring/notifier_v2.py $SERVER:/root/notifier_v2_grid.py
    echo "   ✅ 上传到临时位置 /root/notifier_v2_grid.py"
    echo "   ℹ️  需要手动复制到容器内或重启容器后替换"
else
    echo "   ⚠️  未找到网格交易容器"
fi
echo ""

# 5. 验证部署
echo "============================================="
echo "验证部署..."
echo "============================================="

# 检查容器状态
echo "容器状态:"
ssh $SSH_OPTS $SERVER "docker ps --format 'table {{.Names}}\t{{.Status}}'" | grep -E 'stockfilter|short-selling|binance-trade'

# 检查文件
echo ""
echo "文件检查:"
ssh $SSH_OPTS $SERVER "ls -la /root/stockfilter/output/feishu.py /root/short_selling_system/core/notifier.py 2>/dev/null | tail -2"

echo ""
echo "============================================="
echo "🎉 批量部署完成！"
echo "============================================="
echo ""
echo "📋 下一步操作:"
echo "1. 重启新币做空系统容器: docker restart short-selling-system"
echo "2. 查看通知服务日志：docker logs --tail 50 common_service_notification"
echo "3. 测试各系统的通知功能"
echo "4. 完成 BTC/ETH 交易系统的改造"
echo ""
