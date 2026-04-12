#!/bin/bash

# ============================================
# 一键部署脚本 - 币安自动化交易系统
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

PROJECT_NAME="bianace_btcethbnb_trade"
DOCKER_CONTAINER_NAME="binance-trade-analyzer"
DOCKER_IMAGE_NAME="bianace_btcethbnb_trade-binance-trade-analyzer:latest"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
SERVER_PROJECT_PATH="/root/bianace_btcethbnb_trade"

echo "📥 解压部署包..."
cd /root
tar -xzf $DEPLOY_PACKAGE_NAME -C $PROJECT_NAME

cd $PROJECT_NAME

# 设置权限
echo "🔧 设置权限..."
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true
chmod +x *.sh 2>/dev/null || true

# 停止并删除旧容器
echo "🛑 停止旧容器..."
docker stop $DOCKER_CONTAINER_NAME 2>/dev/null || true

echo "🗑️ 删除旧容器..."
docker rm $DOCKER_CONTAINER_NAME 2>/dev/null || true

# 删除旧镜像
echo "🗑️ 删除旧镜像..."
docker rmi $DOCKER_IMAGE_NAME 2>/dev/null || true

# 构建新镜像
echo "🏗️  构建新镜像..."
docker-compose build --no-cache

# 启动新容器
echo "🚀 启动新容器..."
docker-compose up -d

# 等待启动
echo "⏳ 等待容器启动..."
sleep 5

# 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=$DOCKER_CONTAINER_NAME
echo "============================================="
echo "最近日志:"
docker logs --tail 30 $DOCKER_CONTAINER_NAME
echo "============================================="

ENDSSH

# 步骤 4：验证
echo "✅ 步骤 4/4: 验证部署..."
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "docker ps -f name=$DOCKER_CONTAINER_NAME --format '容器 {{.Names}} 状态：{{.Status}}'"

# 清理本地压缩包
echo "🧹 清理本地临时文件..."
rm -f $DEPLOY_PACKAGE_NAME

echo "============================================="
echo "🎉 一键部署完成！"
echo "============================================="
echo "💡 提示："
echo "- 查看日志：ssh root@$SERVER_IP 'docker logs -f $DOCKER_CONTAINER_NAME'"
echo "- 查看状态：ssh root@$SERVER_IP 'docker ps -f name=$DOCKER_CONTAINER_NAME'"
echo "- 重启容器：ssh root@$SERVER_IP 'docker restart $DOCKER_CONTAINER_NAME'"
echo "============================================="
