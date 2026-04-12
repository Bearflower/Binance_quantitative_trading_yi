#!/bin/bash
# 在服务器上获取全市场 K 线数据并导出到本地

echo "======================================"
echo "步骤 1: 在服务器上获取全市场 K 线数据"
echo "======================================"

# 在 Docker 容器中运行获取脚本
docker exec -i stockfilter-app python3 << 'PYTHON_SCRIPT'
from data.database import DatabaseManager
from data.fetcher import get_stock_daily_kline
import time

db = DatabaseManager()

# 获取股票列表
stocks_df = db.get_stock_list()
print(f"找到 {len(stocks_df)} 只股票")

# 只获取沪市和深市主板股票（过滤创业板和科创板以减少数据量）
stocks_df = stocks_df[
    stocks_df['code'].str.startswith(('60', '00'))
]
print(f"过滤后剩余 {len(stocks_df)} 只股票（沪市 + 深市主板）")

success_count = 0
error_count = 0

for idx, row in stocks_df.iterrows():
    code = row['code']
    symbol = row['symbol']
    
    try:
        # 检查是否已有数据
        existing = db.get_kline_history(code, days=300)
        if existing is not None and len(existing) > 200:
            print(f"[{idx+1}/{len(stocks_df)}] {code} - 已有数据，跳过")
            continue
        
        # 获取 K 线数据
        print(f"[{idx+1}/{len(stocks_df)}] {code} - 正在获取...", end=" ")
        df = get_stock_daily_kline(symbol, days=300)
        
        if df is not None and len(df) > 0:
            db.save_kline_history(code, df)
            print(f"✅ 成功 ({len(df)}条)")
            success_count += 1
        else:
            print(f"❌ 失败")
            error_count += 1
        
        # 每 100 只股票暂停一下，避免请求过快
        if (idx + 1) % 100 == 0:
            print(f"进度：{idx+1}/{len(stocks_df)}，成功：{success_count}，失败：{error_count}，暂停 10 秒...")
            time.sleep(10)
        
    except Exception as e:
        print(f"❌ {code} 异常：{e}")
        error_count += 1

print(f"\n完成！成功：{success_count}，失败：{error_count}")
db.close()
PYTHON_SCRIPT

echo ""
echo "======================================"
echo "步骤 2: 导出 K 线数据到本地"
echo "======================================"
echo "请在本地运行：python3 export_all_sh_stocks.py"
