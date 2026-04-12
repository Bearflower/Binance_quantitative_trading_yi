#!/bin/bash
# 深市股票数据修复 - 部署脚本
# 用途：将修改后的 data_source.py 上传到服务器并验证

set -e

# 配置
SERVER_HOST="43.156.242.184"
SERVER_USER="root"
SSH_KEY="~/.ssh/stockfilter_key"
REMOTE_APP_DIR="/app"
CONTAINER_NAME="stockfilter-app"

echo "============================================================"
echo "深市股票数据修复 - 部署脚本"
echo "============================================================"
echo ""

# 检查文件是否存在
if [ ! -f "data/data_source.py" ]; then
    echo "❌ 错误：data/data_source.py 文件不存在"
    exit 1
fi

echo "✓ 本地文件检查通过"
echo ""

# 1. 上传文件
echo "📤 正在上传修改后的文件到服务器..."
scp -i "$SSH_KEY" data/data_source.py ${SERVER_USER}@${SERVER_HOST}:${REMOTE_APP_DIR}/data/

if [ $? -eq 0 ]; then
    echo "✓ 文件上传成功"
else
    echo "❌ 文件上传失败"
    exit 1
fi

echo ""

# 2. 验证文件
echo "🔍 正在验证服务器上的文件..."
ssh -i "$SSH_KEY" ${SERVER_USER}@${SERVER_HOST} "ls -lh ${REMOTE_APP_DIR}/data/data_source.py"

echo ""

# 3. 重启容器（让修改生效）
echo "🔄 正在重启容器..."
ssh -i "$SSH_KEY" ${SERVER_USER}@${SERVER_HOST} "docker restart ${CONTAINER_NAME}"

echo ""
echo "⏳ 等待容器启动（10 秒）..."
sleep 10

# 4. 检查容器状态
echo "📊 检查容器状态..."
ssh -i "$SSH_KEY" ${SERVER_USER}@${SERVER_HOST} "docker ps -f name=${CONTAINER_NAME}"

echo ""

# 5. 测试数据源
echo "🧪 测试 AKShare 数据源..."
ssh -i "$SSH_KEY" ${SERVER_USER}@${SERVER_HOST} "docker exec ${CONTAINER_NAME} python -c \"
from data.data_source import DataSourceManager
import time

print('正在初始化数据源管理器...')
m = DataSourceManager(primary_source='akshare')

print('正在获取股票列表...')
start = time.time()
df = m.get_stock_list()
elapsed = time.time() - start

if df is not None:
    sh = df[df['code'].str.startswith('6')]
    sz = df[df['code'].str.startswith(('00', '30'))]
    print(f'✅ 获取成功！耗时：{elapsed:.2f}秒')
    print(f'   总计：{len(df)}只股票')
    print(f'   沪市：{len(sh)}只 ({len(sh)/len(df)*100:.1f}%)')
    print(f'   深市：{len(sz)}只 ({len(sz)/len(df)*100:.1f}%)')
    
    if len(sz) > 0:
        print('✅ 深市股票数据正常！')
    else:
        print('❌ 警告：没有获取到深市股票！')
else:
    print('❌ 获取失败')
    exit(1)
\""

echo ""
echo "============================================================"
echo "部署完成！"
echo "============================================================"
echo ""
echo "📝 下一步操作："
echo ""
echo "1. 查看实时日志："
echo "   ssh -i ~/.ssh/stockfilter_key root@${SERVER_HOST} \"docker logs -f ${CONTAINER_NAME}\""
echo ""
echo "2. 手动触发股票列表同步（可选）："
echo "   ssh -i ~/.ssh/stockfilter_key root@${SERVER_HOST} \"docker exec ${CONTAINER_NAME} python main.py --init\""
echo ""
echo "3. 验证数据库中的股票数量："
echo "   ssh -i ~/.ssh/stockfilter_key root@${SERVER_HOST} \"docker exec ${CONTAINER_NAME} python -c \\\"from data.database import DatabaseManager; db = DatabaseManager(); stocks = db.get_stock_list(); print(f'股票总数：{len(stocks)}')\\\"\""
echo ""
echo "✅ 修复说明："
echo "   - 首选数据源已更改为 AKShare"
echo "   - AKShare 返回完整的 A 股列表（包括深市）"
echo "   - 深市股票应占总数的约 52.5%"
echo ""
