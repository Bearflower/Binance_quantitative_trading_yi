#!/bin/bash
# 服务器端使用 Baostock 获取全市场历史数据
# 直接存储到 PostgreSQL 数据库

set -e

echo "============================================="
echo "服务器端 Baostock 历史数据获取脚本"
echo "============================================="

# 创建日志目录
mkdir -p /root/stockfilter/logs

# 在容器中执行 Python 脚本
docker exec stockfilter-app python3 << 'PYTHON_SCRIPT'

import baostock as bs
import pandas as pd
import psycopg2
from datetime import datetime
import time
import sys

# 数据库配置
DB_CONFIG = {
    'host': 'postgres-db',
    'port': 5432,
    'database': 'stockfilter',
    'user': 'stockfilter_user',
    'password': 'Stock@2024'
}

# 日期范围
START_DATE = '2025-08-01'
END_DATE = datetime.now().strftime('%Y-%m-%d')

def generate_stock_codes():
    """生成全市场股票代码列表"""
    stock_list = []
    
    # 沪市 A 股（600000-609999, 688000-688999）
    for i in range(600000, 610000):
        code = f"{i:06d}"
        stock_list.append({'code': code, 'symbol': f"sh.{code}"})
    
    # 深市主板（000001-002999）
    for i in range(1, 3000):
        code = f"{i:06d}"
        stock_list.append({'code': code, 'symbol': f"sz.{code}"})
    
    return stock_list


def fetch_kline_baostock(symbol, start_date, end_date):
    """使用 Baostock 获取 K 线数据"""
    try:
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
            return df
        return None
    except Exception as e:
        print(f"获取 {symbol} 失败：{e}")
        return None


def save_to_postgres(df, symbol, code):
    """保存数据到 PostgreSQL"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        for _, row in df.iterrows():
            cur.execute("""
                INSERT INTO kline_data (symbol, code, date, open, high, low, close, volume, amount, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (symbol, date) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    amount = EXCLUDED.amount,
                    updated_at = NOW()
            """, (
                symbol,
                code,
                row['date'],
                float(row['open']) if row['open'] else None,
                float(row['high']) if row['high'] else None,
                float(row['low']) if row['low'] else None,
                float(row['close']) if row['close'] else None,
                int(row['volume']) if row['volume'] else None,
                float(row['amount']) if row['amount'] else None
            ))
        
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"保存 {symbol} 到数据库失败：{e}")
        return False


def main():
    print("=" * 60)
    print("开始获取全市场历史数据")
    print(f"日期范围：{START_DATE} 到 {END_DATE}")
    print("=" * 60)
    
    # 登录 Baostock
    bs.login()
    print("✅ Baostock 登录成功")
    
    # 生成股票代码列表
    stock_list = generate_stock_codes()
    print(f"📊 共 {len(stock_list)} 只股票待获取")
    
    success_count = 0
    fail_count = 0
    total_stocks = len(stock_list)
    
    for i, stock in enumerate(stock_list, 1):
        symbol = stock['symbol']
        code = stock['code']
        
        # 获取 K 线数据
        df = fetch_kline_baostock(symbol, START_DATE, END_DATE)
        
        if df is not None and len(df) > 0:
            # 保存到数据库
            if save_to_postgres(df, symbol, code):
                success_count += 1
                print(f"[{i}/{total_stocks}] ✅ {symbol} - {len(df)}条")
            else:
                fail_count += 1
                print(f"[{i}/{total_stocks}] ❌ {symbol} - 保存失败")
        else:
            print(f"[{i}/{total_stocks}] ⚠️  {symbol} - 无数据")
        
        # 每 100 只打印进度
        if i % 100 == 0:
            print(f"\n进度：{i}/{total_stocks} ({i/total_stocks*100:.1f}%)")
            print(f"成功：{success_count} 失败：{fail_count}\n")
    
    # 登出
    bs.logout()
    
    print("\n" + "=" * 60)
    print("数据获取完成")
    print(f"成功：{success_count}/{total_stocks}")
    print(f"失败：{fail_count}/{total_stocks}")
    print("=" * 60)


if __name__ == '__main__':
    main()

PYTHON_SCRIPT

echo ""
echo "============================================="
echo "脚本执行完成"
echo "============================================="
