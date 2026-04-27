#!/bin/bash

# ============================================
# 自动化打包脚本 - 股票形态筛选系统
# ============================================

set -e

echo "============================================="
echo "开始打包项目：stockfilter"
echo "============================================="

# 创建临时目录
TEMP_DIR="/tmp/stockfilter_deploy_$$"
echo "📁 创建临时目录：$TEMP_DIR"
mkdir -p "$TEMP_DIR"

# 复制项目文件（排除不需要的文件）
echo "📋 复制项目文件..."
rsync -av \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='logs/*' \
    --exclude='reports/*' \
    --exclude='*.tar.gz' \
    --exclude='.DS_Store' \
    --exclude='._*' \
    --exclude='node_modules/*' \
    --exclude='.env.local' \
    --exclude='.trae/*' \
    --exclude='.venv/*' \
    --exclude='venv/*' \
    --exclude='data/backtest/*' \
    --exclude='!data/backtest/.gitkeep' \
    --delete \
    ./ "$TEMP_DIR/"

# 清理 macOS 资源文件
echo "🧹 清理 macOS 资源文件..."
find "$TEMP_DIR" -name '._*' -delete
find "$TEMP_DIR" -name '.DS_Store' -delete

# 创建压缩包
echo "📦 创建压缩包..."
cd "$TEMP_DIR"
tar -czf "$OLDPWD/deployment_package.tar.gz" .

# 清理临时目录
cd "$OLDPWD"
rm -rf "$TEMP_DIR"

# 显示结果
PACKAGE_SIZE=$(ls -lh deployment_package.tar.gz | awk '{print $5}')
echo "============================================="
echo "✅ 打包完成！"
echo "📦 压缩包：deployment_package.tar.gz"
echo "📊 大小：$PACKAGE_SIZE"
echo "============================================="
