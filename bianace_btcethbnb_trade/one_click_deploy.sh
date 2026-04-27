#!/bin/bash

# ============================================
# 一键部署脚本 - 打包 + 上传 + 部署
# ============================================

set -e

# 加载配置
source .deploy_config

echo "============================================="
echo "一键部署 - $PROJECT_NAME"
echo "目标服务器：$SERVER_IP"
echo "============================================="

# 步骤 1：打包
echo "📦 步骤 1/4: 打包项目..."
if [ ! -f "./auto_package.sh" ]; then
    echo "❌ 错误：auto_package.sh 不存在"
    exit 1
fi
./auto_package.sh

# 步骤 2：上传
echo "📤 步骤 2/4: 上传到服务器..."
if [ ! -f "./upload_to_server.sh" ]; then
    echo "❌ 错误：upload_to_server.sh 不存在"
    exit 1
fi
./upload_to_server.sh

# 步骤 3：远程部署
echo "🚀 步骤 3/4: 远程部署..."

# 构建 SSH 命令
SSH_CMD="ssh"
if [ -n "$SSH_KEY_FILE" ] && [ -f "$SSH_KEY_FILE" ]; then
    SSH_CMD="$SSH_CMD -i $SSH_KEY_FILE"
fi
SSH_CMD="$SSH_CMD -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $SERVER_USER@$SERVER_IP"

# 使用 SSH 执行远程部署命令（使用密钥认证）
$SSH_CMD << ENDSSH
    
# 在服务器上执行的命令
set -e

PROJECT_NAME="$PROJECT_NAME"
DOCKER_CONTAINER_NAME="$DOCKER_CONTAINER_NAME"
DEPLOY_PACKAGE_NAME="$DEPLOY_PACKAGE_NAME"

# 验证部署包是否存在
echo "🔍 验证部署包..."
if [ ! -f "$DEPLOY_PACKAGE_NAME" ]; then
    echo "❌ 错误：部署包不存在"
    exit 1
fi

echo "📥 下载部署包到项目目录..."
cd /root

# 停止并删除旧容器
echo "🛑 停止旧容器..."
docker stop $DOCKER_CONTAINER_NAME 2>/dev/null || true
docker rm $DOCKER_CONTAINER_NAME 2>/dev/null || true

# 清理旧的项目目录（保留日志和数据）
echo "🧹 清理旧代码..."
if [ -d "$PROJECT_NAME" ]; then
    # 备份重要文件
    if [ -f "$PROJECT_NAME/.env" ]; then
        cp $PROJECT_NAME/.env /root/.env.backup
    fi
    if [ -d "$PROJECT_NAME/logs" ]; then
        cp -r $PROJECT_NAME/logs /root/logs.backup
    fi
    if [ -d "$PROJECT_NAME/data" ]; then
        cp -r $PROJECT_NAME/data /root/data.backup
    fi
    
    # 删除旧目录
    rm -rf $PROJECT_NAME
fi

# 创建新目录
echo "📁 创建项目目录..."
mkdir -p $PROJECT_NAME

# 解压新包
echo "📦 解压部署包..."
tar -xzf $DEPLOY_PACKAGE_NAME -C $PROJECT_NAME

# 恢复配置文件
if [ -f /root/.env.backup ]; then
    cp /root/.env.backup $PROJECT_NAME/.env
    rm /root/.env.backup
fi

# 设置权限
echo "🔐 设置权限..."
cd $PROJECT_NAME
chmod +x deploy.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true
chmod +x scheduler_new.py 2>/dev/null || true

# 显示更新的文件
echo "📋 更新的文件:"
ls -la

# 构建并启动
echo "🏗️  构建 Docker 镜像..."
docker-compose build --no-cache

echo "🚀 启动容器..."
docker-compose up -d

# 等待启动
sleep 5

# 显示状态
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

# 构建 SSH 命令（如果还没有构建）
if [ -z "$SSH_CMD" ]; then
    SSH_CMD="ssh"
    if [ -n "$SSH_KEY_FILE" ] && [ -f "$SSH_KEY_FILE" ]; then
        SSH_CMD="$SSH_CMD -i $SSH_KEY_FILE"
    fi
    SSH_CMD="$SSH_CMD -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $SERVER_USER@$SERVER_IP"
fi

$SSH_CMD "docker ps -f name=$DOCKER_CONTAINER_NAME --format '容器 {{.Names}} 状态：{{.Status}}'"

echo "============================================="
echo "🎉 一键部署完成！"
echo "============================================="
echo ""
echo "📝 查看日志："
echo "   ssh root@$SERVER_IP \"docker logs -f $DOCKER_CONTAINER_NAME\""
echo ""
echo "🔄 重启容器："
echo "   ssh root@$SERVER_IP \"docker restart $DOCKER_CONTAINER_NAME\""
echo ""
