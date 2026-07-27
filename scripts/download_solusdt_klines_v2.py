#!/usr/bin/env python3
"""下载 SOLUSDT 多时间周期K线数据"""
import urllib.request
import json
import pandas as pd
import os
from datetime import datetime, timedelta
import time

BASE_URL = "https://fapi.binance.com"
SYMBOL = "SOLUSDT"
OUTPUT_DIR = os.path.expanduser("/Users/yl/vscode/Binance_quantitative_trading/backtest/btc_eth/data")
INTERVALS = {'1h': 3600000, '4h': 14400000, '1d': 86400000}

def download_klines(interval, start_ms, end_ms):
    """下载指定时间范围的K线数据"""
    all_klines = []
    current_start = start_ms
    ms_per_candle = INTERVALS[interval]
    max_per_request = 1000

    while current_start < end_ms:
        current_end = min(current_start + ms_per_candle * max_per_request, end_ms)
        url = f"{BASE_URL}/fapi/v1/klines?symbol={SYMBOL}&interval={interval}&startTime={current_start}&endTime={current_end}&limit={max_per_request}"
        
        try:
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            
            if not data:
                break
                
            all_klines.extend(data)
            current_start = data[-1][0] + ms_per_candle
            print(f"  [{interval}] 已下载 {len(data)} 根, 累计 {len(all_klines)} 根, 最后时间 {datetime.fromtimestamp(data[-1][0]/1000)}")
            time.sleep(0.05)
            
        except Exception as e:
            print(f"  [{interval}] 下载异常: {e}")
            break
    
    return all_klines

def main():
    end_time = datetime.now()
    start_time = end_time - timedelta(days=60)
    start_ms = int(start_time.timestamp() * 1000)
    end_ms = int(end_time.timestamp() * 1000)

    print(f"下载 {SYMBOL} K线数据: {start_time} ~ {end_time}")
    print(f"输出目录: {OUTPUT_DIR}")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for interval, ms in INTERVALS.items():
        print(f"\n{'='*50}")
        print(f"下载 {interval} 数据...")
        
        klines = download_klines(interval, start_ms, end_ms)
        
        if not klines:
            print(f"  无数据")
            continue

        df = pd.DataFrame(klines, columns=[
            'open_time','open_price','high_price','low_price','close_price',
            'volume','close_time','quote_volume','trades',
            'taker_buy_base','taker_buy_quote','ignore'
        ])
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df = df[['open_time','open_price','high_price','low_price','close_price','volume']]
        
        output_file = os.path.join(OUTPUT_DIR, f"solusdt_{interval}.csv")
        df.to_csv(output_file, index=False)
        print(f"  已保存: {output_file}")
        print(f"  共 {len(df)} 行, {df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]}")

    print("\n完成!")

if __name__ == "__main__":
    main()