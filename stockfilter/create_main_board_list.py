#!/usr/bin/env python3
"""创建主板股票列表"""
import os
os.environ['DB_HOST']='10.3.0.12'
os.environ['DB_PORT']='5432'
os.environ['DB_NAME']='stockfilter'
os.environ['DB_USER']='stockfilter_user'
os.environ['DB_PASSWORD']='Stock@2024'
os.environ['DB_SCHEMA']='schema_stockfilter'

from data.database import DatabaseManager
import pandas as pd

db = DatabaseManager()
cursor = db.conn.cursor()
cursor.execute("""SELECT code, name, symbol FROM stocks 
WHERE code LIKE %s OR code LIKE %s OR code LIKE %s OR code LIKE %s 
OR code LIKE %s OR code LIKE %s OR code LIKE %s""", 
('600%', '601%', '603%', '605%', '000%', '001%', '002%'))
rows = cursor.fetchall()
columns = ['code', 'name', 'symbol']
df = pd.DataFrame(rows, columns=columns)
df.to_csv('/app/main_board_stocks.csv', index=False)
print(f'已创建主板股票列表：{len(df)} 只')
db.close()
