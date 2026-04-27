#!/bin/bash

# ============================================
# V2.4 远程部署脚本
# ============================================

SERVER_IP="43.156.242.184"
SERVER_USER="root"
SSH_KEY_FILE="/Users/yl/vscode/inspection_automation/docs/only.pem"
DOCKER_CONTAINER_NAME="stockfilter-app"
PROJECT_NAME="stockfilter"

echo "============================================="
echo "在服务器上部署 V2.4"
echo "============================================="

# 使用 SSH 执行远程部署命令
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -i "$SSH_KEY_FILE" \
    "$SERVER_USER@$SERVER_IP" << 'ENDSSH'
    
set -e

PROJECT_NAME="stockfilter"
DOCKER_CONTAINER_NAME="stockfilter-app"
DEPLOY_PACKAGE="deployment_package.tar.gz"

echo "📦 步骤 1: 停止旧容器"
docker stop $DOCKER_CONTAINER_NAME 2>/dev/null || true
docker rm $DOCKER_CONTAINER_NAME 2>/dev/null || true

echo "📂 步骤 2: 清理旧项目文件"
# 保留配置文件
cp /root/$PROJECT_NAME/.env /root/.env.backup 2>/dev/null || true
rm -rf /root/$PROJECT_NAME
mkdir -p /root/$PROJECT_NAME

echo "📥 步骤 3: 解压新包"
cd /root
tar -xzf $DEPLOY_PACKAGE -C $PROJECT_NAME
cd $PROJECT_NAME

# 恢复配置文件
if [ -f /root/.env.backup ]; then
    cp /root/.env.backup .env
    echo "✅ 已恢复配置文件"
fi

echo "🔧 步骤 4: 设置权限"
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true
chmod 600 config_v24_final.yaml 2>/dev/null || true

echo "🏗️  步骤 5: 重新构建 Docker 镜像"
docker-compose build --no-cache

echo "🚀 步骤 6: 启动容器"
docker-compose up -d

echo "⏳ 等待容器启动..."
sleep 5

echo "============================================="
echo "容器状态:"
docker ps -f name=$DOCKER_CONTAINER_NAME
echo "============================================="
echo "最近日志:"
docker logs --tail 30 $DOCKER_CONTAINER_NAME
echo "============================================="
echo "✅ V2.4 部署完成！"
echo "============================================="

ENDSSH

echo "============================================="
echo "🎉 远程部署完成！"
echo "============================================="
