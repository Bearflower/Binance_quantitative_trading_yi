#!/bin/bash

# ============================================
# V6.13.3 一键部署脚本
# 优化内容:
# 1. 缩小止损距离：3-7% → 2-4%
# 2. 优化 ATR 计算：ATR * 1.5
# 3. 新增持仓时间平仓：48h/72h
# ============================================

set -e

# 加载配置
source .deploy_config

echo "============================================="
echo "🚀 V6.13.3 一键部署"
echo "目标服务器：$SERVER_IP"
echo "============================================="

# 步骤 1：打包
echo "📦 步骤 1/4: 打包项目..."
./auto_package.sh

# 步骤 2：上传
echo "📤 步骤 2/4: 上传到服务器..."
./upload_to_server.sh

# 步骤 3：远程部署
echo "🚀 步骤 3/4: 远程部署..."

# 使用 SSH 执行远程部署命令（使用密钥认证）
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" << 'ENDSSH'
    
# 在服务器上执行的命令
set -e

echo "============================================="
echo "V6.13.3 远程部署开始"
echo "============================================="

# 1. 停止并删除旧容器
echo "🛑 停止旧容器..."
docker stop $DOCKER_CONTAINER_NAME 2>/dev/null || true
docker rm $DOCKER_CONTAINER_NAME 2>/dev/null || true

# 2. 解压新包
echo "📦 解压新版本..."
cd /root
mkdir -p $PROJECT_NAME
tar -xzf $DEPLOY_PACKAGE_NAME -C $PROJECT_NAME --strip-components=0
cd $PROJECT_NAME

# 3. 设置权限
echo "🔒 设置权限..."
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true

# 4. 运行数据库迁移（创建 time_close_logs 表）
echo "🗄️  运行数据库迁移..."
python3 database/migrations/create_time_close_logs.py || true

# 5. 构建并启动
echo "🏗️  构建 Docker 镜像..."
docker-compose build --no-cache

echo "🚀 启动容器..."
docker-compose up -d

# 6. 等待启动
sleep 5

# 7. 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=$DOCKER_CONTAINER_NAME
echo "============================================="
echo "最近日志:"
docker logs --tail 50 $DOCKER_CONTAINER_NAME
echo "============================================="

ENDSSH

# 步骤 4：验证
echo "✅ 步骤 4/4: 验证部署..."
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "docker ps -f name=$DOCKER_CONTAINER_NAME --format '容器 {{.Names}} 状态：{{.Status}}'"

echo "============================================="
echo "🎉 V6.13.3 部署完成！"
echo "============================================="
echo ""
echo "V6.13.3 核心优化:"
echo "✅ 止损距离：2-4% (从 3-7% 下调)"
echo "✅ ATR 倍数：1.5× (更科学)"
echo "✅ 持仓时间平仓：48h/72h (新增)"
echo ""
echo "监控重点:"
echo "- 时间平仓执行次数"
echo "- 止损/止盈触发比例"
echo "- 平均持仓时间变化"
echo "============================================="
