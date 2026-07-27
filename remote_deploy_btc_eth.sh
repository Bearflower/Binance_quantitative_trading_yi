#!/bin/bash
set -e

# 加载部署配置
source .deploy_config

echo "============================================="
echo "远程部署开始 - BTC/ETH 策略 (限价单版本)"
echo "============================================="

# 1. 创建项目目录
echo "创建项目目录..."
mkdir -p $SERVER_PROJECT_PATH
mkdir -p $SERVER_PROJECT_PATH/logs
mkdir -p $SERVER_PROJECT_PATH/data

# 2. 停止并删除旧容器
echo "停止旧容器..."
for container in $BTC_ETH_CONTAINER_NAME; do
    if docker ps -q -f name=$container | grep -q .; then
        docker stop $container
        echo "容器 $container 已停止"
    fi
    if docker ps -aq -f name=$container | grep -q .; then
        docker rm $container
        echo "容器 $container 已删除"
    fi
done

# 3. 删除旧镜像
echo "删除旧镜像..."
if docker images -q $BTC_ETH_IMAGE_NAME | grep -q .; then
    docker rmi $BTC_ETH_IMAGE_NAME --force 2>/dev/null || true
    echo "旧镜像已删除"
fi

# 4. 解压新包
echo "解压新代码包..."
cd /root
tar -xzf $DEPLOY_PACKAGE_NAME -C $SERVER_PROJECT_PATH
echo "代码包已解压"

# 5. 设置权限
cd $SERVER_PROJECT_PATH
chmod 600 .env 2>/dev/null || true
echo "权限已设置"

# 6. 创建 Docker 网络（如果不存在）
echo "创建 Docker 网络..."
if docker network ls | grep -q trading-network; then
    echo "Docker 网络已存在"
else
    docker network create trading-network
    echo "Docker 网络已创建"
fi

# 7. 构建并启动（不使用缓存）
echo "构建 BTC/ETH 策略镜像（--no-cache）..."
cd $SERVER_PROJECT_PATH
docker-compose build --no-cache btc-eth-strategy
echo "镜像构建成功"

echo "启动 BTC/ETH 策略容器..."
docker-compose up -d btc-eth-strategy
echo "容器启动成功"

# 8. 等待并检查状态
echo "等待容器启动..."
sleep 10

echo "============================================="
echo "容器状态:"
docker ps -f name=$BTC_ETH_CONTAINER_NAME
echo "============================================="

echo "检查镜像创建时间:"
docker inspect -f '{{.Created}}' $BTC_ETH_CONTAINER_NAME 2>/dev/null
echo "============================================="

echo "验证策略代码版本（检查是否有市价单残留）:"
docker exec $BTC_ETH_CONTAINER_NAME grep -c 'STOP_MARKET\|TAKE_PROFIT_MARKET' /app/strategies/btc_eth/strategy.py || echo "0"
echo "============================================="

echo "查看最近日志:"
docker logs --tail 30 $BTC_ETH_CONTAINER_NAME 2>&1 || true
echo "============================================="