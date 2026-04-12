#!/bin/bash

# ============================================
# AI 编辑部系统工作流执行脚本
# ============================================

set -e

echo "========================================="
echo "执行时间：$(date)"
echo "========================================="

# 服务器配置
SERVER_PORT="8888"

# 执行文章创建工作流
echo ""
echo "📝 执行文章创建工作流..."
curl -s -X POST "http://localhost:$SERVER_PORT/api/workflows" \
  -H "Content-Type: application/json" \
  -d '{"workflow_type": "article_creation", "parameters": {}}'

# 执行热点监测工作流
echo ""
echo "🔥 执行热点监测工作流..."
curl -s -X POST "http://localhost:$SERVER_PORT/api/workflows" \
  -H "Content-Type: application/json" \
  -d '{"workflow_type": "topic_monitoring", "parameters": {}}'

echo ""
echo "========================================="
echo "工作流执行完成！"
echo "========================================="
