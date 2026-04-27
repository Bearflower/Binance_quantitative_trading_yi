#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查数据库中的 K 线数据"""

from data.database import DatabaseManager
import pandas as pd

db = DatabaseManager()

# 检查 K 线数据统计
df = pd.read_sql_query("""
    SELECT 
        MIN(date) as earliest,
        MAX(date) as latest,
        COUNT(*) as total_records,
        COUNT(DISTINCT code) as stocks
    FROM klines
""", db.conn)

print('K 线数据统计:')
print(df)
print()

# 检查 4 月以来每日数据量
df2 = pd.read_sql_query("""
    SELECT date, COUNT(*) as stock_count 
    FROM klines 
    WHERE date >= '2026-04-01' 
    GROUP BY date 
    ORDER BY date DESC
    LIMIT 20
""", db.conn)

print('4 月以来每日数据量 (最近 20 天):')
print(df2)
print()

# 检查是否有今天的数据
today = pd.Timestamp.now().normalize()
df3 = pd.read_sql_query("""
    SELECT COUNT(*) as today_count
    FROM klines 
    WHERE date = %s
""", db.conn, params=[today])

print(f'今天 ({today.date()}) 的数据量：{df3["today_count"].iloc[0]}')

db.close()
