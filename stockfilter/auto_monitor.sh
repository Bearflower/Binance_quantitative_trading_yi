#!/bin/bash
# 定时检查数据获取进度，完成后提醒用户

echo "======================================"
echo "启动自动监控（每 15 分钟检查一次）"
echo "======================================"
echo ""

TARGET_COUNT=3000  # 目标股票数量
CHECK_INTERVAL=900  # 检查间隔（秒）= 15 分钟

while true; do
    CURRENT_TIME=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$CURRENT_TIME] 检查进度..."
    
    # 查询当前进度
    RESULT=$(ssh -i ~/.ssh/stockfilter_key root@43.156.242.184 "docker exec -i stockfilter-app python3 -c \"from data.database import DatabaseManager; db = DatabaseManager(); import pandas as pd; df = pd.read_sql('SELECT COUNT(DISTINCT code) as cnt FROM klines', db.conn); print(df.iloc[0,0]); db.close()\" 2>/dev/null")
    
    if [ -n "$RESULT" ]; then
        echo "  已获取：$RESULT 只股票（目标：$TARGET_COUNT 只）"
        
        # 检查是否完成
        if [ "$RESULT" -ge "$TARGET_COUNT" ]; then
            echo ""
            echo "======================================"
            echo "✅ 数据获取完成！"
            echo "======================================"
            echo ""
            echo "已获取 $RESULT 只股票的 K 线数据"
            echo ""
            echo "下一步操作："
            echo "1. 导出到本地：python3 export_all_sh_stocks.py"
            echo "2. 运行回测：python3 batch_backtest.py"
            echo ""
            
            # 发送提醒（可以添加其他通知方式）
            echo "🔔 提醒：数据已获取完成，可以进行本地回测！"
            
            break
        fi
    else
        echo "  ⚠️ 无法获取进度，请检查服务器连接"
    fi
    
    echo "  下次检查：15 分钟后"
    echo ""
    sleep $CHECK_INTERVAL
done
