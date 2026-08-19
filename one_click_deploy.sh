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

# 使用 SSH 执行远程部署命令（使用密钥认证）
ssh -i "$SSH_KEY_PATH" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" << ENDSSH

# 在服务器上执行的命令
set -e

echo "============================================="
echo "远程部署开始"
echo "============================================="

# 1. 创建项目目录（如果不存在）
echo "📁 创建项目目录..."
mkdir -p $SERVER_PROJECT_PATH
mkdir -p $SERVER_PROJECT_PATH/logs
mkdir -p $SERVER_PROJECT_PATH/data
mkdir -p $SERVER_PROJECT_PATH/database/postgres/init-scripts
mkdir -p $SERVER_PROJECT_PATH/database/postgres/scripts
mkdir -p $SERVER_PROJECT_PATH/database/postgres/backups

# 2. 停止并删除旧容器
echo "🛑 停止旧容器..."
cd $SERVER_PROJECT_PATH

# 停止所有策略容器
# 注意：docker ps -f name= 是前缀匹配，如 trading_system-kline 会匹配到
# trading_system-kline-monitor，在精确容器不存在时 docker stop 会失败
# 因此使用 || true 防止 set -e 退出
for container in $BTC_ETH_CONTAINER_NAME $NEW_COIN_CONTAINER_NAME $GRID_CONTAINER_NAME $HRS_CONTAINER_NAME $AI_TUNER_CONTAINER_NAME $KLINE_CONTAINER_NAME $KLINE_MONITOR_CONTAINER_NAME; do
    if docker ps -q -f name=\$container | grep -q .; then
        docker stop \$container || true
        echo "✅ 容器 \$container 已停止"
    else
        echo "⚠️  容器 \$container 未运行，跳过停止"
    fi
done

# 停止 PostgreSQL 容器
if docker ps -q -f name=$POSTGRES_CONTAINER_NAME | grep -q .; then
    echo "⚠️  PostgreSQL 容器正在运行，保持运行状态"
else
    echo "⚠️  PostgreSQL 容器未运行"
fi

echo "🗑️  删除旧容器..."
for container in $BTC_ETH_CONTAINER_NAME $NEW_COIN_CONTAINER_NAME $GRID_CONTAINER_NAME $HRS_CONTAINER_NAME $AI_TUNER_CONTAINER_NAME $KLINE_CONTAINER_NAME $KLINE_MONITOR_CONTAINER_NAME; do
    if docker ps -aq -f name=\$container | grep -q .; then
        docker rm \$container || true
        echo "✅ 容器 \$container 已删除"
    else
        echo "⚠️  容器 \$container 不存在，跳过删除"
    fi
done

# 周报容器删除（已废弃，保留为空占位以备清理）
:

# 3. 删除旧镜像（关键步骤，防止使用缓存）⭐⭐⭐
echo "🗑️  删除旧镜像（防止使用缓存）..."
for image in $BTC_ETH_IMAGE_NAME $NEW_COIN_IMAGE_NAME $GRID_IMAGE_NAME $HRS_IMAGE_NAME $AI_TUNER_IMAGE_NAME $KLINE_IMAGE_NAME $KLINE_MONITOR_IMAGE_NAME; do
    if docker images -q \$image | grep -q .; then
        docker rmi \$image --force 2>/dev/null || true
        echo "✅ 旧镜像 \$image 已删除"
    else
        echo "⚠️  旧镜像 \$image 不存在，跳过删除"
    fi
done

# 4. 解压新包
echo "📦 解压新代码包..."
cd /root
tar -xzf $DEPLOY_PACKAGE_NAME -C $SERVER_PROJECT_PATH
echo "✅ 代码包已解压"

# 5. 设置权限
cd $SERVER_PROJECT_PATH
chmod +x database/postgres/scripts/backup-postgres.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true
echo "✅ 权限已设置"

# 6. 清理 Docker 缓存（可选，如果磁盘空间紧张）
echo "🧹 清理 Docker 悬空镜像..."
docker image prune -f --filter "until=24h" 2>/dev/null || true

# 7. 创建 Docker 网络（如果不存在）
echo "🌐 创建 Docker 网络..."
if docker network ls | grep -q trading-network; then
    echo "✅ Docker 网络已存在"
else
    docker network create trading-network
    echo "✅ Docker 网络已创建"
fi

# 8. 启动 PostgreSQL（如果未运行）
if [ "$DEPLOY_POSTGRES" = true ]; then
    echo "🗄️  检查 PostgreSQL 容器..."
    if docker ps -q -f name=$POSTGRES_CONTAINER_NAME | grep -q .; then
        echo "✅ PostgreSQL 容器已在运行"
    else
        # 清理残留的旧容器（防止 docker-compose 命名冲突）
        # 根因：docker-compose 项目名变更时会产生带前缀的残留容器，
        # 如 b62539d01e8b_trading_system-postgres，导致 docker-compose up 失败
        echo "🧹 清理残留的 PostgreSQL 容器..."
        for stale in \$(docker ps -aq -f name=postgres 2>/dev/null); do
            docker rm -f \$stale 2>/dev/null || true
        done

        echo "🚀 启动 PostgreSQL 容器..."
        cd $SERVER_PROJECT_PATH
        docker-compose up -d postgres || {
            echo "⚠️  docker-compose 启动 postgres 失败，尝试直接创建..."
            docker run -d \
                --name $POSTGRES_CONTAINER_NAME \
                --network trading-network-v2 \
                -e POSTGRES_DB=trading_platform \
                -e POSTGRES_USER=trading_user \
                -e POSTGRES_PASSWORD=\${DATABASE_PASSWORD:-trading_password_2024} \
                -v postgres-data:/var/lib/postgresql/data \
                postgres:15-alpine
        }

        # 等待 PostgreSQL 启动
        echo "⏳ 等待 PostgreSQL 启动..."
        sleep 10

        # 检查 PostgreSQL 健康状态
        for i in {1..30}; do
            if docker exec $POSTGRES_CONTAINER_NAME pg_isready -U trading_user -d trading_platform > /dev/null 2>&1; then
                echo "✅ PostgreSQL 已就绪"
                break
            fi
            echo "   等待中... (\$i/30)"
            sleep 2
        done
    fi
fi

# 8.1 提前启动辅助服务（在构建策略之前启动，防止后续构建失败导致遗漏）
# 根因：主服务构建失败时 set -e 会退出脚本，导致后续辅助服务被跳过
echo "🔍 启动辅助服务（kline-monitor 等）..."
cd $SERVER_PROJECT_PATH
docker-compose up -d kline-monitor 2>/dev/null || true
echo "✅ 辅助服务已启动"

# 9. 先构建并启动基础服务（kline-service 等），策略服务依赖它们
# 根因：策略服务 depends_on kline-service (condition: service_healthy)，
# 如果 kline-service 未构建，docker-compose up -d 策略容器时会因依赖不满足而失败
if [ "$DEPLOY_KLINE" = true ]; then
    echo "🏗️  构建 K 线数据服务镜像（不使用缓存）..."
    cd $SERVER_PROJECT_PATH
    docker-compose build --no-cache kline-service
    if [ \$? -ne 0 ]; then
        echo "❌ K 线数据服务镜像构建失败！"
        exit 1
    fi
    echo "✅ K 线数据服务镜像构建成功"

    echo "🚀 启动 K 线数据服务容器..."
    docker-compose up -d kline-service
    if [ \$? -ne 0 ]; then
        echo "❌ K 线数据服务容器启动失败！"
        exit 1
    fi
    echo "✅ K 线数据服务容器启动成功"
fi

if [ "$DEPLOY_KLINE_MONITOR" = true ]; then
    echo "🏗️  构建 K 线服务健康监控镜像（不使用缓存）..."
    cd $SERVER_PROJECT_PATH
    docker-compose build --no-cache kline-monitor
    if [ \$? -ne 0 ]; then
        echo "❌ K 线服务健康监控镜像构建失败！"
        exit 1
    fi
    echo "✅ K 线服务健康监控镜像构建成功"

    echo "🚀 启动 K 线服务健康监控容器..."
    docker-compose up -d kline-monitor
    if [ \$? -ne 0 ]; then
        echo "❌ K 线服务健康监控容器启动失败！"
        exit 1
    fi
    echo "✅ K 线服务健康监控容器启动成功"
fi

# 10. 构建并启动策略容器（不使用缓存）
if [ "$DEPLOY_BTC_ETH" = true ]; then
    echo "🏗️  构建 BTC/ETH 策略镜像（不使用缓存）..."
    cd $SERVER_PROJECT_PATH
    docker-compose build --no-cache btc-eth-strategy
    if [ \$? -ne 0 ]; then
        echo "❌ BTC/ETH 策略镜像构建失败！"
        exit 1
    fi
    echo "✅ BTC/ETH 策略镜像构建成功"

    echo "🚀 启动 BTC/ETH 策略容器..."
    docker-compose up -d btc-eth-strategy
    if [ \$? -ne 0 ]; then
        echo "❌ BTC/ETH 策略容器启动失败！"
        exit 1
    fi
    echo "✅ BTC/ETH 策略容器启动成功"
fi

if [ "$DEPLOY_NEW_COIN" = true ]; then
    echo "🏗️  构建新币做空策略镜像（不使用缓存）..."
    cd $SERVER_PROJECT_PATH
    docker-compose build --no-cache new-coin-strategy
    if [ \$? -ne 0 ]; then
        echo "❌ 新币做空策略镜像构建失败！"
        exit 1
    fi
    echo "✅ 新币做空策略镜像构建成功"

    echo "🚀 启动新币做空策略容器..."
    docker-compose up -d new-coin-strategy
    if [ \$? -ne 0 ]; then
        echo "❌ 新币做空策略容器启动失败！"
        exit 1
    fi
    echo "✅ 新币做空策略容器启动成功"
fi

if [ "$DEPLOY_GRID" = true ]; then
    echo "🏗️  构建网格交易策略镜像（不使用缓存）..."
    cd $SERVER_PROJECT_PATH
    docker-compose build --no-cache grid-strategy
    if [ \$? -ne 0 ]; then
        echo "❌ 网格交易策略镜像构建失败！"
        exit 1
    fi
    echo "✅ 网格交易策略镜像构建成功"

    echo "🚀 启动网格交易策略容器..."
    docker-compose up -d grid-strategy
    if [ \$? -ne 0 ]; then
        echo "❌ 网格交易策略容器启动失败！"
        exit 1
    fi
    echo "✅ 网格交易策略容器启动成功"
fi

if [ "$DEPLOY_HRS" = true ]; then
    echo "🏗️  构建 HRS 混合反转策略镜像（不使用缓存）..."
    cd $SERVER_PROJECT_PATH
    docker-compose build --no-cache hrs-strategy
    if [ \$? -ne 0 ]; then
        echo "❌ HRS 混合反转策略镜像构建失败！"
        exit 1
    fi
    echo "✅ HRS 混合反转策略镜像构建成功"

    echo "🚀 启动 HRS 混合反转策略容器..."
    docker-compose up -d hrs-strategy
    if [ \$? -ne 0 ]; then
        echo "❌ HRS 混合反转策略容器启动失败！"
        exit 1
    fi
    echo "✅ HRS 混合反转策略容器启动成功"
fi

if [ "$DEPLOY_AI_TUNER" = true ]; then
    echo "🏗️  构建 StratTuneAI 调优镜像（不使用缓存）..."
    cd $SERVER_PROJECT_PATH
    docker-compose build --no-cache ai-tuner
    if [ \$? -ne 0 ]; then
        echo "❌ StratTuneAI 调优镜像构建失败！"
        exit 1
    fi
    echo "✅ StratTuneAI 调优镜像构建成功"

    echo "🚀 启动 StratTuneAI 调优容器..."
    docker-compose up -d ai-tuner
    if [ \$? -ne 0 ]; then
        echo "❌ StratTuneAI 调优容器启动失败！"
        exit 1
    fi
    echo "✅ StratTuneAI 调优容器启动成功"
fi

# 11. 等待容器启动
echo "⏳ 等待容器启动..."
sleep 5

# 11. 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=trading_system
echo "============================================="
echo "PostgreSQL 状态:"
docker ps -f name=$POSTGRES_CONTAINER_NAME
echo "============================================="

ENDSSH

# 检查远程部署是否成功
if [ $? -ne 0 ]; then
    echo "❌ 远程部署失败！"
    exit 1
fi

# 步骤 4：验证部署（关键步骤，确保容器更新到最新版本）⭐⭐⭐
echo "✅ 步骤 4/5: 验证部署..."
./verify_deployment.sh

# 检查验证结果
if [ $? -ne 0 ]; then
    echo "============================================="
    echo "⚠️  部署完成但验证失败！请检查上述错误！"
    echo "============================================="
    echo ""
    echo "🔧 建议执行以下命令重新部署："
    echo ""
    echo "ssh -i $SSH_KEY_PATH $SERVER_USER@$SERVER_IP << 'EOF'"
    echo "cd $SERVER_PROJECT_PATH"
    echo "docker-compose down"
    echo "docker rmi $BTC_ETH_IMAGE_NAME $NEW_COIN_IMAGE_NAME $GRID_IMAGE_NAME --force"
    echo "docker-compose build --no-cache"
    echo "docker-compose up -d"
    echo "EOF"
    echo ""
    exit 1
fi

# 步骤 5：清理临时文件
echo ""
echo "📤 步骤 5/5: 清理临时文件..."
rm -f /tmp/verify_deployment.sh 2>/dev/null || true
ssh -i "$SSH_KEY_PATH" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "rm -f /root/$DEPLOY_PACKAGE_NAME"
echo "✅ 临时文件已清理"

echo ""
echo "============================================="
echo "🎉 部署全部完成！"
echo "============================================="
echo ""
echo "📊 部署摘要："
echo "  - PostgreSQL: $POSTGRES_CONTAINER_NAME"
echo "  - BTC/ETH 策略: $BTC_ETH_CONTAINER_NAME"
echo "  - 新币做空策略: $NEW_COIN_CONTAINER_NAME"
echo "  - 网格交易策略: $GRID_CONTAINER_NAME"
echo "  - HRS 混合反转策略: $HRS_CONTAINER_NAME"
echo "  - StratTuneAI: $AI_TUNER_CONTAINER_NAME"
echo "  - K 线数据服务: $KLINE_CONTAINER_NAME"
echo ""
echo "🔍 查看容器状态："
echo "  ssh -i $SSH_KEY_PATH $SERVER_USER@$SERVER_IP 'docker ps -f name=trading_system'"
echo ""
echo "📋 查看容器日志："
echo "  ssh -i $SSH_KEY_PATH $SERVER_USER@$SERVER_IP 'docker logs -f --tail 100 $BTC_ETH_CONTAINER_NAME'"
echo ""
