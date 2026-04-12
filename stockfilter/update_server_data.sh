#!/bin/bash
# 在服务器上更新全市场股票数据的脚本

echo "======================================"
echo "在服务器上更新全市场股票数据"
echo "======================================"

# 1. 初始化股票列表（获取全市场股票）
echo ""
echo "步骤 1: 初始化股票列表..."
docker exec -it stockfilter-app python main.py --init

if [ $? -ne 0 ]; then
    echo "❌ 初始化失败"
    exit 1
fi

echo ""
echo "✅ 初始化完成"

# 2. 更新全市场 K 线数据
echo ""
echo "步骤 2: 更新全市场 K 线数据（这可能需要较长时间）..."
docker exec -it stockfilter-app python main.py --update --update-all

if [ $? -ne 0 ]; then
    echo "❌ 更新失败"
    exit 1
fi

echo ""
echo "✅ 更新完成"

# 3. 验证数据
echo ""
echo "步骤 3: 验证数据..."
docker exec -it stockfilter-app python check_server_sh_stocks.py

echo ""
echo "======================================"
echo "数据更新完成！"
echo "======================================"
