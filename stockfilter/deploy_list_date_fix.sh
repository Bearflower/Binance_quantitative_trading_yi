#!/bin/bash
# list_date 字段修复部署脚本
# 修复 AData 'list_date2' 错误和 SQL list index out of range 错误

set -e

echo "============================================================"
echo "股票列表 list_date 字段修复部署"
echo "============================================================"

# 配置
SSH_KEY="${HOME}/.ssh/stockfilter_key"
SERVER_USER="root"
SERVER_HOST="47.118.53.81"
CONTAINER_NAME="stockfilter-app"
SERVER_PATH="/root/stockfilter"

# 颜色
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "\n${YELLOW}[1/6] 检查 SSH 连接...${NC}"
if ssh -i "$SSH_KEY" -o ConnectTimeout=10 "${SERVER_USER}@${SERVER_HOST}" "echo '连接成功'" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ SSH 连接正常${NC}"
else
    echo -e "${RED}✗ SSH 连接失败，请检查网络和 SSH 密钥${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[2/6] 运行本地测试...${NC}"
if [ -f "test_list_date_logic.py" ]; then
    if python3 test_list_date_logic.py > /dev/null 2>&1; then
        echo -e "${GREEN}✓ 本地测试通过${NC}"
    else
        echo -e "${RED}✗ 本地测试失败，请检查代码${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}⚠  测试文件不存在，跳过测试${NC}"
fi

echo -e "\n${YELLOW}[3/6] 上传 data_source.py 到服务器...${NC}"
if scp -i "$SSH_KEY" data/data_source.py "${SERVER_USER}@${SERVER_HOST}:${SERVER_PATH}/data/"; then
    echo -e "${GREEN}✓ 文件上传成功${NC}"
else
    echo -e "${RED}✗ 文件上传失败${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[4/6] 复制文件到容器...${NC}"
if ssh -i "$SSH_KEY" "${SERVER_USER}@${SERVER_HOST}" "docker cp ${SERVER_PATH}/data/data_source.py ${CONTAINER_NAME}:/app/data/"; then
    echo -e "${GREEN}✓ 文件复制到容器成功${NC}"
else
    echo -e "${RED}✗ 复制到容器失败${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[5/6] 重启容器...${NC}"
if ssh -i "$SSH_KEY" "${SERVER_USER}@${SERVER_HOST}" "docker restart ${CONTAINER_NAME}"; then
    echo -e "${GREEN}✓ 容器重启成功${NC}"
else
    echo -e "${RED}✗ 容器重启失败${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[6/6] 验证部署...${NC}"
echo -e "\n${YELLOW}最近日志：${NC}"
ssh -i "$SSH_KEY" "${SERVER_USER}@${SERVER_HOST}" "docker logs --tail 30 ${CONTAINER_NAME} 2>&1"

echo -e "\n${YELLOW}检查错误（应该没有 list_date 相关错误）：${NC}"
ssh -i "$SSH_KEY" "${SERVER_USER}@${SERVER_HOST}" "docker logs --tail 100 ${CONTAINER_NAME} 2>&1 | grep -i 'list_date' || echo '没有 list_date 相关错误'"

echo -e "\n${YELLOW}验证股票列表字段：${NC}"
ssh -i "$SSH_KEY" "${SERVER_USER}@${SERVER_HOST}" "docker exec ${CONTAINER_NAME} python -c \"
from data.database import DatabaseManager
db = DatabaseManager()
stocks = db.get_stock_list()
print(f'列名：{stocks.columns.tolist()}')
print(f'股票总数：{len(stocks)}只')
if 'list_date' in stocks.columns:
    print(f'✓ list_date 字段存在')
    print(f'  非空：{stocks[\"list_date\"].notna().sum()}只')
    print(f'  空值：{stocks[\"list_date\"].isna().sum()}只')
else:
    print('✗ list_date 字段不存在')
db.close()
\"" 2>/dev/null || echo "验证命令执行失败"

echo -e "\n${GREEN}============================================================"
echo "✅ 部署完成！"
echo "============================================================${NC}"
echo -e "\n${YELLOW}后续验证建议：${NC}"
echo "1. 等待下一次定时任务执行（15:30）"
echo "2. 查看完整日志：ssh -i ${SSH_KEY} ${SERVER_USER}@${SERVER_HOST} \"docker logs ${CONTAINER_NAME}\""
echo "3. 查看应用日志：ssh -i ${SSH_KEY} ${SERVER_USER}@${SERVER_HOST} \"tail -f ${SERVER_PATH}/logs/app.log\""
echo ""
