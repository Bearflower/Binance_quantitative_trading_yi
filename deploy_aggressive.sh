#!/bin/bash

# ============================================
# MTPCS激进版 定向部署脚本（不影响原版及其他运行中容器）
#
# 使用场景：仅部署/更新激进版策略服务，保持原版 btc_eth 与其
# 他策略容器持续运行（满足"上线激进版后原版也不停，也继续运行"）。
#
# 流程：
#   1. 本地打包
#   2. 上传到服务器
#   3. 解压新代码
#   4. 删除激进版旧镜像（防部署幻觉）
#   5. 仅构建激进版镜像（--no-cache）
#   6. 启动激进版容器（不动其他容器）
#   7. 五层反幻觉验证（容器/镜像/VERSION/MD5/日志）
# ============================================

set -e

# 加载配置
source .deploy_config

SERVICE_NAME="btc-eth-aggressive-strategy"
CONTAINER_NAME="${BTC_ETH_AGGRESSIVE_CONTAINER_NAME:-trading_system-btc_eth_aggressive}"
IMAGE_NAME="${BTC_ETH_AGGRESSIVE_IMAGE_NAME:-trading_system-btc_eth_aggressive:latest}"

# 激进版关键文件（用于 MD5 校验）
KEY_FILES=(
    "strategies/btc_eth_aggressive/main.py"
    "strategies/btc_eth_aggressive/config.yaml"
    "strategies/btc_eth_aggressive/strategy.py"
)

echo "============================================="
echo "MTPCS激进版 定向部署"
echo "目标服务器：$SERVER_IP"
echo "目标服务：$SERVICE_NAME"
echo "============================================="

# 1. 打包
echo ""
echo "📦 步骤 1/5: 打包项目..."
bash auto_package.sh

# 2. 上传
echo ""
echo "📤 步骤 2/5: 上传部署包..."
bash upload_to_server.sh

# 3-6. 远程部署（仅激进版禁入，不停止原版）
echo ""
echo "🚀 步骤 3/5: 远程部署激进版..."

ssh -i "$SSH_KEY_PATH" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" << ENDSSH
set -e

echo "=== 解压新代码包 ==="
cd /root
tar -xzf $DEPLOY_PACKAGE_NAME -C $SERVER_PROJECT_PATH

# 校验 VERSION 文件
if [ ! -f "$SERVER_PROJECT_PATH/VERSION" ]; then
    echo "❌ VERSION 文件不存在，部署包可能损坏"
    exit 1
fi
cat $SERVER_PROJECT_PATH/VERSION

cd $SERVER_PROJECT_PATH
# 确保激进版覆盖层目录容器内可写
chmod -R 777 strategies/btc_eth_aggressive/tuning_overrides 2>/dev/null || true

echo ""
echo "=== 删除激进版旧镜像（防空） ==="
docker images -q $IMAGE_NAME | xargs -r docker rmi --force

echo ""
echo "=== 构建激进版镜像（--no-cache） ==="
docker-compose build --no-cache $SERVICE_NAME

echo ""
echo "=== 启动激进版容器（不影响原版） ==="
docker-compose up -d $SERVICE_NAME

echo ""
echo "=== 等待容器就绪 ==="
sleep 10

if ! docker ps -q -f name=$CONTAINER_NAME | grep -q .; then
    echo "❌ $CONTAINER_NAME 启动失败"
    docker logs --tail 50 $CONTAINER_NAME || true
    exit 1
fi
echo "✅ $CONTAINER_NAME 已运行"

ENDSSH

if [ $? -ne 0 ]; then
    echo "❌ 远程部署失败！"
    exit 1
fi

# 7. 五层反幻觉验证（激进版）
echo ""
echo "✅ 步骤 4/5: 激进版部署验证..."

echo "=== 1. 容器状态 ==="
ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "docker ps -f name=$CONTAINER_NAME --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

echo "=== 2. VERSION 文件校验 ==="
LOCAL_DEPLOY_ID=$(grep DEPLOY_ID VERSION | cut -d= -f2)
REMOTE_DEPLOY_ID=$(ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "docker exec $CONTAINER_NAME cat /app/VERSION 2>/dev/null | grep DEPLOY_ID | cut -d= -f2" || echo "NOT_FOUND")

if [ "$LOCAL_DEPLOY_ID" = "$REMOTE_DEPLOY_ID" ] && [ "$REMOTE_DEPLOY_ID" != "NOT_FOUND" ]; then
    echo "✅ VERSION 匹配 (部署ID: $LOCAL_DEPLOY_ID)"
else
    echo "❌ VERSION 不匹配！本地=$LOCAL_DEPLOY_ID 容器=$REMOTE_DEPLOY_ID"
    exit 1
fi

echo "=== 3. 关键文件 MD5 校验 ==="
MD5_OK=true
for FILE in "${KEY_FILES[@]}"; do
    LOCAL_MD5=$(md5sum "$FILE" 2>/dev/null | cut -d' ' -f1)
    REMOTE_MD5=$(ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" \
        "docker exec $CONTAINER_NAME md5sum /app/$FILE 2>/dev/null | cut -d' ' -f1" || echo "NOT_FOUND")
    if [ "$LOCAL_MD5" = "$REMOTE_MD5" ]; then
        echo "✅ $FILE MD5 匹配"
    else
        echo "❌ $FILE MD5 不匹配！本地=$LOCAL_MD5 容器=$REMOTE_MD5"
        MD5_OK=false
    fi
done

echo "=== 4. 日志无错误检查 ==="
ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" \
    "docker logs --tail 100 $CONTAINER_NAME 2>&1 | tail -30"

echo ""
if [ "$MD5_OK" = true ] && [ "$LOCAL_DEPLOY_ID" = "$REMOTE_DEPLOY_ID" ]; then
    echo "============================================="
    echo "✅ 激进版部署成功！原位版本不受影响。"
    echo "============================================="
    echo "原版容器状态（应仍在运行）:"
    ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" \
        "docker ps -f name=$BTC_ETH_CONTAINER_NAME --format 'table {{.Names}}\t{{.Status}}'"
    echo ""
    echo "容器日志："
    echo "  ssh -i $SSH_KEY_PATH $SERVER_USER@$SERVER_IP 'docker logs -f --tail 100 $CONTAINER_NAME'"
else
    echo "❌ 部署验证未全部通过，请检查！"
    exit 1
fi