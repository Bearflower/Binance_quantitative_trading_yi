#!/bin/bash
# HRS 策略批量回测一键脚本
# 用法：
#   ./batch_backtest.sh                                    # 默认: LABUSDT,SAHARAUSDT 30天
#   ./batch_backtest.sh "LABUSDT,SAHARAUSDT,1000PEPEUSDT"  # 自定义交易对
#   ./batch_backtest.sh "LABUSDT" 7                        # 自定义交易对和天数
set -e

cd "$(dirname "$0")/../.."

echo "=============================================="
echo "  HRS 策略批量回测"
echo "=============================================="
echo "  交易对: ${1:-LABUSDT,SAHARAUSDT}"
echo "  数据天数: ${2:-30}"
echo "=============================================="
echo ""

python3 backtest/hrs/run_backtest.py \
    --symbols "${1:-LABUSDT,SAHARAUSDT}" \
    --days "${2:-30}"

echo ""
echo "=============================================="
echo "  回测完成"
echo "=============================================="