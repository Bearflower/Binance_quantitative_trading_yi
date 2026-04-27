#!/bin/bash

# 网格交易信号灯系统 V2.0 部署脚本

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 配置
SERVER_IP="43.156.242.184"
SERVER_USER="root"
SERVER_PROJECT_PATH="/root/grid_signal_bot_v2"
PROJECT_NAME="grid_signal_bot_v2"

echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}🚀 网格交易信号灯系统 V2.0 部署${NC}"
echo -e "${GREEN}============================================================${NC}"

# 步骤 1: 打包项目
echo -e "\n${YELLOW}📦 步骤 1/5: 打包项目文件...${NC}"
tar -czf deployment_package.tar.gz \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='*.log' \
    --exclude='logs/*' \
    --exclude='venv' \
    --exclude='.env' \
    src/ config/ migrations/ requirements.txt Dockerfile README.md scripts/

echo -e "${GREEN}✅ 项目打包完成${NC}"

# 步骤 2: 上传到服务器
echo -e "\n${YELLOW}📤 步骤 2/5: 上传到服务器...${NC}"
scp deployment_package.tar.gz ${SERVER_USER}@${SERVER_IP}:/tmp/

echo -e "${GREEN}✅ 文件上传完成${NC}"

# 步骤 3: 在服务器上解压并配置
echo -e "\n${YELLOW}📂 步骤 3/5: 解压并配置...${NC}"
ssh ${SERVER_USER}@${SERVER_IP} << 'ENDSSH'
# 创建项目目录
mkdir -p /root/grid_signal_bot_v2
cd /root/grid_signal_bot_v2

# 解压文件
tar -xzf /tmp/deployment_package.tar.gz

# 创建日志目录
mkdir -p logs

# 创建 .env 文件（如果不存在）
if [ ! -f config/.env ]; then
    cat > config/.env << 'EOF'
# 数据库配置
DATABASE_URL=postgresql://binance:Bianace%402024@common_service_postgres:5432/binance_data

# 服务配置
KLINE_SERVICE_URL=http://common_service_kline:8000
NOTIFICATION_SERVICE_URL=http://common_service_notification:8000

# 日志配置
LOG_LEVEL=INFO

# 巡检间隔
INSPECTION_INTERVAL=60
EOF
    echo "✅ .env 文件已创建"
fi

echo "✅ 项目解压完成"
ENDSSH

echo -e "${GREEN}✅ 解压并配置完成${NC}"

# 步骤 4: 初始化数据库
echo -e "\n${YELLOW}🗄️  步骤 4/5: 初始化数据库...${NC}"
ssh ${SERVER_USER}@${SERVER_IP} << 'ENDSSH'
# 进入项目目录
cd /root/grid_signal_bot_v2

# 检查是否在 common_service 网络中
if docker network inspect common_common_network &> /dev/null; then
    echo "✅ 找到 common_service 网络"
    
    # 使用 Docker 容器执行数据库初始化
    docker run --rm \
        --network common_common_network \
        -v /root/grid_signal_bot_v2:/app \
        -w /app \
        python:3.10-slim \
        bash -c "
            pip install -q psycopg2-binary python-dotenv &&
            python scripts/init_database.py
        "
else
    echo "⚠️  未找到 common_service 网络，跳过数据库初始化"
    echo "请手动在 common_service 容器中执行数据库初始化"
fi
ENDSSH

echo -e "${GREEN}✅ 数据库初始化完成${NC}"

# 步骤 5: 构建并启动容器
echo -e "\n${YELLOW}🐳 步骤 5/5: 构建并启动容器...${NC}"
ssh ${SERVER_USER}@${SERVER_IP} << 'ENDSSH'
# 进入项目目录
cd /root/grid_signal_bot_v2

# 构建镜像
docker build -t grid-signal-bot:v2.0 .

# 停止旧容器（如果存在）
docker stop grid-signal-bot 2>/dev/null || true
docker rm grid-signal-bot 2>/dev/null || true

# 启动新容器
docker run -d \
    --name grid-signal-bot \
    --network common_common_network \
    --restart always \
    --env-file config/.env \
    grid-signal-bot:v2.0

echo "✅ 容器启动成功"

# 显示容器状态
docker ps -f name=grid-signal-bot
ENDSSH

echo -e "${GREEN}✅ 容器部署完成${NC}"

# 清理
echo -e "\n${YELLOW}🧹 清理临时文件...${NC}"
rm -f deployment_package.tar.gz

echo -e "\n${GREEN}============================================================${NC}"
echo -e "${GREEN}✅ 部署完成！${NC}"
echo -e "${GREEN}============================================================${NC}"

echo -e "\n${YELLOW}📋 后续操作：${NC}"
echo -e "1. 查看日志："
echo -e "   ssh ${SERVER_USER}@${SERVER_IP} 'docker logs -f grid-signal-bot'"
echo -e "\n2. 检查容器状态："
echo -e "   ssh ${SERVER_USER}@${SERVER_IP} 'docker ps -f name=grid-signal-bot'"
echo -e "\n3. 重启容器："
echo -e "   ssh ${SERVER_USER}@${SERVER_IP} 'docker restart grid-signal-bot'"
