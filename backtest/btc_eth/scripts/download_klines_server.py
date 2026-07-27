#!/usr/bin/env python3
"""在服务器上运行，从币安API下载3个月K线数据"""
import requests
import csv
import os
import time
from datetime import datetime, timedelta

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "TRXUSDT"]
INTERVALS = ["1h", "4h", "1d"]
OUTPUT_DIR = "/tmp/klines_backtest"

# 3个月：2026-03-23 ~ 2026-06-23
END_DATE = datetime(2026, 6, 23, 23, 59, 59)
START_DATE = END_DATE - timedelta(days=93)  # 多一些缓冲

os.makedirs(OUTPUT_DIR, exist_ok=True)

INTERVAL_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

for symbol in SYMBOLS:
    for interval in INTERVALS:
        output_file = os.path.join(OUTPUT_DIR, f"{symbol.lower()}_{interval}.csv")
        
        # 检查是否已存在
        if os.path.exists(output_file):
            print(f"跳过 {symbol} {interval}（已存在）")
            continue
        
        print(f"下载 {symbol} {interval} ...", end=" ", flush=True)
        
        start_ms = int(START_DATE.timestamp() * 1000)
        end_ms = int(END_DATE.timestamp() * 1000)
        
        all_rows = []
        current_start = start_ms
        
        while current_start < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": current_start,
                "limit": 1000,
            }
            
            try:
                resp = requests.get(
                    "https://fapi.binance.com/fapi/v1/klines",
                    params=params,
                    timeout=30
                )
                
                if resp.status_code != 200:
                    print(f"HTTP {resp.status_code}")
                    break
                
                data = resp.json()
                if not data:
                    break
                
                all_rows.extend(data)
                current_start = data[-1][0] + INTERVAL_MS[interval]
                
                # 速率限制
                time.sleep(0.15)
                
            except Exception as e:
                print(f"错误: {e}")
                break
        
        if all_rows:
            with open(output_file, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["open_time", "open_price", "high_price", "low_price", "close_price", "volume"])
                for row in all_rows:
                    writer.writerow([
                        row[0], row[1], row[2], row[3], row[4], row[5]
                    ])
            print(f"OK ({len(all_rows)} 行)")
        else:
            print("FAILED (0 行)")
        
        time.sleep(0.5)

print("\n全部下载完成！")
print(f"文件保存在: {OUTPUT_DIR}")