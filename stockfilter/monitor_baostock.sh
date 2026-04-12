#!/bin/bash
# 监控 Baostock 数据获取进度

TARGET=5000  # 目标股票数量

while true; do
    CURRENT=$(ls /Users/yl/vscode/stockfilter/data/backtest/baostocks/*.csv 2>/dev/null | wc -l)
    TIME=$(date "+%H:%M:%S")
    
    echo "[$TIME] 已获取：$CURRENT 只股票（目标：$TARGET）"
    
    if [ "$CURRENT" -ge "$TARGET" ]; then
        echo ""
        echo "======================================"
        echo "✅ 数据获取完成！"
        echo "======================================"
        echo ""
        echo "已获取 $CURRENT 只股票的数据"
        echo ""
        echo "下一步操作："
        echo "1. 运行回测：python3 batch_backtest.py"
        echo "2. 查看报告：cat backtest_report.md"
        echo ""
        break
    fi
    
    sleep 300  # 每 5 分钟检查一次
done
