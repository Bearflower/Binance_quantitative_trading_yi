#!/bin/bash
# 监控 K 线数据获取进度

echo "======================================"
echo "K 线数据获取进度监控"
echo "======================================"
echo ""

# 检查后台进程
echo "【后台进程状态】"
ps_count=$(ssh -i ~/.ssh/stockfilter_key root@43.156.242.184 "ps aux | grep -E 'python.*fetch|python.*DatabaseManager' | grep -v grep | wc -l")
if [ "$ps_count" -gt 0 ]; then
    echo "✅ 后台脚本正在运行 (进程数：$ps_count)"
else
    echo "⚠️  未检测到后台脚本"
fi

echo ""
echo "【数据库状态】"

# 查询当前有 K 线数据的股票数量
result=$(ssh -i ~/.ssh/stockfilter_key root@43.156.242.184 "docker exec -i stockfilter-app python3 -c \"from data.database import DatabaseManager; db = DatabaseManager(); import pandas as pd; df = pd.read_sql('SELECT COUNT(DISTINCT code) as cnt FROM klines', db.conn); print(df.iloc[0,0]); db.close()\" 2>/dev/null")

if [ -n "$result" ]; then
    echo "当前有 K 线数据的股票数量：$result 只"
fi

# 查询总记录数
total_records=$(ssh -i ~/.ssh/stockfilter_key root@43.156.242.184 "docker exec -i stockfilter-app python3 -c \"from data.database import DatabaseManager; db = DatabaseManager(); import pandas as pd; df = pd.read_sql('SELECT COUNT(*) as cnt FROM klines', db.conn); print(df.iloc[0,0]); db.close()\" 2>/dev/null")

if [ -n "$total_records" ]; then
    echo "K 线数据总记录数：$total_records 条"
fi

echo ""
echo "【最新获取的股票】"
ssh -i ~/.ssh/stockfilter_key root@43.156.242.184 "docker exec -i stockfilter-app python3 -c \"from data.database import DatabaseManager; db = DatabaseManager(); import pandas as pd; df = pd.read_sql('SELECT code, COUNT(*) as cnt FROM klines GROUP BY code ORDER BY cnt DESC LIMIT 10', db.conn); print(df.to_string(index=False)); db.close()\" 2>/dev/null"

echo ""
echo "======================================"
echo "提示："
echo "- 预计总耗时：1-2 小时"
echo "- 每 100 只股票暂停 5 秒"
echo "- 运行此脚本查看进度：./monitor_fetch_progress.sh"
echo "======================================"
