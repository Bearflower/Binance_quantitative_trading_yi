#!/usr/bin/env python3
"""回补 SOLUSDT 1d K线数据到数据库"""
import json
import urllib.request
from datetime import datetime

url = "https://fapi.binance.com/fapi/v1/klines?symbol=SOLUSDT&interval=1d&limit=7"
with urllib.request.urlopen(url) as resp:
    data = json.loads(resp.read())

print(f"从币安API获取到 {len(data)} 条1d数据")

for d in data:
    open_time = datetime.fromtimestamp(d[0]/1000).strftime('%Y-%m-%d %H:%M:%S')
    close_time = datetime.fromtimestamp(d[6]/1000).strftime('%Y-%m-%d %H:%M:%S')
    
    sql = f"""INSERT INTO kline_solusdt_1d 
        (open_time, open_price, high_price, low_price, close_price, volume, close_time, quote_volume, trade_count, taker_buy_volume, taker_buy_quote_volume)
        VALUES ('{open_time}', {d[1]}, {d[2]}, {d[3]}, {d[4]}, {d[5]}, '{close_time}', {d[7]}, {d[8]}, {d[9]}, {d[10]})
        ON CONFLICT (open_time) DO NOTHING;"""
    print(sql)