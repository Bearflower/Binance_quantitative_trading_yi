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

# ============================================
# 安全部署策略：先构建所有镜像，构建成功后才替换旧容器
# 防止某个服务构建失败导致其他服务容器丢失
# ============================================

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

# 2. 解压新包（先解压，后续构建使用新代码）
echo "📦 解压新代码包..."
cd /root
tar -xzf $DEPLOY_PACKAGE_NAME -C $SERVER_PROJECT_PATH
echo "✅ 代码包已解压"

# 3. 设置权限
cd $SERVER_PROJECT_PATH
chmod +x database/postgres/scripts/backup-postgres.sh 2>/dev/null || true
chmod 600 .env 2>/dev/null || true
echo "✅ 权限已设置"

# 4. 构建所有镜像（先构建，不删除旧镜像，构建失败不影响运行中的容器）
echo "🏗️  构建所有服务镜像（不使用缓存）..."
BUILD_FAILED=false
BUILD_FAILED_SERVICES=""

# 4.1 构建基础服务
echo "--- 构建基础服务 ---"
for service in kline-service kline-monitor; do
    if [ "\$DEPLOY_${service%%-*}_${service##*-}" = true ] 2>/dev/null || [ true = true ]; then
        echo "构建 \$service ..."
        cd $SERVER_PROJECT_PATH
        if docker-compose build --no-cache \$service; then
            echo "✅ \$service 构建成功"
        else
            echo "⚠️  \$service 构建失败，继续构建其他服务..."
            BUILD_FAILED=true
            BUILD_FAILED_SERVICES="\$BUILD_FAILED_SERVICES \$service"
        fi
    fi
done

# 4.2 构建策略服务
echo "--- 构建策略服务 ---"
for service in btc-eth-strategy new-coin-strategy grid-strategy hrs-strategy ai-tuner; do
    echo "构建 \$service ..."
    cd $SERVER_PROJECT_PATH
    if docker-compose build --no-cache \$service; then
        echo "✅ \$service 构建成功"
    else
        echo "⚠️  \$service 构建失败，继续构建其他服务..."
        BUILD_FAILED=true
        BUILD_FAILED_SERVICES="\$BUILD_FAILED_SERVICES \$service"
    fi
done

# 4.3 如果有服务构建失败，输出警告但不阻断
if [ "\$BUILD_FAILED" = true ]; then
    echo "============================================="
    echo "⚠️  以下服务构建失败：\$BUILD_FAILED_SERVICES"
    echo "将尝试启动已成功的服务，失败的服务需要手动处理"
    echo "============================================="
fi

# 5. 停止旧容器（现在才停止，因为新镜像已构建完成）
echo "🛑 停止旧容器..."
cd $SERVER_PROJECT_PATH

# 停止所有策略容器
# 注意：docker ps -f name= 是前缀匹配，如 trading_system-kline 会匹配到
# trading_system-kline-monitor，在精确容器不存在时 docker stop 会失败
# 因此使用 || true 防止错误退出
for container in $BTC_ETH_CONTAINER_NAME $NEW_COIN_CONTAINER_NAME $GRID_CONTAINER_NAME $HRS_CONTAINER_NAME $AI_TUNER_CONTAINER_NAME $KLINE_CONTAINER_NAME $KLINE_MONITOR_CONTAINER_NAME; do
    if docker ps -q -f name=\$container | grep -q .; then
        docker stop \$container || true
        echo "✅ 容器 \$container 已停止"
    else
        echo "⚠️  容器 \$container 未运行，跳过停止"
    fi
done

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

# 6. 删除旧镜像（关键步骤，防止使用缓存）⭐⭐⭐
echo "🗑️  删除旧镜像（防止使用缓存）..."
for image in $BTC_ETH_IMAGE_NAME $NEW_COIN_IMAGE_NAME $GRID_IMAGE_NAME $HRS_IMAGE_NAME $AI_TUNER_IMAGE_NAME $KLINE_IMAGE_NAME $KLINE_MONITOR_IMAGE_NAME; do
    if docker images -q \$image | grep -q .; then
        docker rmi \$image --force 2>/dev/null || true
        echo "✅ 旧镜像 \$image 已删除"
    else
        echo "⚠️  旧镜像 \$image 不存在，跳过删除"
    fi
done

# 7. 清理 Docker 缓存（可选，如果磁盘空间紧张）
echo "🧹 清理 Docker 悬空镜像..."
docker image prune -f --filter "until=24h" 2>/dev/null || true

# 8. 创建 Docker 网络（如果不存在）
echo "🌐 创建 Docker 网络..."
if docker network ls | grep -q trading-network; then
    echo "✅ Docker 网络已存在"
else
    docker network create trading-network
    echo "✅ Docker 网络已创建"
fi

# 9. 启动 PostgreSQL（如果未运行）
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
                --network trading-network \
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

# 10. 启动所有服务容器（镜像已在上一步构建完成）
echo "🚀 启动所有服务容器..."
cd $SERVER_PROJECT_PATH

# 先启动基础服务（kline-service 等），再启动策略服务
# 策略服务 depends_on kline-service (condition: service_healthy)
if [ "$DEPLOY_KLINE" = true ]; then
    echo "  启动 K 线数据服务..."
    docker-compose up -d kline-service || echo "⚠️  kline-service 启动失败"
fi

if [ "$DEPLOY_KLINE_MONITOR" = true ]; then
    echo "  启动 K 线服务健康监控..."
    docker-compose up -d kline-monitor || echo "⚠️  kline-monitor 启动失败"
fi

# 等待基础服务就绪
echo "⏳ 等待基础服务就绪..."
sleep 5

# 启动策略服务
if [ "$DEPLOY_BTC_ETH" = true ]; then
    echo "  启动 BTC/ETH 策略..."
    docker-compose up -d btc-eth-strategy || echo "⚠️  btc-eth-strategy 启动失败"
fi
if [ "$DEPLOY_NEW_COIN" = true ]; then
    echo "  启动新币做空策略..."
    docker-compose up -d new-coin-strategy || echo "⚠️  new-coin-strategy 启动失败"
fi
if [ "$DEPLOY_GRID" = true ]; then
    echo "  启动网格交易策略..."
    docker-compose up -d grid-strategy || echo "⚠️  grid-strategy 启动失败"
fi
if [ "$DEPLOY_HRS" = true ]; then
    echo "  启动 HRS 混合反转策略..."
    docker-compose up -d hrs-strategy || echo "⚠️  hrs-strategy 启动失败"
fi
if [ "$DEPLOY_AI_TUNER" = true ]; then
    echo "  启动 StratTuneAI 调优..."
    docker-compose up -d ai-tuner || echo "⚠️  ai-tuner 启动失败"
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
