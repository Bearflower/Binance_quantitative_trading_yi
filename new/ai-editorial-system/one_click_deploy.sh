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
    "$SERVER_USER@$SERVER_IP" << ENDSSH
    
# 在服务器上执行的命令
set -e

# 确保项目目录存在
mkdir -p /root/ai-editorial-system

# 停止并删除旧容器
docker stop ai-editorial-system 2>/dev/null || true
docker rm ai-editorial-system 2>/dev/null || true

# 解压新包
cd /root
mkdir -p ai-editorial-system
tar -xzf deployment_package.tar.gz -C ai-editorial-system
cd ai-editorial-system

# 设置权限
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true

# 构建并启动
docker-compose build --no-cache
docker-compose up -d

# 等待启动
sleep 3

# 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=ai-editorial-system
echo "============================================="
echo "最近日志:"
docker logs --tail 30 ai-editorial-system
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
