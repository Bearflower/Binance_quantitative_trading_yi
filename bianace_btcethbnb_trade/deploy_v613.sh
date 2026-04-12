#!/bin/bash
# v6.13 动态仓位调整功能部署脚本
# 版本：v6.13
# 日期：2026-04-10

set -e

echo "========================================"
echo "v6.13 动态仓位调整功能部署脚本"
echo "========================================"
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 服务器配置
SERVER_HOST="43.156.242.184"
SERVER_USER="root"
REMOTE_DIR="/root/binance-trade-analyzer"
CONTAINER_NAME="binance-trade-analyzer"

echo -e "${YELLOW}📦 准备部署的文件:${NC}"
echo "  - services/position_adjuster.py (新增)"
echo "  - config/v613_params.yaml (新增)"
echo "  - scheduler_new.py (修改)"
echo "  - docs/reports/v613 动态仓位调整更新报告.md (新增)"
echo ""

# 1. 备份现有文件
echo -e "${YELLOW}步骤 1/6: 备份现有文件...${NC}"
ssh ${SERVER_USER}@${SERVER_HOST} "cd ${REMOTE_DIR} && cp scheduler_new.py scheduler_new.py.bak.$(date +%Y%m%d_%H%M%S)"
echo -e "${GREEN}✅ 备份完成${NC}"
echo ""

# 2. 上传新文件
echo -e "${YELLOW}步骤 2/6: 上传新文件到服务器...${NC}"

# 上传动态仓位调整器
scp services/position_adjuster.py ${SERVER_USER}@${SERVER_HOST}:${REMOTE_DIR}/services/
echo "  ✅ services/position_adjuster.py"

# 上传配置文件
scp config/v613_params.yaml ${SERVER_USER}@${SERVER_HOST}:${REMOTE_DIR}/config/
echo "  ✅ config/v613_params.yaml"

# 上传调度器（已修改）
scp scheduler_new.py ${SERVER_USER}@${SERVER_HOST}:${REMOTE_DIR}/
echo "  ✅ scheduler_new.py"

# 上传文档
scp docs/reports/v613\ 动态仓位调整更新报告.md ${SERVER_USER}@${SERVER_HOST}:${REMOTE_DIR}/docs/reports/
echo "  ✅ docs/reports/v613 动态仓位调整更新报告.md"

echo -e "${GREEN}✅ 文件上传完成${NC}"
echo ""

# 3. 验证文件完整性
echo -e "${YELLOW}步骤 3/6: 验证文件完整性...${NC}"
ssh ${SERVER_USER}@${SERVER_HOST} "cd ${REMOTE_DIR} && ls -lh services/position_adjuster.py config/v613_params.yaml scheduler_new.py"
echo -e "${GREEN}✅ 文件验证完成${NC}"
echo ""

# 4. 语法检查
echo -e "${YELLOW}步骤 4/6: 执行语法检查...${NC}"
ssh ${SERVER_USER}@${SERVER_HOST} "cd ${REMOTE_DIR} && python3 -m py_compile services/position_adjuster.py scheduler_new.py"
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ 语法检查通过${NC}"
else
    echo -e "${RED}❌ 语法检查失败${NC}"
    exit 1
fi
echo ""

# 5. 重启容器
echo -e "${YELLOW}步骤 5/6: 重启 Docker 容器...${NC}"
ssh ${SERVER_USER}@${SERVER_HOST} "docker restart ${CONTAINER_NAME}"
echo -e "${GREEN}✅ 容器重启完成${NC}"
echo ""

# 6. 验证部署
echo -e "${YELLOW}步骤 6/6: 验证部署是否成功...${NC}"
echo "等待 5 秒让容器启动..."
sleep 5

# 查看日志，确认 v6.13 功能已加载
ssh ${SERVER_USER}@${SERVER_HOST} "docker logs --tail 50 ${CONTAINER_NAME} | grep -E 'v6.13|动态仓位|PositionAdjuster'"

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ v6.13 功能已成功加载${NC}"
else
    echo -e "${YELLOW}⚠️  未找到 v6.13 相关日志，请手动检查${NC}"
fi

echo ""
echo "========================================"
echo -e "${GREEN}🎉 v6.13 部署完成！${NC}"
echo "========================================"
echo ""
echo -e "${YELLOW}📋 验证方法:${NC}"
echo "  1. SSH 登录服务器：ssh root@43.156.242.184"
echo "  2. 查看容器日志：docker logs -f ${CONTAINER_NAME} | grep '动态调仓'"
echo "  3. 查看配置文件：cat ${REMOTE_DIR}/config/v613_params.yaml"
echo ""
echo -e "${YELLOW}📊 预期效果:${NC}"
echo "  - 资金充足时：不调整，全额执行"
echo "  - 资金略不足：自动降仓（60-90%）"
echo "  - 资金严重不足：跳过交易（<5U）"
echo ""
echo -e "${YELLOW}⚠️  注意事项:${NC}"
echo "  - 首次执行会显示'动态仓位调整器 v6.13 初始化完成'日志"
echo "  - 资金不足时会显示'触发 v6.13 动态调仓'日志"
echo "  - 最小保证金阈值：5U（可在配置文件中调整）"
echo ""
