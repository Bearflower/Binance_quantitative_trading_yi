#!/bin/bash

# ============================================
# Docker 空间清理脚本
# 用于清理 Docker 构建缓存和悬空镜像
# ============================================

set -e  # 遇到错误立即退出

echo "============================================="
echo "Docker 空间清理"
echo "============================================="
echo ""

# 清理前统计
echo "📊 清理前空间使用情况:"
docker system df
echo ""

# 检查是否有运行中的容器
RUNNING_CONTAINERS=$(docker ps -q | wc -l)
echo "🔍 发现 ${RUNNING_CONTAINERS} 个运行中的容器"
echo ""

# 1. 清理构建缓存
echo "🧹 步骤 1/2: 清理 Docker 构建缓存..."
BUILDER_CLEAN=$(docker builder prune -f 2>&1 | grep "Total:" | awk '{print $2}')
echo "✅ 已清理构建缓存：${BUILDER_CLEAN:-未知}"
echo ""

# 2. 清理悬空镜像
echo "🧹 步骤 2/2: 清理悬空镜像..."
IMAGE_CLEAN=$(docker image prune -f 2>&1 | grep "Total reclaimed space:" | awk '{print $NF}')
echo "✅ 已清理悬空镜像：${IMAGE_CLEAN:-未知}"
echo ""

# 清理后统计
echo "📊 清理后空间使用情况:"
docker system df
echo ""

# 显示释放的空间
echo "============================================="
echo "清理完成！"
echo "============================================="
echo ""
echo "💡 提示："
echo "  - 构建缓存：下次构建镜像时会重新创建"
echo "  - 悬空镜像：不影响运行中的容器"
echo "  - 建议每周执行一次此清理脚本"
echo ""
