#!/bin/bash

# ============================================
# 悬空镜像清理脚本
# 用于手动清理 Docker 悬空镜像
# ============================================

set -e  # 遇到错误立即退出

echo "============================================="
echo "Docker 悬空镜像清理"
echo "============================================="
echo ""

# 清理前统计
echo "📊 清理前悬空镜像统计:"
DANGLING_COUNT=$(docker images --filter "dangling=true" -q 2>/dev/null | wc -l)
DANGLING_SIZE=$(docker images --filter "dangling=true" 2>/dev/null | awk 'NR>1 {sum+=$4} END {print sum}')

echo "  悬空镜像数量：${DANGLING_COUNT} 个"
echo "  占用空间：${DANGLING_SIZE:-0}MB"
echo ""

if (( DANGLING_COUNT == 0 )); then
    echo "✅ 没有悬空镜像，无需清理"
    exit 0
fi

# 显示悬空镜像列表
echo "📋 悬空镜像列表:"
docker images --filter "dangling=true" --format "table {{.ID}}\t{{.Size}}\t{{.CreatedSince}}"
echo ""

# 确认清理
echo "⚠️  警告：此操作将删除所有悬空镜像（<none> 标签）"
echo "    这不会影响正在运行的容器，但可能影响快速回滚能力"
echo ""
read -p "确定要清理这些悬空镜像吗？(y/N): " confirm

if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "❌ 操作已取消"
    exit 0
fi

echo ""
echo "🧹 开始清理悬空镜像..."
CLEANED_SIZE=$(docker image prune -f 2>&1 | grep "Total reclaimed space:" | awk '{print $NF}')

echo ""
echo "============================================="
echo "清理完成！"
echo "============================================="
echo ""

# 清理后统计
AFTER_COUNT=$(docker images --filter "dangling=true" -q 2>/dev/null | wc -l)
echo "📊 清理后统计:"
echo "  删除悬空镜像：${DANGLING_COUNT} 个"
echo "  释放空间：${CLEANED_SIZE:-未知}"
echo "  剩余悬空镜像：${AFTER_COUNT} 个"
echo ""

if (( AFTER_COUNT == 0 )); then
    echo "✅ 所有悬空镜像已清理完成"
else
    echo "⚠️  仍有 ${AFTER_COUNT} 个悬空镜像未清理"
fi

echo ""
echo "💡 提示："
echo "  - 悬空镜像不会影响运行中的容器"
echo "  - 保留悬空镜像可以快速回滚到旧版本"
echo "  - 建议定期（如每月）清理一次"
echo ""
