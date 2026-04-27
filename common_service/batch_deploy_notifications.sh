#!/bin/bash

# ============================================
# 批量部署通知服务改造版本
# ============================================

set -e  # 遇到错误立即退出

SERVER="root@43.156.242.184"
SSH_OPTS="-o StrictHostKeyChecking=no -i /Users/yl/vscode/inspection_automation/docs/only.pem_new"

echo "============================================="
echo "批量部署通知服务改造版本"
echo "============================================="
echo ""

# 1. 部署检查自动化系统（已完成）
echo "📋 步骤 1/4: 检查自动化系统（已完成）"
echo "   ✅ server_check.sh 已更新并运行"
echo ""

# 2. 部署股票筛选系统
echo "📊 步骤 2/4: 部署股票筛选系统..."

# 上传改造后的通知模块
scp $SSH_OPTS /Users/yl/vscode/stockfilter/output/feishu_v2.py $SERVER:/root/stockfilter/output/feishu_v2.py
echo "   ✅ 上传 feishu_v2.py"

scp $SSH_OPTS /Users/yl/vscode/stockfilter/feishu_push_v2.py $SERVER:/root/stockfilter/feishu_push_v2.py
echo "   ✅ 上传 feishu_push_v2.py"

# 设置权限
ssh $SSH_OPTS $SERVER "chmod +x /root/stockfilter/feishu_push_v2.py"
echo "   ✅ 设置执行权限"

# 备份并替换（可选）
ssh $SSH_OPTS $SERVER "cd /root/stockfilter && cp output/feishu.py output/feishu.py.backup && cp output/feishu_v2.py output/feishu.py"
echo "   ✅ 备份并替换旧版本"
echo ""

# 3. 部署网格交易系统
echo "📈 步骤 3/4: 部署网格交易系统..."

# 上传改造后的通知模块
scp $SSH_OPTS /Users/yl/vscode/Grid_Trading/adaptive_grid_trading/src/monitoring/notifier_v2.py $SERVER:/root/binance-trade-analyzer/adaptive_grid_trading/src/monitoring/notifier_v2.py
echo "   ✅ 上传 notifier_v2.py"

# 设置权限
ssh $SSH_OPTS $SERVER "chmod 644 /root/binance-trade-analyzer/adaptive_grid_trading/src/monitoring/notifier_v2.py"
echo "   ✅ 设置权限"

# 重启容器（如果需要）
echo "   ℹ️  需要重启容器以应用更改"
echo ""

# 4. 部署新币做空系统
echo "🪙 步骤 4/4: 部署新币做空系统..."

# 上传改造后的通知模块
scp $SSH_OPTS /Users/yl/vscode/bianace_newtrade_trade/short_selling_system/core/notifier_v2.py $SERVER:/root/short_selling_system/short_selling_system/core/notifier_v2.py
echo "   ✅ 上传 notifier_v2.py"

# 设置权限
ssh $SSH_OPTS $SERVER "chmod 644 /root/short_selling_system/short_selling_system/core/notifier_v2.py"
echo "   ✅ 设置权限"

# 备份并替换（可选）
ssh $SSH_OPTS $SERVER "cd /root/short_selling_system/short_selling_system/core && cp notifier.py notifier.py.backup && cp notifier_v2.py notifier.py"
echo "   ✅ 备份并替换旧版本"

# 重启容器
echo "   🔄 重启新币做空系统容器..."
ssh $SSH_OPTS $SERVER "docker restart short-selling-system"
echo "   ✅ 容器已重启"
echo ""

# 验证部署
echo "============================================="
echo "验证部署..."
echo "============================================="

# 检查容器状态
ssh $SSH_OPTS $SERVER "docker ps -f name=short-selling-system --format '容器 {{.Names}}: {{.Status}}'"
ssh $SSH_OPTS $SERVER "docker ps -f name=stockfilter-app --format '容器 {{.Names}}: {{.Status}}'"
ssh $SSH_OPTS $SERVER "docker ps -f name=binance-trade-analyzer --format '容器 {{.Names}}: {{.Status}}'"

echo ""
echo "============================================="
echo "🎉 批量部署完成！"
echo "============================================="
echo ""
echo "📋 下一步:"
echo "1. 查看日志确认通知发送成功"
echo "2. 测试各系统的通知功能"
echo "3. 完成 BTC/ETH 交易系统的改造"
echo ""
