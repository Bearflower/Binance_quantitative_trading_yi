#!/bin/bash

# ============================================
# 一键部署脚本 - 币安新币做空系统
# ============================================

set -e

# 加载配置
source .deploy_config

echo "============================================="
echo "一键部署 - $PROJECT_NAME"
echo "目标服务器：$SERVER_IP"
echo "============================================="

# 步骤 1：打包
echo "📦 步骤 1/4: 打包项目..."
./auto_package.sh

# 步骤 2：上传
echo "📤 步骤 2/4: 上传到服务器..."
./upload_to_server.sh

# 步骤 3：远程部署
echo "🚀 步骤 3/4: 远程部署..."

# 使用 SSH 执行远程部署命令（使用密钥认证）
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" << 'ENDSSH'
    
# 在服务器上执行的命令
set -e

PROJECT_NAME="short_selling_system"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
DOCKER_CONTAINER_NAME="short-selling-system"
DOCKER_IMAGE_NAME="short-selling-system:latest"

echo "============================================="
echo "远程部署 - $PROJECT_NAME"
echo "============================================="

# 停止并删除旧容器
echo "🛑 停止旧容器..."
docker stop $DOCKER_CONTAINER_NAME 2>/dev/null || true
docker rm $DOCKER_CONTAINER_NAME 2>/dev/null || true

# 解压新包
echo "📦 解压新代码..."
cd /root
tar -xzf $DEPLOY_PACKAGE_NAME -C $PROJECT_NAME

# 设置权限
echo "🔐 设置权限..."
cd $PROJECT_NAME
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true

# 构建并启动
echo "🏗️  构建 Docker 镜像..."
docker-compose build --no-cache

echo "🚀 启动容器..."
docker-compose up -d

# 等待启动
sleep 5

# 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=$DOCKER_CONTAINER_NAME
echo "============================================="
echo "最近日志:"
docker logs --tail 50 $DOCKER_CONTAINER_NAME
echo "============================================="
ENDSSH

# 步骤 4：验证
echo "✅ 步骤 4/4: 验证部署..."
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "docker ps -f name=$DOCKER_CONTAINER_NAME --format '容器 {{.Names}} 状态：{{.Status}}'"

echo "============================================="
echo "🎉 一键部署完成！"
echo "============================================="
echo ""
echo "📋 更新内容:"
echo "  ✅ 币安交易 API 模块 (binance_trading_api.py)"
echo "  ✅ 交易执行器更新 (trading_executor.py)"
echo "  ✅ 精度处理优化 (使用 Decimal)"
echo "  ✅ 单元测试和验证工具"
echo "  ✅ 完整文档（使用指南、快速参考、配置说明）"
echo ""
echo "📖 查看文档:"
echo "  - docs/binance_api_usage.md (使用指南)"
echo "  - docs/binance_api_quick_reference.md (快速参考)"
echo "  - docs/precision_handling.md (精度处理详解)"
echo "  - docs/precision_optimization_summary.md (优化总结)"
echo ""
echo "============================================="
