from data.database import DatabaseManager
import pandas as pd

db = DatabaseManager()

print("=" * 80)
print("数据同步情况全面检查")
print("=" * 80)

# 1. 总体统计
print("\n【1. 总体统计】")
df_total = pd.read_sql("SELECT COUNT(*) as total_records, COUNT(DISTINCT code) as stocks_with_data FROM klines", db.conn)
print(f"K 线总记录数：{df_total['total_records'].iloc[0]:,} 条")
print(f"有数据的股票：{df_total['stocks_with_data'].iloc[0]} 只")

# 2. 读取固定股票列表
stocks_df = pd.read_csv('/app/main_board_stocks.csv', dtype={'code': str})
total_stocks = len(stocks_df)
print(f"\n沪深主板股票总数：{total_stocks} 只")

# 3. 检查每只股票的数据
print("\n【2. 数据完整性统计】")
df_stats = pd.read_sql("SELECT code, COUNT(*) as cnt FROM klines GROUP BY code", db.conn)
merged = stocks_df.merge(df_stats, on='code', how='left')
merged['cnt'] = merged['cnt'].fillna(0).astype(int)

with_data = len(merged[merged['cnt'] > 0])
without_data = len(merged[merged['cnt'] == 0])
complete_200 = len(merged[merged['cnt'] >= 200])
complete_100 = len(merged[(merged['cnt'] >= 100) & (merged['cnt'] < 200)])
less_100 = len(merged[merged['cnt'] < 100])

print(f"✅ 有数据：{with_data} 只 ({with_data/total_stocks*100:.1f}%)")
print(f"❌ 无数据：{without_data} 只 ({without_data/total_stocks*100:.1f}%)")
print(f"  - 数据完整 (≥200 天): {complete_200} 只")
print(f"  - 数据较完整 (100-199 天): {complete_100} 只")
print(f"  - 数据较少 (<100 天): {less_100} 只")

# 4. 4 月份数据
print("\n【3. 2026 年 4 月数据】")
df_april = pd.read_sql("SELECT date, COUNT(DISTINCT code) as cnt FROM klines WHERE date >= '2026-04-01' GROUP BY date ORDER BY date", db.conn)
for _, row in df_april.iterrows():
    print(f"{row['date']}: {row['cnt']} 只股票")

# 5. 昨天和今天数据
print("\n【4. 最新数据检查】")
df_yesterday = pd.read_sql("SELECT COUNT(DISTINCT code) as cnt FROM klines WHERE date = '2026-04-08'", db.conn)
df_today = pd.read_sql("SELECT COUNT(DISTINCT code) as cnt FROM klines WHERE date = '2026-04-09'", db.conn)
print(f"昨天 (2026-04-08): {df_yesterday['cnt'].iloc[0]} 只股票")
print(f"今天 (2026-04-09): {df_today['cnt'].iloc[0]} 只股票")

# 6. 无数据的股票
print("\n【5. 无数据的股票 (前 20 只)】")
no_data = merged[merged['cnt'] == 0].head(20)
for _, row in no_data.iterrows():
    print(f"  {row['code']} - {row['name']}")

print(f"\n无数据股票总数：{without_data} 只")

db.close()
print("\n" + "=" * 80)
