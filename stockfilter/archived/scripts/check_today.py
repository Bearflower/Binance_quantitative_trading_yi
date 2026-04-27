from data.database import DatabaseManager
import pandas as pd

db = DatabaseManager()

print("检查最新数据日期...")
df = pd.read_sql("""
    SELECT date, COUNT(DISTINCT code) as cnt 
    FROM klines 
    WHERE date >= '2026-04-07' 
    GROUP BY date 
    ORDER BY date
""", db.conn)

print(df.to_string())

# 检查今天是否有数据
df_today = pd.read_sql("""
    SELECT COUNT(DISTINCT code) as cnt 
    FROM klines 
    WHERE date = '2026-04-09'
""", db.conn)

print(f"\n今天 (2026-04-09) 的股票数量：{df_today['cnt'].iloc[0]}")

db.close()
