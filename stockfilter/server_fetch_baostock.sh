#!/bin/bash
# 在服务器上使用 Baostock 批量获取全市场 K 线数据（近半年）

echo "======================================"
echo "服务器 Baostock 批量获取 K 线数据"
echo "日期范围：2025-08-01 到 2026-04-03"
echo "======================================"
echo ""

docker exec -i stockfilter-app python3 << 'PYEOF'
import baostock as bs
import pandas as pd
from datetime import datetime
from data.database import DatabaseManager
import time

print("登录 Baostock...")
bs.login()

db = DatabaseManager()

# 生成股票代码列表
stock_list = []

# 沪市 A 股
print("生成沪市股票代码...")
for i in range(600000, 610000):
    code = f"{i:06d}"
    stock_list.append(f"sh.{code}")

# 深市 A 股
print("生成深市股票代码...")
for i in range(1, 3000):
    code = f"{i:06d}"
    stock_list.append(f"sz.{code}")

print(f"共 {len(stock_list)} 只股票")

success = 0
error = 0
no_data = 0

start_date = '2025-08-01'
end_date = '2026-04-03'

print(f"\n开始获取 {start_date} 到 {end_date} 的数据...\n")

start_time = datetime.now()

for idx, symbol in enumerate(stock_list):
    code = symbol.split('.')[1]
    
    try:
        # 获取 K 线数据
        rs = bs.query_history_k_data_plus(
            symbol,
            "date,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="3"  # 后复权
        )
        
        data_list = []
        while (rs.error_code == '0') and rs.next():
            data_list.append(rs.get_row_data())
        
        if data_list:
            df = pd.DataFrame(data_list, columns=rs.fields)
            df['date'] = pd.to_datetime(df['date'])
            
            # 转换数值类型
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # 保存到数据库
            db.save_kline_history(code, df)
            success += 1
            
            # 每 100 只打印进度
            if (idx + 1) % 100 == 0:
                elapsed = (datetime.now() - start_time).total_seconds() / 60
                rate = (idx+1) / elapsed if elapsed > 0 else 0
                print(f"[{idx+1}/{len(stock_list)}] 成功:{success} 失败:{error} 无数据:{no_data} | {elapsed:.1f}m | 速度:{rate:.1f}只/m")
        else:
            no_data += 1
            
    except Exception as e:
        error += 1
    
    # 每 100 只暂停 0.5 秒
    if (idx + 1) % 100 == 0:
        time.sleep(0.5)

bs.logout()

end_time = datetime.now()
total_time = (end_time - start_time).total_seconds() / 60

print(f"\n{'='*80}")
print(f"完成！")
print(f"成功：{success}")
print(f"失败：{error}")
print(f"无数据：{no_data}")
print(f"总用时：{total_time:.1f}分钟")
print(f"{'='*80}")

db.close()
PYEOF

echo ""
echo "======================================"
echo "服务器数据获取完成！"
echo "======================================"
