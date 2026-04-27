#!/usr/bin/env python3
from data.database import DatabaseManager
import pandas as pd

db = DatabaseManager()

print('=' * 60)
print('数据库总体统计')
print('=' * 60)

df_stocks = pd.read_sql_query('SELECT COUNT(*) as total FROM stocks', db.conn)
print(f'股票总数：{df_stocks["total"].iloc[0]} 只')

df_klines = pd.read_sql_query('''
    SELECT
        COUNT(*) as total_records,
        COUNT(DISTINCT code) as stocks_with_data,
        MIN(date) as earliest_date,
        MAX(date) as latest_date
    FROM klines
''', db.conn)

print(f'K线总记录数：{df_klines["total_records"].iloc[0]:,} 条')
print(f'有K线数据的股票：{df_klines["stocks_with_data"].iloc[0]} 只')
print(f'最早日期：{df_klines["earliest_date"].iloc[0]}')
print(f'最新日期：{df_klines["latest_date"].iloc[0]}')

print('\n' + '=' * 60)
print('数据完整性统计')
print('=' * 60)

df_completeness = pd.read_sql_query('''
    SELECT
        COUNT(CASE WHEN cnt >= 200 THEN 1 END) as complete,
        COUNT(CASE WHEN cnt >= 100 AND cnt < 200 THEN 1 END) as good,
        COUNT(CASE WHEN cnt >= 50 AND cnt < 100 THEN 1 END) as fair,
        COUNT(CASE WHEN cnt < 50 OR cnt IS NULL THEN 1 END) as poor
    FROM (
        SELECT code, COUNT(*) as cnt FROM klines GROUP BY code
    ) t
''', db.conn)

print(f'数据完整（≥200天）：{df_completeness["complete"].iloc[0]} 只')
print(f'数据较完整（100-199天）：{df_completeness["good"].iloc[0]} 只')
print(f'数据较少（50-99天）：{df_completeness["fair"].iloc[0]} 只')
print(f'数据很少（<50天）：{df_completeness["poor"].iloc[0]} 只')

print('\n' + '=' * 60)
print('2026年4月数据情况')
print('=' * 60)

df_april = pd.read_sql_query('''
    SELECT
        date,
        COUNT(DISTINCT code) as stock_count,
        COUNT(*) as record_count
    FROM klines
    WHERE date >= '2026-04-01'
    GROUP BY date
    ORDER BY date
''', db.conn)

if len(df_april) > 0:
    for _, row in df_april.iterrows():
        print(f'{row["date"]}: {row["stock_count"]} 只股票, {row["record_count"]} 条记录')
else:
    print('4月暂无数据')

db.close()
