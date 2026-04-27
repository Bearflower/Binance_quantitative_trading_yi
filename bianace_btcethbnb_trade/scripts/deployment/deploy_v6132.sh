#!/bin/bash
# V6.13.2 限价单优化部署脚本
# 功能：将开仓订单从市价单改为限价单，节省 60% 手续费

set -e

echo "========================================"
echo "  V6.13.2 限价单优化部署脚本"
echo "========================================"
echo ""

SERVER_IP="43.156.242.184"
CONTAINER_NAME="binance-trade-analyzer"
PROJECT_DIR="/root/binance-trade-analyzer"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}开始部署 V6.13.2 限价单优化...${NC}"
echo ""

# 步骤 1: 检查服务器连接
echo "步骤 1: 检查服务器连接..."
if ssh root@$SERVER_IP "echo '连接成功'" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ 服务器连接成功${NC}"
else
    echo -e "${RED}✗ 服务器连接失败${NC}"
    exit 1
fi

# 步骤 2: 备份当前文件
echo ""
echo "步骤 2: 备份当前文件..."
ssh root@$SERVER_IP "
    cp $PROJECT_DIR/core/order_generator.py $PROJECT_DIR/core/order_generator.py.v6131.bak
    cp $PROJECT_DIR/utils/binance_api.py $PROJECT_DIR/utils/binance_api.py.v6131.bak
"
echo -e "${GREEN}✓ 备份完成${NC}"

# 步骤 3: 上传新版本文件
echo ""
echo "步骤 3: 上传 V6.13.2 版本文件..."

# 上传 order_generator.py
scp core/order_generator.py root@$SERVER_IP:$PROJECT_DIR/core/order_generator.py
echo -e "${GREEN}✓ order_generator.py 上传完成${NC}"

# 上传 binance_api.py
scp utils/binance_api.py root@$SERVER_IP:$PROJECT_DIR/utils/binance_api.py
echo -e "${GREEN}✓ binance_api.py 上传完成${NC}"

# 步骤 4: 上传到容器内
echo ""
echo "步骤 4: 复制文件到容器内..."
ssh root@$SERVER_IP "
    docker cp $PROJECT_DIR/core/order_generator.py $CONTAINER_NAME:/app/core/order_generator.py
    docker cp $PROJECT_DIR/utils/binance_api.py $CONTAINER_NAME:/app/utils/binance_api.py
"
echo -e "${GREEN}✓ 文件已复制到容器${NC}"

# 步骤 5: 重启容器
echo ""
echo "步骤 5: 重启容器..."
ssh root@$SERVER_IP "docker restart $CONTAINER_NAME"
echo -e "${GREEN}✓ 容器重启成功${NC}"

# 步骤 6: 验证部署
echo ""
echo "步骤 6: 验证部署..."
sleep 5

# 检查容器状态
CONTAINER_STATUS=$(ssh root@$SERVER_IP "docker inspect -f '{{.State.Status}}' $CONTAINER_NAME")
if [ "$CONTAINER_STATUS" = "running" ]; then
    echo -e "${GREEN}✓ 容器运行正常${NC}"
else
    echo -e "${RED}✗ 容器状态异常：$CONTAINER_STATUS${NC}"
    exit 1
fi

# 检查限价单函数是否存在
echo ""
echo "步骤 7: 检查 V6.13.2 功能..."
if ssh root@$SERVER_IP "docker exec $CONTAINER_NAME grep -q 'generate_limit_order_params' /app/core/order_generator.py"; then
    echo -e "${GREEN}✓ 限价单函数已部署${NC}"
else
    echo -e "${RED}✗ 限价单函数未找到${NC}"
    exit 1
fi

# 检查订单簿 API 是否存在
if ssh root@$SERVER_IP "docker exec $CONTAINER_NAME grep -q 'get_orderbook_data' /app/utils/binance_api.py"; then
    echo -e "${GREEN}✓ 订单簿 API 已部署${NC}"
else
    echo -e "${RED}✗ 订单簿 API 未找到${NC}"
    exit 1
fi

echo ""
echo "========================================"
echo -e "${GREEN}✓ V6.13.2 部署完成！${NC}"
echo "========================================"
echo ""
echo "优化内容:"
echo "  ✅ 开仓订单：市价单 → 限价单"
echo "  ✅ 手续费：taker 0.05% → maker 0.02%（节省 60%）"
echo "  ✅ 做多：按买一价下单，做空：按卖一价下单"
echo ""
echo "预期效果（以每天 4 笔交易计算）:"
echo "  - 市价单月手续费：约 12U"
echo "  - 限价单月手续费：约 4.8U"
echo "  - 每月节省：约 7.2U"
echo ""
echo -e "${YELLOW}下一步操作:${NC}"
echo "  1. 等待下一次整点分析验证功能"
echo "  2. 观察限价单是否正常成交"
echo "  3. 查看飞书推送中的手续费变化"
echo ""
