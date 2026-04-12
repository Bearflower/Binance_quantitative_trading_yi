#!/bin/bash

# 部署脚本

echo "============================================="
echo "部署 AI 编辑部系统"
echo "============================================="

# 检查Docker是否运行
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker 未运行，请启动 Docker 服务"
    exit 1
fi

# 停止并删除旧容器
echo "🛑 停止并删除旧容器..."
docker stop ai-editorial-system 2>/dev/null || true
docker rm ai-editorial-system 2>/dev/null || true

# 构建新镜像
echo "🏗️  构建新镜像..."
docker-compose build --no-cache

# 启动容器
echo "🚀 启动容器..."
docker-compose up -d

# 等待启动
sleep 5

# 检查容器状态
echo "📊 检查容器状态..."
docker ps -f name=ai-editorial-system

# 查看日志
echo "📋 查看最近日志..."
docker logs --tail 50 ai-editorial-system

echo "============================================="
echo "🎉 部署完成！"
echo "============================================="
