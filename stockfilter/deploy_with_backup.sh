#!/bin/bash

# ============================================
# 服务器部署脚本 - 保留数据库版本
# ============================================

SSH_KEY="$HOME/.ssh/stockfilter_key"
SERVER="root@43.156.242.184"
PROJECT_DIR="/root/stockfilter"

echo "============================================="
echo "股票形态筛选系统 - 服务器部署"
echo "============================================="
echo ""

# 测试 SSH 连接
echo "🔑 测试 SSH 连接..."
if ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i $SSH_KEY $SERVER "echo 成功" >/dev/null 2>&1; then
    echo "✅ SSH 连接成功"
else
    echo "❌ SSH 连接失败"
    exit 1
fi

echo ""
echo "📋 部署步骤:"
echo "1. 备份数据库文件"
echo "2. 停止旧容器"
echo "3. 上传并解压"
echo "4. 恢复数据库文件"
echo "5. 构建 Docker 镜像"
echo "6. 启动新容器"
echo ""
echo "============================================="
echo "开始部署..."
echo "============================================="

# 步骤 1: 备份数据库文件
echo "💾 [1/6] 备份数据库文件..."
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER << 'ENDSSH'
cd /root/stockfilter
if [ -f "data/stock_scanner.db" ]; then
    echo "备份数据库..."
    cp data/stock_scanner.db /tmp/stock_scanner.db.backup
    echo "✅ 数据库已备份到 /tmp/stock_scanner.db.backup"
else
    echo "ℹ️  数据库不存在，跳过备份"
fi
ENDSSH

# 步骤 2: 停止旧容器
echo "🛑 [2/6] 停止旧容器..."
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER \
    "cd $PROJECT_DIR && docker-compose down" || echo "⚠️  没有运行中的容器"

# 步骤 3: 上传并解压（保留 data 目录）
echo "📦 [3/6] 上传并解压..."
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER << 'ENDSSH'
cd /root/stockfilter
echo "解压项目文件..."
# 只解压特定目录，保留 data 目录
tar -xzf deployment_package.tar.gz --exclude='data'
echo "✅ 解压完成"
ENDSSH

# 步骤 4: 恢复数据库文件
echo "🔄 [4/6] 恢复数据库文件..."
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER << 'ENDSSH'
cd /root/stockfilter
if [ -f "/tmp/stock_scanner.db.backup" ]; then
    echo "恢复数据库..."
    mkdir -p data
    cp /tmp/stock_scanner.db.backup data/stock_scanner.db
    chown -R 1000:1000 data
    chmod -R 777 data
    echo "✅ 数据库已恢复"
else
    echo "ℹ️  没有备份的数据库，将重新初始化"
    # 确保 data 目录权限正确
    mkdir -p data
    chown -R 1000:1000 data
    chmod -R 777 data
    echo "✅ data 目录权限已设置"
fi
ENDSSH

# 步骤 5: 构建镜像
echo "🏗️  [5/6] 构建 Docker 镜像..."
echo "💡 提示：构建过程需要 3-5 分钟"
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER \
    "cd $PROJECT_DIR && docker-compose build"

# 步骤 6: 启动容器
echo "🚀 [6/6] 启动容器..."
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER \
    "cd $PROJECT_DIR && docker-compose up -d"

# 等待启动
sleep 10

# 查看状态
echo ""
echo "📊 查看容器状态..."
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER \
    "docker ps -f name=stockfilter-app --format 'table {{.Names}}\t{{.Status}}'"

echo ""
echo "📝 最近日志:"
ssh -o StrictHostKeyChecking=no -i $SSH_KEY $SERVER \
    "docker logs --tail 30 stockfilter-app" 2>/dev/null || echo "暂无日志"

echo ""
echo "============================================="
echo "✅ 部署完成！"
echo "============================================="
echo ""
echo "📌 管理命令:"
echo "  查看状态：ssh -i $SSH_KEY $SERVER 'docker ps -f name=stockfilter-app'"
echo "  查看日志：ssh -i $SSH_KEY $SERVER 'docker logs -f stockfilter-app'"
echo "  重启：ssh -i $SSH_KEY $SERVER 'docker restart stockfilter-app'"
echo ""
echo "============================================="
