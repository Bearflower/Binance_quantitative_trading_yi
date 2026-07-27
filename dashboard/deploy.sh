#!/bin/bash

# ============================================
# Dashboard 一键部署脚本
# ============================================

set -e

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 加载配置
source "$SCRIPT_DIR/.deploy_config"

echo "============================================="
echo "Dashboard 一键部署"
echo "目标服务器：$SERVER_IP"
echo "============================================="

# 步骤 1：打包
echo "📦 步骤 1/5: 打包Dashboard..."

# 创建临时目录
TEMP_DIR="/tmp/dashboard_deploy_$$"
mkdir -p "$TEMP_DIR"

# 复制整个项目（包括strategies、shared模块）
echo "  复制项目文件..."
rsync -av --delete \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='*.pyo' \
    --exclude='*.tar.gz' \
    --exclude='.DS_Store' \
    --exclude='tests/*' \
    --exclude='.git/*' \
    --exclude='node_modules/*' \
    --exclude='backtest/*' \
    --exclude='data/*' \
    --exclude='logs/*' \
    "$PROJECT_ROOT/" "$TEMP_DIR/"

# 复制Dashboard专用文件
echo "  复制Dashboard专用文件..."
cp "$SCRIPT_DIR/backend/main_docker.py" "$TEMP_DIR/dashboard/backend/main.py"
cp "$SCRIPT_DIR/backend/api/routes_docker.py" "$TEMP_DIR/dashboard/backend/api/routes.py"
cp "$SCRIPT_DIR/backend/services/data_service_docker.py" "$TEMP_DIR/dashboard/backend/services/data_service.py"

# 复制前端文件
echo "  复制前端文件..."
rsync -av --delete \
    --exclude='.DS_Store' \
    "$SCRIPT_DIR/frontend/" "$TEMP_DIR/dashboard/frontend/"

# 复制Nginx配置
echo "  复制Nginx配置..."
cp -r "$SCRIPT_DIR/nginx" "$TEMP_DIR/dashboard/"

# 复制配置文件
echo "  复制配置文件..."
cp "$SCRIPT_DIR/.deploy_config" "$TEMP_DIR/dashboard/"
if [ -f "$PROJECT_ROOT/.env" ]; then
    cp "$PROJECT_ROOT/.env" "$TEMP_DIR/.env"
else
    echo "  警告：.env文件不存在，请手动创建"
fi

# 创建压缩包
echo "  创建压缩包..."
cd "$TEMP_DIR"
tar -czf "$SCRIPT_DIR/$DEPLOY_PACKAGE_NAME" .

# 清理
cd "$SCRIPT_DIR"
rm -rf "$TEMP_DIR"

PACKAGE_SIZE=$(ls -lh "$DEPLOY_PACKAGE_NAME" | awk '{print $5}')
echo "✅ 打包完成！大小：$PACKAGE_SIZE"

# 步骤 2：上传
echo ""
echo "📤 步骤 2/5: 上传到服务器..."

# 测试SSH密钥
if ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no -o BatchMode=yes "$SERVER_USER@$SERVER_IP" "echo 密钥可用" 2>/dev/null; then
    echo "  使用SSH密钥认证..."
    scp -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no "$DEPLOY_PACKAGE_NAME" "$SERVER_USER@$SERVER_IP:/root/"
    echo "✅ 上传成功"
else
    echo "❌ SSH密钥不可用，请检查密钥路径：$SSH_KEY_PATH"
    exit 1
fi

# 步骤 3：远程部署
echo ""
echo "🚀 步骤 3/5: 远程部署..."

ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" << ENDSSH

set -e

echo "============================================="
echo "远程部署开始"
echo "============================================="

# 创建目录
echo "1. 创建目录..."
mkdir -p /root/dashboard

# 解压文件
echo "2. 解压文件..."
cd /root
rm -rf dashboard
mkdir -p dashboard
tar -xzf $DEPLOY_PACKAGE_NAME -C dashboard

# 设置权限
echo "3. 设置权限..."
cd /root/dashboard
chmod 600 .env 2>/dev/null || true
chmod +x dashboard/backend/*.py 2>/dev/null || true

# 停止旧容器
echo "4. 停止旧容器..."
if docker ps -q -f name=$DASHBOARD_CONTAINER_NAME | grep -q .; then
    docker stop $DASHBOARD_CONTAINER_NAME
    docker rm $DASHBOARD_CONTAINER_NAME
    echo "  旧容器已停止"
else
    echo "  无旧容器运行"
fi

# 删除旧镜像
echo "5. 删除旧镜像..."
if docker images -q $DASHBOARD_IMAGE_NAME | grep -q .; then
    docker rmi $DASHBOARD_IMAGE_NAME --force 2>/dev/null || true
    echo "  旧镜像已删除"
fi

# 创建Docker网络（如果不存在）
echo "6. 创建Docker网络..."
docker network create trading-network-v2 2>/dev/null || echo "  网络已存在"

# 构建镜像（从项目根目录构建，确保shared模块被包含）
echo "7. 构建Docker镜像..."
cd /root/dashboard
docker build -t $DASHBOARD_IMAGE_NAME -f dashboard/backend/Dockerfile .
if [ \$? -ne 0 ]; then
    echo "❌ Docker构建失败！"
    exit 1
fi
echo "  镜像构建成功"

# 启动容器
echo "8. 启动容器..."
docker run -d \
    --name $DASHBOARD_CONTAINER_NAME \
    --network trading-network-v2 \
    -p $DASHBOARD_PORT:8767 \
    --env-file /root/dashboard/.env \
    -e DATABASE_HOST=trading_system-postgres \
    -e DB_HOST=trading_system-postgres \
    --restart unless-stopped \
    $DASHBOARD_IMAGE_NAME
if [ \$? -ne 0 ]; then
    echo "❌ 容器启动失败！"
    exit 1
fi
echo "  容器启动成功"

# 等待容器启动
echo "9. 等待容器启动..."
sleep 5

# 显示状态
echo "============================================="
echo "容器状态:"
docker ps -f name=$DASHBOARD_CONTAINER_NAME
echo "============================================="

ENDSSH

if [ $? -ne 0 ]; then
    echo "❌ 远程部署失败！"
    exit 1
fi

# 步骤 4：配置Nginx
echo ""
echo "🔧 步骤 4/5: 配置Nginx..."

ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" << 'ENDSSH'

echo "============================================="
echo "配置Nginx"
echo "============================================="

# 备份现有Nginx配置
if [ -f /etc/nginx/conf.d/default.conf ]; then
    cp /etc/nginx/conf.d/default.conf /etc/nginx/conf.d/default.conf.backup.$(date +%Y%m%d%H%M%S)
    echo "  已备份现有配置"
fi

# 复制Dashboard Nginx配置到conf.d目录
cp /root/dashboard/dashboard/nginx/dashboard.conf /etc/nginx/conf.d/dashboard.conf
echo "  已复制Nginx配置"

# 删除默认配置（确保只访问Dashboard）
rm -f /etc/nginx/conf.d/default.conf
echo "  已删除默认配置"

# 测试Nginx配置
echo "  测试Nginx配置..."
nginx -t
if [ $? -ne 0 ]; then
    echo "❌ Nginx配置测试失败！"
    exit 1
fi

# 重启Nginx
echo "  重启Nginx..."
systemctl restart nginx
if [ $? -ne 0 ]; then
    echo "❌ Nginx重启失败！"
    exit 1
fi

echo "✅ Nginx配置完成"

# 复制前端文件到Nginx目录
echo "  复制前端文件到Nginx目录..."
cp -r /root/dashboard/dashboard/frontend/* /var/www/dashboard/ 2>/dev/null || echo "  前端目录不存在，创建中..."
mkdir -p /var/www/dashboard
cp -r /root/dashboard/dashboard/frontend/* /var/www/dashboard/ 2>/dev/null || true
echo "  前端文件已同步"

ENDSSH

if [ $? -ne 0 ]; then
    echo "❌ Nginx配置失败！"
    exit 1
fi

# 步骤 5：验证部署
echo ""
echo "✅ 步骤 5/5: 验证部署..."

ssh -i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no "$SERVER_USER@$SERVER_IP" << ENDSSH

echo "============================================="
echo "验证部署"
echo "============================================="

# 检查容器状态
echo "1. 容器状态:"
docker ps -f name=$DASHBOARD_CONTAINER_NAME

# 检查容器健康
echo ""
echo "2. 容器健康状态:"
docker inspect -f '{{.State.Health.Status}}' $DASHBOARD_CONTAINER_NAME 2>/dev/null || echo "  无健康检查"

# 检查端口
echo ""
echo "3. 端口监听:"
netstat -tlnp | grep 8767 || ss -tlnp | grep 8767

# 检查Nginx
echo ""
echo "4. Nginx状态:"
systemctl status nginx --no-pager | grep Active

# 测试API
echo ""
echo "5. 测试API:"
curl -s http://localhost:8767/api/health | head -20

# 测试前端
echo ""
echo "6. 测试前端:"
curl -s -I http://localhost/ | head -5

ENDSSH

# 清理本地临时文件
rm -f "$SCRIPT_DIR/$DEPLOY_PACKAGE_NAME"

echo ""
echo "============================================="
echo "🎉 Dashboard部署完成！"
echo "============================================="
echo ""
echo "访问地址："
echo "  http://$SERVER_IP"
echo ""
echo "API文档："
echo "  http://$SERVER_IP:8767/api/docs"
echo ""
echo "注意事项："
echo "  1. Nginx已配置为只允许访问Dashboard页面"
echo "  2. 其他路径已被禁止访问"
echo "  3. API限流已启用（10r/s）"
echo ""
