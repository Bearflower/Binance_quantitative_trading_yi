#!/usr/bin/env python3
"""
分析主板股票 (3,026 只) 的 K 线数据完整性
"""

import os
from data.database import DatabaseManager

# 设置数据库连接
os.environ['DB_HOST'] = '10.3.0.12'
os.environ['DB_PORT'] = '5432'
os.environ['DB_NAME'] = 'stockfilter'
os.environ['DB_USER'] = 'stockfilter_user'
os.environ['DB_PASSWORD'] = 'Stock@2024'
os.environ['DB_SCHEMA'] = 'schema_stockfilter'

db = DatabaseManager()
cur = db.conn.cursor()

print("=" * 80)
print("主板股票 (3,026 只) K 线数据完整性分析")
print("=" * 80)

# 1. 基础统计
cur.execute("""
    SELECT COUNT(*) FROM stocks 
    WHERE code LIKE '600%' OR code LIKE '601%' OR code LIKE '603%' OR code LIKE '605%' 
          OR code LIKE '000%' OR code LIKE '001%' OR code LIKE '002%'
""")
total_main_board = cur.fetchone()[0]
print(f"\n📊 主板股票总数：{total_main_board}")

# 2. 4 月份数据完整性
cur.execute("""
    SELECT COUNT(DISTINCT k.code) 
    FROM klines k 
    INNER JOIN stocks s ON k.code = s.code 
    WHERE (s.code LIKE '600%' OR s.code LIKE '601%' OR s.code LIKE '603%' OR s.code LIKE '605%' 
           OR s.code LIKE '000%' OR s.code LIKE '001%' OR s.code LIKE '002%')
    AND k.date >= '2026-04-01'
""")
april_stocks = cur.fetchone()[0]
print(f"\n📅 4 月份有数据的主板股票：{april_stocks} / {total_main_board} ({april_stocks/total_main_board*100:.1f}%)")

cur.execute("""
    SELECT COUNT(*) 
    FROM klines k 
    INNER JOIN stocks s ON k.code = s.code 
    WHERE (s.code LIKE '600%' OR s.code LIKE '601%' OR s.code LIKE '603%' OR s.code LIKE '605%' 
           OR s.code LIKE '000%' OR s.code LIKE '001%' OR s.code LIKE '002%')
    AND k.date >= '2026-04-01'
""")
april_records = cur.fetchone()[0]
print(f"📈 4 月份 K 线数据总条数：{april_records}")
print(f"📊 平均每只股票 4 月份数据条数：{april_records/april_stocks:.1f} 天")

# 3. 昨天 (4 月 9 日) 数据
cur.execute("""
    SELECT COUNT(DISTINCT k.code) 
    FROM klines k 
    INNER JOIN stocks s ON k.code = s.code 
    WHERE (s.code LIKE '600%' OR s.code LIKE '601%' OR s.code LIKE '603%' OR s.code LIKE '605%' 
           OR s.code LIKE '000%' OR s.code LIKE '001%' OR s.code LIKE '002%')
    AND k.date = '2026-04-09'
""")
yesterday_stocks = cur.fetchone()[0]
print(f"\n📅 昨天 (4 月 9 日) 有数据的主板股票：{yesterday_stocks} / {total_main_board} ({yesterday_stocks/total_main_board*100:.1f}%)")

cur.execute("""
    SELECT COUNT(*) 
    FROM klines k 
    INNER JOIN stocks s ON k.code = s.code 
    WHERE (s.code LIKE '600%' OR s.code LIKE '601%' OR s.code LIKE '603%' OR s.code LIKE '605%' 
           OR s.code LIKE '000%' OR s.code LIKE '001%' OR s.code LIKE '002%')
    AND k.date = '2026-04-09'
""")
yesterday_records = cur.fetchone()[0]
print(f"📈 昨天 K 线数据条数：{yesterday_records}")

# 4. 日期范围统计
cur.execute("""
    SELECT MAX(date), MIN(date) 
    FROM klines 
    WHERE date >= '2026-04-01'
""")
max_date, min_date = cur.fetchone()
print(f"\n📅 4 月份数据日期范围：{min_date} 到 {max_date}")

# 5. 缺失数据的股票
cur.execute("""
    SELECT code, name 
    FROM stocks 
    WHERE (code LIKE '600%' OR code LIKE '601%' OR code LIKE '603%' OR code LIKE '605%' 
           OR code LIKE '000%' OR code LIKE '001%' OR code LIKE '002%')
    AND code NOT IN (
        SELECT DISTINCT code FROM klines WHERE date >= '2026-04-01'
    )
""")
missing_stocks = cur.fetchall()
print(f"\n⚠️  4 月份缺失数据的股票数：{len(missing_stocks)}")
if len(missing_stocks) > 0 and len(missing_stocks) < 50:
    print("缺失股票列表:")
    for code, name in missing_stocks:
        print(f"  {code} - {name}")

# 6. 昨天缺失数据的股票
cur.execute("""
    SELECT code, name 
    FROM stocks 
    WHERE (code LIKE '600%' OR code LIKE '601%' OR code LIKE '603%' OR code LIKE '605%' 
           OR code LIKE '000%' OR code LIKE '001%' OR code LIKE '002%')
    AND code NOT IN (
        SELECT DISTINCT code FROM klines WHERE date = '2026-04-09'
    )
""")
yesterday_missing = cur.fetchall()
print(f"\n⚠️  昨天 (4 月 9 日) 缺失数据的股票数：{len(yesterday_missing)}")
if len(yesterday_missing) > 0 and len(yesterday_missing) < 50:
    print("缺失股票列表 (前 30 只):")
    for code, name in yesterday_missing[:30]:
        print(f"  {code} - {name}")
    if len(yesterday_missing) > 30:
        print(f"  ... 还有 {len(yesterday_missing) - 30} 只股票")

# 7. 数据完整性总结
print("\n" + "=" * 80)
print("📊 数据完整性总结")
print("=" * 80)
print(f"✅ 4 月份数据完整率：{april_stocks/total_main_board*100:.1f}%")
print(f"✅ 昨天数据完整率：{yesterday_stocks/total_main_board*100:.1f}%")
if april_stocks/total_main_board*100 >= 99:
    print("✅ 4 月份数据非常完整，可以用于形态扫描")
else:
    print("⚠️  4 月份数据有部分缺失，可能需要补全")

if yesterday_stocks/total_main_board*100 < 50:
    print("⚠️  昨天数据完整率较低，可能是:")
    print("   - 清明节后第一个交易日，部分股票停牌")
    print("   - 数据同步还在进行中")
    print("   - 建议等待今晚 22:00 的 K 线更新任务")

db.close()
print("\n✅ 分析完成！")
