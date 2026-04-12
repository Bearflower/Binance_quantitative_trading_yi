#!/bin/bash

# ============================================
# 自动化打包脚本
# ============================================

set -e  # 遇到错误立即退出

# 加载配置
if [ -f ".deploy_config" ]; then
    source .deploy_config
    echo "✅ 已加载部署配置"
else
    echo "❌ 错误：.deploy_config 文件不存在"
    exit 1
fi

# 设置默认值
DEPLOY_PACKAGE_NAME=${DEPLOY_PACKAGE_NAME:-"deployment_package.tar.gz"}
PROJECT_NAME=${PROJECT_NAME:-$(basename "$(pwd)")}

echo "============================================="
echo "开始打包项目：$PROJECT_NAME"
echo "============================================="

# 创建临时目录
TEMP_DIR="/tmp/${PROJECT_NAME}_deploy_$$"
echo "📁 创建临时目录：$TEMP_DIR"
mkdir -p "$TEMP_DIR"

# 复制项目文件（排除不需要的文件）
echo "📋 复制项目文件..."
rsync -av \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='logs/*' \
    --exclude='data/*' \
    --exclude='reports/*' \
    --exclude='*.tar.gz' \
    --exclude='.DS_Store' \
    --exclude='._*' \
    --exclude='node_modules/*' \
    --exclude='.env.local' \
    --exclude='.trae/*' \
    ./ "$TEMP_DIR/"

# 创建压缩包
echo "📦 创建压缩包..."
cd "$TEMP_DIR"
tar -czf "$OLDPWD/$DEPLOY_PACKAGE_NAME" .

# 清理临时目录
cd "$OLDPWD"
rm -rf "$TEMP_DIR"

# 显示结果
PACKAGE_SIZE=$(ls -lh "$DEPLOY_PACKAGE_NAME" | awk '{print $5}')
echo "============================================="
echo "✅ 打包完成！"
echo "📦 压缩包：$DEPLOY_PACKAGE_NAME"
echo "📊 大小：$PACKAGE_SIZE"
echo "============================================="
