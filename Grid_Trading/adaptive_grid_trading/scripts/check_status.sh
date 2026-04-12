#!/bin/bash

# ============================================
# 系统管理脚本 - 查看运行状态
# ============================================

SERVER="root@43.156.242.184"
CONTAINER="grid-trading"

echo "============================================="
echo "📊 自适应网格交易系统 - 运行状态"
echo "============================================="
echo ""

# 1. Docker 容器状态
echo "🐳 Docker 容器状态:"
ssh -o StrictHostKeyChecking=no $SERVER "docker ps -f name=$CONTAINER --format '容器 ID: {{.ID}}\n镜像：{{.Image}}\n状态：{{.Status}}\n端口：{{.Ports}}'"
echo ""

# 2. 系统资源使用
echo "💾 系统资源使用:"
ssh -o StrictHostKeyChecking=no $SERVER "docker stats $CONTAINER --no-stream --format 'CPU: {{.CPUPerc}}\n内存：{{.MemUsage}}\n网络：{{.NetIO}}'"
echo ""

# 3. 最新日志（最后 10 条）
echo "📋 最新日志:"
ssh -o StrictHostKeyChecking=no $SERVER "docker logs --tail 10 $CONTAINER 2>&1"
echo ""

# 4. 关键事件统计
echo "📈 今日关键事件:"
ssh -o StrictHostKeyChecking=no $SERVER "docker logs $CONTAINER 2>&1 | grep \"$(date +%Y-%m-%d)\" | grep -E '(网格创建|参数调整|移动止盈|硬止损|紧急暂停)' | wc -l"
echo "个事件"
echo ""

# 5. 运行时长
echo "⏱️  运行时长:"
ssh -o StrictHostKeyChecking=no $SERVER "docker inspect $CONTAINER --format='{{.State.StartedAt}}' | xargs -I {} bash -c 'echo \"容器启动时间：{}\"'"
echo ""

echo "============================================="
echo "✅ 状态检查完成"
echo "============================================="
