#!/bin/bash
# 数据获取恢复脚本
# 用法：./resume_fetch.sh

echo "=============================================="
echo "检查数据获取进度"
echo "=============================================="

# 统计已获取的股票数量
count=$(ls /Users/yl/vscode/stockfilter/data/backtest/baostocks_full/*.csv 2>/dev/null | wc -l)
echo "已获取股票数：$count 只"

# 查看日志最后几行
echo ""
echo "【最后获取的股票】"
tail -10 /tmp/fetch_full_history.log | grep -E "^\["

# 查看总体进度
echo ""
echo "【总体进度】"
grep "进度：" /tmp/fetch_full_history.log | tail -1

# 检查进程是否在运行
echo ""
echo "【进程状态】"
if ps aux | grep -i "fetch_full_history" | grep -v grep > /dev/null; then
    echo "✅ 数据获取进程正在运行中"
else
    echo "❌ 数据获取进程已停止"
    echo ""
    echo "是否要重新启动？(y/n)"
    read answer
    if [ "$answer" = "y" ]; then
        echo "重新启动数据获取..."
        cd /Users/yl/vscode/stockfilter
        python3 fetch_full_history_baostock.py > /tmp/fetch_full_history.log 2>&1 &
        echo "✅ 已重新启动，后台运行中"
        echo "查看进度：tail -f /tmp/fetch_full_history.log"
    fi
fi

echo ""
echo "=============================================="
echo "查看实时进度命令："
echo "  tail -f /tmp/fetch_full_history.log"
echo ""
echo "查看已获取股票数："
echo "  ls data/backtest/baostocks_full/*.csv | wc -l"
echo "=============================================="
