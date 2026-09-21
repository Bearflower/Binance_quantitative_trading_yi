#!/bin/bash

# ============================================
# 数据后台容器一键部署脚本
# 承载数据维护定时任务，与 dashboard 解耦独立部署
# ============================================

set -e

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# 加载配置
source "$SCRIPT_DIR/.deploy_config"

echo "============================================="
echo "数据后台一键部署"
echo "目标服务器：$SERVER_IP"
echo "============================================="

# 步骤 1：打包
echo "📦 步骤 1/4: 打包数据后台..."

# 生成版本标记（部署防幻觉验证用）
VERSION_FILE="$SCRIPT_DIR/VERSION"
cat > "$VERSION_FILE" << EOF
DEPLOY_TIME=$(date '+%Y-%m-%d %H:%M:%S')
GIT_COMMIT=$(git -C "$PROJECT_ROOT" log --oneline -1 2>/dev/null || echo "no-git")
DEPLOY_ID=$(uuidgen | cut -d- -f1)
FILE_MD5=$(md5 -q "$PROJECT_ROOT/dashboard/backend/services/data_service_docker.py")
EOF

# 创建临时目录
TEMP_DIR="/tmp/data_backend_deploy_$$"
mkdir -p "$TEMP_DIR"

echo "  复制共享模块..."
rsync -aq --exclude='*.pyc' --exclude='__pycache__' --exclude='*.pyo' \
    "$PROJECT_ROOT/shared/" "$TEMP_DIR/shared/"

echo "  复制 dashboard 数据服务层（services/core/config）..."
for sub in services core config; do
    mkdir -p "$TEMP_DIR/dashboard/backend/$sub"
    rsync -aq --exclude='*.pyc' --exclude='__pycache__' --exclude='*.pyo' \
        "$PROJECT_ROOT/dashboard/backend/$sub/" "$TEMP_DIR/dashboard/backend/$sub/"
done

echo "  复制数据后台容器代码..."
mkdir -p "$TEMP_DIR/services/data_backend"
rsync -aq --exclude='*.pyc' --exclude='__pycache__' --exclude='*.pyo' \
    "$SCRIPT_DIR/" "$TEMP_DIR/services/data_backend/"
rm -f "$TEMP_DIR/services/data_backend/VERSION" \
      "$TEMP_DIR/services/data_backend/$DEPLOY_PACKAGE_NAME"

echo "  复制依赖与环境配置..."
cp "$PROJECT_ROOT/requirements.txt" "$TEMP_DIR/requirements.txt"
if [ -f "$PROJECT_ROOT/.env" ]; then
    cp "$PROJECT_ROOT/.env" "$TEMP_DIR/.env"
else
    echo "  ⚠️  .env 文件不存在，请手动创建"
fi
cp "$VERSION_FILE" "$TEMP_DIR/VERSION"

# 创建压缩包
echo "  创建压缩包..."
cd "$TEMP_DIR"
tar -czf "$SCRIPT_DIR/$DEPLOY_PACKAGE_NAME" .
cd "$SCRIPT_DIR"
rm -rf "$TEMP_DIR" "$VERSION_FILE"

PACKAGE_SIZE=$(ls -lh "$DEPLOY_PACKAGE_NAME" | awk '{print $5}')
echo "✅ 打包完成！大小：$PACKAGE_SIZE"

# 步骤 2：上传
echo ""
echo "📤 步骤 2/4: 上传到服务器..."

LOCAL_MD5=$(md5 -q "$SCRIPT_DIR/$DEPLOY_PACKAGE_NAME")

if ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no \
    -o BatchMode=yes "$SERVER_USER@$SERVER_IP" "echo 密钥可用" 2>/dev/null; then
    scp -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no \
        "$DEPLOY_PACKAGE_NAME" "$SERVER_USER@$SERVER_IP:/root/"
    echo "✅ 上传成功"
else
    echo "❌ SSH 密钥不可用，请检查密钥路径：$SSH_KEY_PATH"
    exit 1
fi

# 步骤 3：远程部署
echo ""
echo "🚀 步骤 3/4: 远程部署..."

ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" << ENDSSH

set -e

echo "============================================="
echo "远程部署开始"
echo "============================================="

# 上传完整性验证
echo "1. 上传完整性验证..."
SERVER_MD5=\$(md5sum /root/$DEPLOY_PACKAGE_NAME | cut -d' ' -f1)
if [ "$LOCAL_MD5" != "\$SERVER_MD5" ]; then
    echo "❌ 文件上传不完整！本地 MD5=$LOCAL_MD5 服务器 MD5=\$SERVER_MD5"
    exit 1
fi
echo "✅ 上传完整性验证通过"

# 解压文件
echo "2. 解压文件..."
cd /root
rm -rf data_backend
mkdir -p data_backend
tar -xzf $DEPLOY_PACKAGE_NAME -C data_backend

# 验证 VERSION 文件
echo "3. 验证 VERSION 文件..."
if [ ! -f /root/data_backend/VERSION ]; then
    echo "❌ VERSION 文件不存在，部署包可能损坏"
    exit 1
fi
cat /root/data_backend/VERSION

# 停止旧容器
echo "4. 停止旧容器..."
if docker ps -q -f name=$DATA_BACKEND_CONTAINER_NAME | grep -q .; then
    docker stop $DATA_BACKEND_CONTAINER_NAME
    docker rm $DATA_BACKEND_CONTAINER_NAME
    echo "  旧容器已停止"
else
    echo "  无旧容器运行"
fi

# 删除旧镜像
echo "5. 删除旧镜像..."
if docker images -q $DATA_BACKEND_IMAGE_NAME | grep -q .; then
    docker rmi $DATA_BACKEND_IMAGE_NAME --force 2>/dev/null || true
    echo "  旧镜像已删除"
fi

# 创建 Docker 网络（如果不存在）
echo "6. 创建 Docker 网络..."
docker network create trading-network-v2 2>/dev/null || echo "  网络已存在"

# 构建镜像（从项目根构建，确保 shared/ 与 dashboard 数据服务层被包含）
echo "7. 构建 Docker 镜像..."
cd /root/data_backend
docker build -t $DATA_BACKEND_IMAGE_NAME -f services/data_backend/Dockerfile .
if [ \$? -ne 0 ]; then
    echo "❌ Docker 构建失败！"
    exit 1
fi
echo "  镜像构建成功"

# 启动容器（无端口映射，纯后台调度）
echo "8. 启动容器..."
docker run -d \
    --name $DATA_BACKEND_CONTAINER_NAME \
    --network trading-network-v2 \
    --env-file /root/data_backend/.env \
    -e DATABASE_HOST=trading_system-postgres \
    -e DB_HOST=trading_system-postgres \
    --restart unless-stopped \
    $DATA_BACKEND_IMAGE_NAME
if [ \$? -ne 0 ]; then
    echo "❌ 容器启动失败！"
    exit 1
fi
echo "  容器启动成功"

# 等待容器启动
echo "9. 等待容器启动..."
sleep 8

# 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=$DATA_BACKEND_CONTAINER_NAME
echo "============================================="

ENDSSH

if [ $? -ne 0 ]; then
    echo "❌ 远程部署失败！"
    exit 1
fi

# 步骤 4：验证部署
echo ""
echo "✅ 步骤 4/4: 验证部署..."

ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" << ENDSSH

echo "============================================="
echo "验证部署"
echo "============================================="

echo "1. 容器状态:"
docker ps -f name=$DATA_BACKEND_CONTAINER_NAME

echo ""
echo "2. VERSION 文件验证:"
CONTAINER_DEPLOY_ID=\$(docker exec $DATA_BACKEND_CONTAINER_NAME \
    cat /app/VERSION 2>/dev/null | grep DEPLOY_ID | cut -d= -f2 || echo "NOT_FOUND")
echo "  容器内 DEPLOY_ID: \$CONTAINER_DEPLOY_ID"

echo ""
echo "3. 关键文件 MD5（本地 vs 容器内）:"
echo "  本地 data_service_docker.py: $LOCAL_MD5"
docker exec $DATA_BACKEND_CONTAINER_NAME md5sum /app/dashboard/backend/services/data_service_docker.py
docker exec $DATA_BACKEND_CONTAINER_NAME md5sum /app/shared/trade_logger.py

echo ""
echo "4. 容器日志（尾部 40 行）:"
docker logs --tail 40 $DATA_BACKEND_CONTAINER_NAME

echo ""
echo "5. 错误日志检查（计数）:"
docker logs --tail 100 $DATA_BACKEND_CONTAINER_NAME 2>&1 \
    | grep -cE "(ERROR|Exception|FATAL|CRITICAL|Traceback)" || echo "0"

ENDSSH

# 清理本地临时文件
rm -f "$SCRIPT_DIR/$DEPLOY_PACKAGE_NAME"

echo ""
echo "============================================="
echo "🎉 数据后台部署完成！"
echo "============================================="
