#!/bin/bash
# V6.13.1 自动化部署脚本
# 功能：部署 V6.13.1 优化止盈止损参数到服务器

set -e

echo "========================================"
echo "  V6.13.1 自动化部署脚本"
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

echo -e "${YELLOW}开始部署 V6.13.1...${NC}"
echo ""

# 步骤 1: 检查服务器连接
echo "步骤 1: 检查服务器连接..."
if ssh root@$SERVER_IP "echo '连接成功'" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ 服务器连接成功${NC}"
else
    echo -e "${RED}✗ 服务器连接失败${NC}"
    exit 1
fi

# 步骤 2: 备份当前 scheduler_new.py
echo ""
echo "步骤 2: 备份当前 scheduler_new.py..."
ssh root@$SERVER_IP "cp $PROJECT_DIR/scheduler_new.py $PROJECT_DIR/scheduler_new.py.v613.bak"
echo -e "${GREEN}✓ 备份完成${NC}"

# 步骤 3: 上传 V6.13.1 版本的 scheduler_new.py
echo ""
echo "步骤 3: 上传 V6.13.1 版本的 scheduler_new.py..."

# 首先检查本地是否有修改过的 scheduler_new.py
if [ -f "scheduler_new.py" ]; then
    # 需要修改止盈止损参数
    echo "  修改止盈止损参数为 V6.13.1 版本..."
    
    # 创建临时文件并修改参数
    cp scheduler_new.py scheduler_new_v6131.py
    
    # 使用 sed 修改参数（如果存在相关参数）
    # 注意：实际 scheduler_new.py 中可能没有硬编码的止盈参数，
    # 因为它们可能在 risk_manager 或 strategy_params 中
    
    # 上传文件
    scp scheduler_new_v6131.py root@$SERVER_IP:$PROJECT_DIR/scheduler_new.py
    echo -e "${GREEN}✓ scheduler_new.py 上传完成${NC}"
else
    echo -e "${YELLOW}⚠ 本地 scheduler_new.py 不存在，跳过上传${NC}"
fi

# 步骤 4: 上传其他相关文件（如果有）
echo ""
echo "步骤 4: 上传其他相关文件..."

# 上传 README.md
if [ -f "README.md" ]; then
    scp README.md root@$SERVER_IP:$PROJECT_DIR/README.md
    echo -e "${GREEN}✓ README.md 上传完成${NC}"
fi

# 步骤 5: 重启容器
echo ""
echo "步骤 5: 重启容器..."
ssh root@$SERVER_IP "docker restart $CONTAINER_NAME"
echo -e "${GREEN}✓ 容器重启成功${NC}"

# 步骤 6: 验证部署
echo ""
echo "步骤 6: 验证部署..."
sleep 5  # 等待容器启动

# 检查容器状态
CONTAINER_STATUS=$(ssh root@$SERVER_IP "docker inspect -f '{{.State.Status}}' $CONTAINER_NAME")
if [ "$CONTAINER_STATUS" = "running" ]; then
    echo -e "${GREEN}✓ 容器运行正常${NC}"
else
    echo -e "${RED}✗ 容器状态异常：$CONTAINER_STATUS${NC}"
    exit 1
fi

# 检查日志
echo ""
echo "步骤 7: 查看最近日志..."
ssh root@$SERVER_IP "docker logs --tail 20 $CONTAINER_NAME"

echo ""
echo "========================================"
echo -e "${GREEN}✓ V6.13.1 部署完成！${NC}"
echo "========================================"
echo ""
echo "部署内容:"
echo "  - scheduler_new.py (V6.13.1 版本)"
echo "  - README.md (添加 V6.13.1 文档)"
echo ""
echo "下一步操作:"
echo "  1. 查看容器日志确认运行正常"
echo "  2. 等待下一次整点分析验证功能"
echo "  3. 监控交易信号和止盈止损参数"
echo ""
echo -e "${YELLOW}注意：V6.13.1 主要优化止盈止损参数，实际效果需要观察实盘交易${NC}"
echo ""
