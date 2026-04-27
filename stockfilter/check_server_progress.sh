#!/bin/bash
# 监控数据获取进度

echo "======================================"
echo "K 线数据获取进度（服务器端）"
echo "======================================"
echo ""

# 检查进程
ps_count=$(ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184 "ps aux | grep python | grep -v grep | wc -l")
echo "【进程状态】"
if [ "$ps_count" -gt 0 ]; then
    echo "✅ 脚本正在运行"
else
    echo "⚠️  未检测到运行脚本"
fi

echo ""
echo "【数据库状态】"

# 查询进度
result=$(ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184 "docker exec -i stockfilter-app python3 -c \"from data.database import DatabaseManager; db = DatabaseManager(); import pandas as pd; df = pd.read_sql('SELECT COUNT(DISTINCT code) as cnt FROM klines', db.conn); print(df.iloc[0,0]); db.close()\" 2>/dev/null")

if [ -n "$result" ]; then
    echo "已获取 K 线数据的股票数量：$result 只"
    echo "（目标：约 3065 只沪市 + 深市主板股票）"
fi

# 查询总记录数
total=$(ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184 "docker exec -i stockfilter-app python3 -c \"from data.database import DatabaseManager; db = DatabaseManager(); import pandas as pd; df = pd.read_sql('SELECT COUNT(*) as cnt FROM klines', db.conn); print(df.iloc[0,0]); db.close()\" 2>/dev/null")

if [ -n "$total" ]; then
    echo "K 线数据总记录数：$total 条"
fi

echo ""
echo "【最新获取的股票】"
ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184 "docker exec -i stockfilter-app python3 -c \"from data.database import DatabaseManager; db = DatabaseManager(); import pandas as pd; df = pd.read_sql('SELECT code, COUNT(*) as cnt FROM klines GROUP BY code ORDER BY cnt DESC LIMIT 10', db.conn); print(df.to_string(index=False)); db.close()\" 2>/dev/null"

echo ""
echo "======================================"
echo "下一步操作："
echo "1. 等待数据获取完成（约 1-2 小时）"
echo "2. 本地导出：python3 export_all_sh_stocks.py"
echo "3. 本地回测：python3 batch_backtest.py"
echo "======================================"
