#!/bin/bash

# ============================================
# 一键部署脚本 - 打包 + 上传 + 部署
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

PROJECT_NAME="stockfilter"
DOCKER_CONTAINER_NAME="stockfilter-app"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"

echo "📋 停止并删除旧容器..."
docker stop $DOCKER_CONTAINER_NAME 2>/dev/null || true
docker rm $DOCKER_CONTAINER_NAME 2>/dev/null || true

echo "📦 解压新包..."
cd /root
tar -xzf $DEPLOY_PACKAGE_NAME -C $PROJECT_NAME

echo "🔧 设置权限..."
cd $PROJECT_NAME
chmod +x *.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true
chmod 600 .deploy_config 2>/dev/null || true

echo "🏗️  构建 Docker 镜像..."
docker-compose build --no-cache

echo "🚀 启动容器..."
docker-compose up -d

echo "⏳ 等待启动..."
sleep 5

echo "============================================="
echo "容器状态:"
docker ps -f name=$DOCKER_CONTAINER_NAME
echo "============================================="
echo "最近日志:"
docker logs --tail 30 $DOCKER_CONTAINER_NAME
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
echo "📋 后续操作:"
echo "1. 查看实时日志：ssh root@$SERVER_IP 'docker logs -f $DOCKER_CONTAINER_NAME'"
echo "2. 查看容器状态：ssh root@$SERVER_IP 'docker ps -f name=$DOCKER_CONTAINER_NAME'"
echo "3. 重启容器：ssh root@$SERVER_IP 'docker restart $DOCKER_CONTAINER_NAME'"
echo ""
