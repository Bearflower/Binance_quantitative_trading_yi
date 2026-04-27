#!/bin/bash

# ============================================
# v3.1 Docker 部署脚本
# ============================================

set -e

SERVER="root@43.156.242.184"
PROJECT_DIR="/root/short_selling_system"
CONTAINER_NAME="short_selling_system_v31"

echo "============================================="
echo "🚀 v3.1 Docker 部署"
echo "============================================="

# 1. 停止并删除旧容器
echo "🛑 停止并删除旧容器..."
ssh $SERVER "docker stop $CONTAINER_NAME 2>/dev/null || true"
ssh $SERVER "docker rm $CONTAINER_NAME 2>/dev/null || true"

# 2. 创建项目目录
echo "📁 创建项目目录..."
ssh $SERVER "mkdir -p $PROJECT_DIR"

# 3. 上传文件
echo "📤 上传文件到服务器..."
scp Dockerfile docker-compose.simple.yml requirements.txt $SERVER:$PROJECT_DIR/
scp -r core utils $SERVER:$PROJECT_DIR/ 2>/dev/null || true
scp main_v31.py $SERVER:$PROJECT_DIR/

# 4. 创建日志目录
echo "📂 创建日志目录..."
ssh $SERVER "mkdir -p $PROJECT_DIR/{logs,reports,data}"

# 5. 构建并启动容器
echo "🏗️  构建 Docker 镜像..."
ssh $SERVER "cd $PROJECT_DIR && docker-compose -f docker-compose.simple.yml build --no-cache"

echo "🚀 启动 Docker 容器..."
ssh $SERVER "cd $PROJECT_DIR && docker-compose -f docker-compose.simple.yml up -d"

# 6. 等待启动
echo "⏳ 等待容器启动..."
sleep 5

# 7. 查看状态
echo "📊 容器状态:"
ssh $SERVER "docker ps -f name=$CONTAINER_NAME"

echo ""
echo "📋 最近日志:"
ssh $SERVER "docker logs --tail 30 $CONTAINER_NAME"

echo ""
echo "============================================="
echo "✅ 部署完成！"
echo "============================================="
echo ""
echo "常用命令:"
echo "  查看状态：ssh $SERVER 'docker ps -f name=$CONTAINER_NAME'"
echo "  查看日志：ssh $SERVER 'docker logs -f $CONTAINER_NAME'"
echo "  重启容器：ssh $SERVER 'docker restart $CONTAINER_NAME'"
echo "  停止容器：ssh $SERVER 'docker stop $CONTAINER_NAME'"
echo ""
