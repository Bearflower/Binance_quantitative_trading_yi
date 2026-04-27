#!/bin/bash

# ============================================
# 一键部署脚本（增强版 - 确保容器更新到最新版本）
# ============================================

set -e

# 加载配置
source .deploy_config

echo "============================================="
echo "一键部署 - $PROJECT_NAME"
echo "目标服务器：$SERVER_IP"
echo "============================================="

# 步骤 1：打包
echo "📦 步骤 1/5: 打包项目..."
./auto_package.sh

# 步骤 2：上传
echo "📤 步骤 2/5: 上传到服务器..."
./upload_to_server.sh

# 步骤 3：远程部署
echo "🚀 步骤 3/5: 远程部署..."

# 使用 SSH 执行远程部署命令（使用别名）
ssh grid-signal-bot << ENDSSH
    
# 在服务器上执行的命令
set -e

echo "============================================="
echo "远程部署开始"
echo "============================================="

# 1. 停止并删除旧容器
echo "🛑 停止旧容器..."
if docker ps -q -f name=$DOCKER_CONTAINER_NAME | grep -q .; then
    docker stop $DOCKER_CONTAINER_NAME
    echo "✅ 容器已停止"
else
    echo "⚠️  容器未运行，跳过停止"
fi

echo "🗑️  删除旧容器..."
if docker ps -aq -f name=$DOCKER_CONTAINER_NAME | grep -q .; then
    docker rm $DOCKER_CONTAINER_NAME
    echo "✅ 容器已删除"
else
    echo "⚠️  容器不存在，跳过删除"
fi

# 2. 删除旧镜像（关键步骤，防止使用缓存）
echo "🗑️  删除旧镜像（防止使用缓存）..."
if docker images -q $DOCKER_IMAGE_NAME | grep -q .; then
    docker rmi $DOCKER_IMAGE_NAME --force 2>/dev/null || true
    echo "✅ 旧镜像已删除"
else
    echo "⚠️  旧镜像不存在，跳过删除"
fi

# 3. 创建项目目录（如果不存在）
echo "📁 创建项目目录..."
mkdir -p $SERVER_PROJECT_PATH

# 4. 解压新包
echo "📦 解压新代码包..."
cd /root
tar -xzf $DEPLOY_PACKAGE_NAME -C $SERVER_PROJECT_PATH
echo "✅ 代码包已解压"

# 5. 设置权限
cd $SERVER_PROJECT_PATH
chmod +x deploy.sh 2>/dev/null || true
chmod 600 config/.env 2>/dev/null || true
echo "✅ 权限已设置"

# 6. 清理 Docker 缓存（可选，如果磁盘空间紧张）
echo "🧹 清理 Docker 悬空镜像..."
docker image prune -f --filter "until=24h" 2>/dev/null || true

# 7. 构建并启动（不使用缓存）
echo "🏗️  构建 Docker 镜像（不使用缓存）..."
docker-compose build --no-cache
if [ \$? -ne 0 ]; then
    echo "❌ Docker 构建失败！"
    exit 1
fi
echo "✅ Docker 镜像构建成功"

echo "🚀 启动 Docker 容器..."
docker-compose up -d
if [ \$? -ne 0 ]; then
    echo "❌ Docker 容器启动失败！"
    exit 1
fi
echo "✅ Docker 容器启动成功"

# 8. 等待容器启动
echo "⏳ 等待容器启动..."
sleep 5

# 9. 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=$DOCKER_CONTAINER_NAME
echo "============================================="
echo "最近日志:"
docker logs --tail 30 $DOCKER_CONTAINER_NAME
echo "============================================="
ENDSSH

# 检查远程部署是否成功
if [ $? -ne 0 ]; then
    echo "❌ 远程部署失败！"
    exit 1
fi

# 步骤 4：验证部署（关键步骤，确保容器更新到最新版本）
echo "✅ 步骤 4/5: 验证部署（关键步骤，确保容器更新到最新版本）..."

# 在服务器上执行验证
ssh grid-signal-bot << 'VERIFYEOF'

DOCKER_CONTAINER_NAME="$DOCKER_CONTAINER_NAME"
PROJECT_NAME="$PROJECT_NAME"

echo "============================================="
echo "🔍 部署验证 - 确保容器更新到最新版本"
echo "============================================="

# 1. 验证容器是否在运行
echo "1️⃣  验证容器运行状态..."
CONTAINER_STATUS=$(docker ps -f name=$DOCKER_CONTAINER_NAME --format '{{.Status}}')
if [ -z "$CONTAINER_STATUS" ]; then
    echo "❌ 容器未运行！部署失败！"
    exit 1
fi
echo "✅ 容器运行状态：$CONTAINER_STATUS"

# 2. 验证容器镜像版本（关键）
echo ""
echo "2️⃣  验证容器镜像版本（确保是最新版本）..."
CONTAINER_IMAGE=$(docker inspect -f '{{.Config.Image}}' $DOCKER_CONTAINER_NAME 2>/dev/null)
IMAGE_CREATED=$(docker inspect -f '{{.Created}}' $DOCKER_CONTAINER_NAME 2>/dev/null)
echo "   容器镜像：$CONTAINER_IMAGE"
echo "   镜像创建时间：$IMAGE_CREATED"

# 获取本地最新镜像
LOCAL_IMAGE=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep $PROJECT_NAME | head -1)
LOCAL_IMAGE_CREATED=$(docker inspect -f '{{.Created}}' $LOCAL_IMAGE 2>/dev/null)
echo "   本地最新镜像：$LOCAL_IMAGE"
echo "   本地镜像创建时间：$LOCAL_IMAGE_CREATED"

# 比较镜像创建时间
if [ "$IMAGE_CREATED" = "$LOCAL_IMAGE_CREATED" ]; then
    echo "✅ 容器使用的是最新镜像版本"
else
    # 检查时间差（300 秒 = 5 分钟）
    IMAGE_TIMESTAMP=$(date -d "$IMAGE_CREATED" +%s 2>/dev/null || echo "0")
    LOCAL_TIMESTAMP=$(date -d "$LOCAL_IMAGE_CREATED" +%s 2>/dev/null || echo "0")
    TIME_DIFF=$((LOCAL_TIMESTAMP - IMAGE_TIMESTAMP))
    
    if [ $TIME_DIFF -lt 0 ]; then
        TIME_DIFF=$((-TIME_DIFF))
    fi
    
    if [ $TIME_DIFF -le 300 ]; then
        echo "✅ 容器使用的是最新镜像版本（时间差：${TIME_DIFF}秒）"
    else
        echo "⚠️  警告：容器可能未使用最新镜像！"
        echo "   容器镜像创建时间：$IMAGE_CREATED"
        echo "   最新镜像创建时间：$LOCAL_IMAGE_CREATED"
        echo "   时间差：${TIME_DIFF}秒"
        exit 1
    fi
fi

# 3. 验证容器健康状态
echo ""
echo "3️⃣  验证容器健康状态..."
HEALTH_STATUS=$(docker inspect -f '{{.State.Health.Status}}' $DOCKER_CONTAINER_NAME 2>/dev/null || echo "无健康检查")
if [ "$HEALTH_STATUS" = "healthy" ] || [ "$HEALTH_STATUS" = "无健康检查" ]; then
    echo "✅ 容器健康状态：$HEALTH_STATUS"
else
    echo "⚠️  容器健康状态：$HEALTH_STATUS"
fi

# 4. 验证容器日志（检查是否有启动错误）
echo ""
echo "4️⃣  验证容器日志（检查启动错误）..."
ERROR_COUNT=$(docker logs --tail 100 $DOCKER_CONTAINER_NAME 2>&1 | grep -i "error\|exception\|fatal" | wc -l)
if [ "$ERROR_COUNT" -gt 0 ]; then
    echo "⚠️  发现 $ERROR_COUNT 个错误日志，请检查："
    docker logs --tail 20 $DOCKER_CONTAINER_NAME
    exit 1
else
    echo "✅ 未发现明显错误日志"
fi

# 5. 最终验证总结
echo ""
echo "============================================="
echo "📋 验证总结"
echo "============================================="
echo "容器名称：$DOCKER_CONTAINER_NAME"
echo "运行状态：$CONTAINER_STATUS"
echo "镜像版本：$CONTAINER_IMAGE"
echo "健康状态：$HEALTH_STATUS"
echo "错误日志：$ERROR_COUNT 个"
echo "============================================="

if [ "$ERROR_COUNT" -eq 0 ] && [ -n "$CONTAINER_STATUS" ]; then
    echo "✅ 验证通过！容器已成功更新到最新版本！"
    exit 0
else
    echo "❌ 验证失败！请检查上述错误！"
    exit 1
fi
VERIFYEOF

# 检查验证结果
if [ $? -eq 0 ]; then
    echo "============================================="
    echo "🎉 一键部署完成！验证通过！"
    echo "============================================="
else
    echo "============================================="
    echo "⚠️  部署完成但验证失败！请检查上述错误！"
    echo "============================================="
    echo ""
    echo "🔧 建议执行以下命令重新部署："
    echo ""
    echo "ssh grid-signal-bot << 'EOF'"
    echo "cd /root/$PROJECT_NAME"
    echo "docker-compose down"
    echo "docker rmi $DOCKER_IMAGE_NAME --force"
    echo "docker-compose build --no-cache"
    echo "docker-compose up -d"
    echo "EOF"
    echo ""
    exit 1
fi

# 步骤 5：清理临时文件
echo ""
echo "📤 步骤 5/5: 清理临时文件..."
rm -f deployment_package.tar.gz
ssh grid-signal-bot "rm -f /root/$DEPLOY_PACKAGE_NAME"
echo "✅ 临时文件已清理"

echo ""
echo "============================================="
echo "🎉 部署全部完成！"
echo "============================================="
