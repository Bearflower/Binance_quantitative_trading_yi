#!/bin/bash

echo "开始部署修改后的币安合约监控程序..."

# 1. 停止并删除当前运行的容器
echo "步骤1: 停止并删除当前运行的容器..."
docker stop binance-monitor 2>/dev/null || true
docker rm binance-monitor 2>/dev/null || true

# 2. 进入binance-monitor目录
echo "步骤2: 进入binance-monitor目录..."
cd binance-monitor || {
    echo "错误: binance-monitor目录不存在"
    exit 1
}

# 3. 复制新的sd.py文件（假设已经从本地复制过来）
echo "步骤3: 复制新的sd.py文件..."
cp ~/sd.py . || {
    echo "错误: 无法复制sd.py文件"
    echo "请先将本地的sd.py文件复制到服务器上"
    exit 1
}

# 4. 重新构建Docker镜像
echo "步骤4: 重新构建Docker镜像..."
docker build -t binance-monitor . || {
    echo "错误: Docker镜像构建失败"
    exit 1
}

# 5. 运行新的容器
echo "步骤5: 运行新的容器..."
docker run -d --name binance-monitor binance-monitor || {
    echo "错误: 容器启动失败"
    exit 1
}

# 6. 验证容器状态
echo "步骤6: 验证容器状态..."
sleep 2
if docker ps | grep -q binance-monitor; then
    echo "✅ 部署成功！binance-monitor容器正在运行"
    echo "✅ 每日报告功能已禁用，您将不再收到飞书的每日报告"
    docker ps
else
    echo "❌ 容器未正常运行，请检查日志"
    docker logs binance-monitor
    exit 1
fi

echo ""
echo "部署完成！"
