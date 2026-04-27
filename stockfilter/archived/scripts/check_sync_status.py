#!/usr/bin/env python3
import os
os.environ['DB_HOST']='10.3.0.12'
os.environ['DB_PORT']='5432'
os.environ['DB_NAME']='stockfilter'
os.environ['DB_USER']='stockfilter_user'
os.environ['DB_PASSWORD']='Stock@2024'

from data.database import DatabaseManager
import pandas as pd
from datetime import datetime, timedelta

db = DatabaseManager()
stocks = db.get_stock_list()

df = pd.read_sql_query('SELECT COUNT(DISTINCT code) as cnt FROM klines', db.conn)
has_kline = df['cnt'].iloc[0]

df = pd.read_sql_query('SELECT COUNT(*) as cnt FROM klines', db.conn)
total = df['cnt'].iloc[0]

today = datetime.now().date()
yesterday = (today - timedelta(days=1)).strftime('%Y-%m-%d')
df = pd.read_sql_query(f"SELECT COUNT(*) as cnt FROM klines WHERE date = '{yesterday}'", db.conn)
yesterday_count = df['cnt'].iloc[0]

print(f'股票总数：{len(stocks)} 只')
print(f'有 K 线数据：{has_kline} 只')
print(f'K 线总量：{total:,} 条')
print(f'还缺：{len(stocks)-has_kline} 只')
print(f'昨天数据：{yesterday_count} 条')

db.close()
print('')
if has_kline > 5000:
    print('✅ 数据基本完整！')
else:
    print('🔄 数据同步中...')
