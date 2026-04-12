#!/bin/bash

# ============================================
# v6.12 频率控制更新部署脚本
# ============================================

set -e

# 加载配置
if [ -f ".deploy_config" ]; then
    source .deploy_config
    echo "✅ 已加载部署配置"
else
    echo "❌ 错误：.deploy_config 文件不存在"
    exit 1
fi

SERVER_IP="43.156.242.184"
SERVER_USER="root"
PROJECT_PATH="/root/binance-trade-analyzer"
CONTAINER_NAME="binance-trade-analyzer"

echo "============================================="
echo "v6.12 频率控制更新部署"
echo "目标服务器：$SERVER_IP"
echo "============================================="

# 步骤 1：打包项目
echo "📦 步骤 1/4: 打包项目..."
if [ -f "auto_package.sh" ]; then
    ./auto_package.sh
else
    echo "❌ auto_package.sh 不存在"
    exit 1
fi

# 步骤 2：上传到服务器
echo "📤 步骤 2/4: 上传到服务器..."
if [ -f "upload_to_server.sh" ]; then
    ./upload_to_server.sh
else
    # 直接 SCP 上传
    scp -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        deployment_package.tar.gz \
        "$SERVER_USER@$SERVER_IP:/root/"
    echo "✅ 上传成功"
fi

# 步骤 3：远程部署
echo "🚀 步骤 3/4: 远程部署..."

ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" << 'ENDSSH'

set -e

PROJECT_PATH="/root/binance-trade-analyzer"
CONTAINER_NAME="binance-trade-analyzer"
DEPLOY_PACKAGE="deployment_package.tar.gz"

echo "在服务器上执行部署..."

# 停止旧容器
echo "停止旧容器..."
docker stop $CONTAINER_NAME 2>/dev/null || true
docker rm $CONTAINER_NAME 2>/dev/null || true

# 解压新包
echo "解压新版本..."
cd /root
tar -xzf $DEPLOY_PACKAGE -C $PROJECT_PATH

# 设置权限
cd $PROJECT_PATH
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true

# 重新构建并启动
echo "重新构建 Docker 镜像..."
docker-compose build --no-cache

echo "启动新容器..."
docker-compose up -d

# 等待启动
sleep 5

# 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=$CONTAINER_NAME
echo "============================================="
echo "最近日志:"
docker logs --tail 50 $CONTAINER_NAME

ENDSSH

# 步骤 4：验证部署
echo "✅ 步骤 4/4: 验证部署..."

ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "docker ps -f name=$CONTAINER_NAME --format '容器 {{.Names}} 状态：{{.Status}}'"

echo "============================================="
echo "🎉 v6.12 频率控制更新部署完成！"
echo "============================================="
echo ""
echo "更新内容："
echo "✅ 新增 services/frequency_controller.py - 频率控制模块"
echo "✅ 更新 scheduler_new.py - 集成频率检查逻辑"
echo "✅ 新增 trade_records 表 - 交易记录跟踪"
echo ""
echo "频率控制参数（v6.12）："
echo "✅ 每日最大总交易数：4 笔"
echo "✅ 单品种每日最大交易数：2 笔"
echo "✅ 同品种冷却期：12 小时"
echo "✅ 连续亏损暂停：5 笔亏损暂停 1 天"
echo "✅ 每日最大亏损限额：25U（500U * 5%）"
echo ""
echo "============================================="
