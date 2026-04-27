#!/bin/bash

# ============================================
# V2.4 自动化打包脚本
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
DEPLOY_PACKAGE_NAME=${DEPLOY_PACKAGE_NAME:-"deployment_package_v24.tar.gz"}
PROJECT_NAME=${PROJECT_NAME:-$(basename "$(pwd)")}

echo "============================================="
echo "开始打包项目：$PROJECT_NAME (V2.4)"
echo "============================================="

# 创建临时目录
TEMP_DIR="/tmp/${PROJECT_NAME}_v24_deploy_$$"
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
    --exclude='backtest_results/*' \
    --exclude='*.tar.gz' \
    --exclude='.DS_Store' \
    --exclude='._*' \
    --exclude='node_modules/*' \
    --exclude='.env.local' \
    --exclude='.trae/*' \
    --exclude='venv/*' \
    --exclude='*.md' \
    --include='docs/schemes/V2.4 最终配置方案.md' \
    --include='docs/reports/V2.4 策略优化完成报告.md' \
    --include='docs/reports/V2.4 vs V2.5 全方位收益对比报告.md' \
    --include='README.md' \
    --include='config_v24_final.yaml' \
    --include='backtester_v24.py' \
    --include='daily_scan.py' \
    --include='feishu_push.py' \
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
