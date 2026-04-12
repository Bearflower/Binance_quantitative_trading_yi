#!/bin/bash

# ============================================
# 快速部署脚本 - 仅更新配置文件
# ============================================

set -e

SERVER_IP="43.156.242.184"
SERVER_USER="root"
PROJECT_DIR="/root/binance-trade-analyzer"

echo "============================================="
echo "快速部署 - 更新配置和调度器"
echo "============================================="
echo ""

# 创建临时部署包
TEMP_DIR="/tmp/quick_deploy_$$"
mkdir -p "$TEMP_DIR/config"

echo "📋 准备文件..."
cp config/strategy_params.py "$TEMP_DIR/config/"
cp scheduler_new.py "$TEMP_DIR/"

# 创建压缩包
cd "$TEMP_DIR"
tar -czf "$OLDPWD/quick_deploy.tar.gz" config/ scheduler_new.py
cd "$OLDPWD"

echo "✅ 打包完成"

# 上传
echo "📤 上传到服务器..."
scp -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    quick_deploy.tar.gz \
    "$SERVER_USER@$SERVER_IP:/root/"

echo "✅ 上传成功"

# 远程部署
echo "🚀 远程部署..."
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" << 'ENDSSH'
set -e

echo "在服务器上执行..."

cd /root/binance-trade-analyzer

# 备份旧文件
echo "📦 备份旧文件..."
cp config/strategy_params.py config/strategy_params.py.bak
cp scheduler_new.py scheduler_new.py.bak

# 解压新文件
echo "📥 解压新文件..."
cd /root
tar -xzf quick_deploy.tar.gz -C binance-trade-analyzer/

# 设置权限
cd /root/binance-trade-analyzer
chmod 644 config/strategy_params.py
chmod 644 scheduler_new.py

echo "✅ 文件更新完成"

# 重启容器
echo "🔄 重启容器..."
docker restart binance-trade-analyzer

# 等待启动
sleep 3

# 显示状态
echo ""
echo "📊 容器状态:"
docker ps -f name=binance-trade-analyzer

echo ""
echo "📝 最近日志:"
docker logs --tail 20 binance-trade-analyzer

echo ""
echo "✅ 部署完成！"
ENDSSH

# 清理
rm -rf "$TEMP_DIR"
rm -f quick_deploy.tar.gz

echo ""
echo "============================================="
echo "🎉 部署完成！"
echo "============================================="
echo ""
echo "更新内容:"
echo "  ✅ 单仓保证金：30U → 15U"
echo "  ✅ 运行频率：每小时 → 每 3 小时"
echo ""
echo "下次执行时间:"
echo "  - 00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00"
echo ""
echo "============================================="
