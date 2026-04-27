#!/bin/bash

# ============================================
# 服务器部署脚本 - 简化版
# ============================================

SSH_KEY="/Users/yl/vscode/inspection_automation/docs/only.pem"
SERVER="root@43.156.242.184"
PROJECT_DIR="/root/stockfilter"

echo "============================================="
echo "股票形态筛选系统 - 服务器部署"
echo "============================================="
echo ""

# 测试 SSH 连接
echo "🔑 测试 SSH 连接..."
if ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i $SSH_KEY $SERVER "echo 成功" >/dev/null 2>&1; then
    echo "✅ SSH 连接成功"
else
    echo "❌ SSH 连接失败"
    exit 1
fi

echo ""
echo "📋 部署步骤:"
echo "1. 停止旧容器（如果有）"
echo "2. 构建 Docker 镜像"
echo "3. 启动新容器"
echo "4. 查看状态"
echo ""
echo "⚠️  注意：Docker 构建可能需要 5-10 分钟"
echo ""
echo "============================================="
echo "开始部署..."
echo "============================================="

# 步骤 1: 停止旧容器
echo "🛑 [1/4] 停止旧容器..."
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER \
    "cd $PROJECT_DIR && docker-compose down" || echo "⚠️  没有运行中的容器"

# 步骤 2: 构建镜像（后台执行）
echo "🏗️  [2/4] 构建 Docker 镜像..."
echo "💡 提示：构建过程在后台执行，请稍候..."

ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER << 'ENDSSH'
cd /root/stockfilter
nohup docker-compose build --no-cache > /tmp/build.log 2>&1 &
echo "构建进程已启动，PID: $!"
ENDSSH

echo "⏳ 等待构建完成（约 5-10 分钟）..."
echo "💡 你可以在服务器上查看构建日志：tail -f /tmp/build.log"
echo ""

# 等待构建
sleep 300  # 等待 5 分钟

# 步骤 3: 启动容器
echo "🚀 [3/4] 启动容器..."
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER \
    "cd $PROJECT_DIR && docker-compose up -d"

# 等待启动
sleep 5

# 步骤 4: 查看状态
echo "📊 [4/4] 查看容器状态..."
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER \
    "docker ps -f name=stockfilter-app --format 'table {{.Names}}\t{{.Status}}'"

echo ""
echo "📝 最近日志:"
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER \
    "docker logs --tail 20 stockfilter-app" 2>/dev/null || echo "暂无日志"

echo ""
echo "============================================="
echo "✅ 部署完成！"
echo "============================================="
echo ""
echo "📌 管理命令:"
echo "  查看状态：ssh -i $SSH_KEY $SERVER 'docker ps -f name=stockfilter-app'"
echo "  查看日志：ssh -i $SSH_KEY $SERVER 'docker logs -f stockfilter-app'"
echo "  重启：ssh -i $SSH_KEY $SERVER 'docker restart stockfilter-app'"
echo "  停止：ssh -i $SSH_KEY $SERVER 'docker-compose -c $PROJECT_DIR down'"
echo ""
echo "📖 详细文档：SERVER_DEPLOYMENT.md"
echo "============================================="
