#!/bin/bash

# ============================================
# Docker 构建缓存清理脚本
# 用于定期清理 Docker 构建缓存
# ============================================

set -e  # 遇到错误立即退出

echo "============================================="
echo "Docker 构建缓存清理"
echo "============================================="
echo ""

# 清理前统计
echo "📊 清理前 Build Cache 使用情况:"
docker system df | grep "Build Cache" || echo "无法获取 Build Cache 信息"
echo ""

# 检查是否有运行中的容器
RUNNING_CONTAINERS=$(docker ps -q | wc -l)
echo "🔍 发现 ${RUNNING_CONTAINERS} 个运行中的容器"
echo ""

# 清理构建缓存
echo "🧹 清理 Docker 构建缓存..."
CLEANED_SIZE=$(docker builder prune -f 2>&1 | grep "Total:" | awk '{print $2}')

echo ""
echo "✅ 已清理构建缓存：${CLEANED_SIZE:-未知}"
echo ""

# 清理后统计
echo "📊 清理后 Build Cache 使用情况:"
docker system df | grep "Build Cache" || echo "Build Cache 已清空"
echo ""

echo "============================================="
echo "清理完成！"
echo "============================================="
echo ""
echo "💡 提示："
echo "  - 构建缓存清理不会影响运行中的容器"
echo "  - 下次构建镜像时会重新创建缓存"
echo "  - 建议每周清理一次"
echo ""
