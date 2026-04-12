#!/bin/bash

# ============================================
# 清理服务器根目录的旧项目文件夹
# ============================================

set -e

SERVER_IP="43.156.242.184"
SERVER_USER="root"

echo "============================================="
echo "清理服务器 /root/ 目录下的旧项目文件夹"
echo "============================================="

# 需要删除的旧文件夹列表（这些已经在 binance-trade-analyzer 内）
OLD_FOLDERS=(
    "ai_advisory"
    "backtest"
    "backtesting"
    "config"
    "core"
    "data"
    "database"
    "doc"
    "docs"
    "logs"
    "memory-bank"
    "models"
    "output"
    "reporting"
    "scripts"
    "services"
    "skills"
    "strategy"
    "tests"
    "trading"
    "utils"
)

echo "将要删除的旧文件夹："
for folder in "${OLD_FOLDERS[@]}"; do
    echo "  - /root/$folder"
done

echo ""
read -p "确定要删除这些文件夹吗？(y/N): " confirm

if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "❌ 操作已取消"
    exit 0
fi

echo ""
echo "开始删除..."

for folder in "${OLD_FOLDERS[@]}"; do
    if ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" "[ -d /root/$folder ]"; then
        echo "🗑️  删除 /root/$folder"
        ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" "rm -rf /root/$folder"
    else
        echo "⚠️  /root/$folder 不存在，跳过"
    fi
done

echo ""
echo "============================================="
echo "✅ 清理完成！"
echo "============================================="
echo ""
echo "保留的文件夹："
ssh -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" "ls -la /root/ | grep -E '^d' | awk '{print \$9}'"
