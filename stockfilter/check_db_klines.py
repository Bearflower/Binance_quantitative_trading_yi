#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查数据库 K 线数据详情"""

from data.database import DatabaseManager
import pandas as pd

db = DatabaseManager()

# 1. 检查总记录数和股票数量
df1 = pd.read_sql_query("""
    SELECT 
        COUNT(*) as total_records,
        COUNT(DISTINCT code) as unique_stocks,
        MIN(date) as earliest_date,
        MAX(date) as latest_date
    FROM klines
""", db.conn)

print("=" * 80)
print("数据库 K 线数据总览:")
print("=" * 80)
print(f"总记录数：{df1['total_records'].iloc[0]:,} 条")
print(f"股票数量：{df1['unique_stocks'].iloc[0]} 只")
print(f"最早日期：{df1['earliest_date'].iloc[0]}")
print(f"最新日期：{df1['latest_date'].iloc[0]}")
print()

# 2. 检查数据分布（按年份）
df2 = pd.read_sql_query("""
    SELECT 
        EXTRACT(YEAR FROM date) as year,
        COUNT(*) as record_count,
        COUNT(DISTINCT code) as stock_count
    FROM klines
    GROUP BY EXTRACT(YEAR FROM date)
    ORDER BY year
""", db.conn)

print("按年份统计:")
print("-" * 80)
for _, row in df2.iterrows():
    print(f"{int(row['year'])}年：{row['record_count']:>10,} 条数据，{int(row['stock_count']):>4} 只股票")
print()

# 3. 检查最近 30 天的数据量
df3 = pd.read_sql_query("""
    SELECT 
        date,
        COUNT(*) as stock_count
    FROM klines
    WHERE date >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY date
    ORDER BY date DESC
""", db.conn)

print("最近 30 天数据量（部分展示）:")
print("-" * 80)
if len(df3) > 0:
    for _, row in df3.head(10).iterrows():
        print(f"{row['date']}: {row['stock_count']} 只股票")
    if len(df3) > 10:
        print(f"... 还有 {len(df3) - 10} 天")
else:
    print("无最近 30 天数据")
print()

# 4. 检查单只股票的数据量（随机抽样）
df4 = pd.read_sql_query("""
    SELECT code, COUNT(*) as days
    FROM klines
    GROUP BY code
    ORDER BY days DESC
    LIMIT 10
""", db.conn)

print("数据天数最多的 10 只股票:")
print("-" * 80)
for _, row in df4.iterrows():
    print(f"{row['code']}: {row['days']} 天")
print()

# 5. 计算平均每只股票的数据天数
avg_days = df1['total_records'].iloc[0] / df1['unique_stocks'].iloc[0]
print(f"平均每只股票数据天数：{avg_days:.1f} 天")
print("=" * 80)

db.close()
