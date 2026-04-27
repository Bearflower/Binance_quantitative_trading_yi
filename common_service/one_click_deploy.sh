#!/bin/bash

# ============================================
# 一键部署脚本 - Common Service
# ============================================

set -e

# 加载配置
source .deploy_config

PROJECT_NAME_LOCAL="common_service"

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
    "$SERVER_USER@$SERVER_IP" << ENDSSH

# 在服务器上执行的命令
set -e

echo "📥 解压部署包..."
cd /root
mkdir -p $PROJECT_NAME
tar -xzf $DEPLOY_PACKAGE_NAME -C $PROJECT_NAME --strip-components=1

cd $PROJECT_NAME

echo "🛑 停止旧容器..."
docker-compose down 2>/dev/null || true

echo "🏗️  构建镜像..."
docker-compose build --no-cache

echo "🚀 启动服务..."
docker-compose up -d

echo "⏳ 等待服务启动..."
sleep 5

echo "============================================="
echo "容器状态:"
docker ps -f name=${PROJECT_NAME}
echo "============================================="

echo "服务日志:"
docker-compose logs --tail=50
ENDSSH

# 步骤 4：验证
echo ""
echo "✅ 步骤 4/4: 验证部署..."
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "docker ps -f name=${PROJECT_NAME} --format '容器 {{.Names}} 状态：{{.Status}}'"

echo ""
echo "============================================="
echo "🎉 一键部署完成！"
echo "============================================="
echo ""
echo "服务访问地址:"
echo "  - 通知服务：http://$SERVER_IP:8766"
echo "  - K 线数据服务：http://$SERVER_IP:8000"
echo ""
echo "查看日志:"
echo "  ssh root@$SERVER_IP 'docker-compose logs -f -d $PROJECT_NAME'"
echo "============================================="
