#!/bin/bash
# 启动数据获取并自动回测的脚本

echo "=============================================================="
echo "📊 股票数据获取与自动回测启动脚本"
echo "=============================================================="
echo ""

# 检查是否已存在数据获取进程
if ps aux | grep -i "fetch_full_history_baostock.py" | grep -v grep > /dev/null; then
    echo "⚠️  数据获取进程已经在运行中"
    echo ""
    echo "查看进度命令："
    echo "  tail -f /tmp/fetch_full_history.log"
    echo ""
    echo "查看已获取股票数："
    echo "  ls data/backtest/baostocks_full/*.csv | wc -l"
    echo ""
    exit 1
fi

# 启动监控脚本（后台运行）
echo "🚀 启动监控脚本..."
echo "  - 监控数据获取进程"
echo "  - 自动执行批量回测"
echo ""

# 使用 nohup 后台运行
nohup python3 auto_backtest_after_fetch.py > /tmp/auto_backtest_monitor.log 2>&1 &

echo "✅ 监控脚本已启动（后台运行）"
echo ""
echo "=============================================================="
echo "📋 使用说明"
echo "=============================================================="
echo ""
echo "查看监控日志："
echo "  tail -f /tmp/auto_backtest_monitor.log"
echo ""
echo "查看数据获取进度："
echo "  tail -f /tmp/fetch_full_history.log"
echo ""
echo "检查进程状态："
echo "  ps aux | grep auto_backtest"
echo ""
echo "停止监控（如需要）："
echo "  pkill -f auto_backtest_after_fetch.py"
echo ""
echo "=============================================================="
echo "✨ 监控已启动，数据获取完成后将自动执行回测"
echo "=============================================================="
