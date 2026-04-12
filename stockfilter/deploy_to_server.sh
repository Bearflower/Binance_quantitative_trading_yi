#!/bin/bash

# ============================================
# 服务器部署脚本 - 股票形态筛选系统
# ============================================

set -e

SSH_KEY="$HOME/.ssh/stockfilter_key"
SERVER="root@43.156.242.184"
SSH_CMD="ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -i $SSH_KEY"
PROJECT_DIR="/root/stockfilter"

echo "============================================="
echo "股票形态筛选系统 - 服务器部署"
echo "============================================="

# 1. 停止旧容器
echo "🛑 步骤 1: 停止旧容器..."
$SSH_CMD $SERVER "cd $PROJECT_DIR && docker-compose down" || true

# 2. 清理旧镜像
echo "🧹 步骤 2: 清理旧镜像..."
$SSH_CMD $SERVER "docker rmi stockfilter:latest 2>/dev/null" || true

# 3. 构建新镜像
echo "🏗️  步骤 3: 构建 Docker 镜像..."
$SSH_CMD $SERVER "cd $PROJECT_DIR && docker-compose build --no-cache"

# 4. 启动容器
echo "🚀 步骤 4: 启动容器..."
$SSH_CMD $SERVER "cd $PROJECT_DIR && docker-compose up -d"

# 5. 等待启动
echo "⏳ 等待容器启动..."
sleep 5

# 6. 查看状态
echo "📊 步骤 5: 查看容器状态..."
$SSH_CMD $SERVER "docker ps -f name=stockfilter-app"

# 7. 查看日志
echo "📝 最近日志:"
$SSH_CMD $SERVER "docker logs --tail 30 stockfilter-app"

echo "============================================="
echo "✅ 部署完成！"
echo "============================================="
echo ""
echo "容器名称：stockfilter-app"
echo "运行模式：每天执行一次扫描"
echo ""
echo "管理命令:"
echo "  查看状态：ssh -i $SSH_KEY root@43.156.242.184 'docker ps -f name=stockfilter-app'"
echo "  查看日志：ssh -i $SSH_KEY root@43.156.242.184 'docker logs -f stockfilter-app'"
echo "  重启：ssh -i $SSH_KEY root@43.156.242.184 'docker restart stockfilter-app'"
echo "  停止：ssh -i $SSH_KEY root@43.156.242.184 'docker-compose -c $PROJECT_DIR down'"
echo "============================================="
