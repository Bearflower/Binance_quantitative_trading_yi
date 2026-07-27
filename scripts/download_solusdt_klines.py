#!/usr/bin/env python3
"""下载 SOLUSDT 多时间周期K线数据，补全到与其他币种相同的时间范围"""
import asyncio
import aiohttp
import pandas as pd
import os
from datetime import datetime, timedelta

BASE_URL = "https://fapi.binance.com"
SYMBOL = "SOLUSDT"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../backtest/btc_eth/data")
INTERVALS = ['1h', '4h', '1d']

async def download_klines(session, symbol, interval, start_ms, end_ms):
    all_klines = []
    current_start = start_ms
    interval_ms_map = {'1h': 3600000, '4h': 14400000, '1d': 86400000}
    ms_per_request = interval_ms_map[interval] * 1000

    while current_start < end_ms:
        current_end = min(current_start + ms_per_request, end_ms)
        params = {"symbol": symbol, "interval": interval, "startTime": current_start, "endTime": current_end, "limit": 1000}
        try:
            async with session.get(f"{BASE_URL}/fapi/v1/klines", params=params) as resp:
                if resp.status != 200:
                    print(f"  请求失败: {resp.status}")
                    break
                data = await resp.json()
                if not data:
                    break
                all_klines.extend(data)
                current_start = data[-1][0] + interval_ms_map[interval]
                print(f"  已下载 {len(data)} 根, 累计 {len(all_klines)} 根")
                await asyncio.sleep(0.1)
        except Exception as e:
            print(f"  下载异常: {e}")
            break
    return all_klines

async def main():
    end_time = datetime.now()
    start_time = end_time - timedelta(days=60)
    start_ms = int(start_time.timestamp() * 1000)
    end_ms = int(end_time.timestamp() * 1000)

    print(f"下载 SOLUSDT K线数据: {start_time} ~ {end_time}")
    print(f"输出目录: {OUTPUT_DIR}")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        for interval in INTERVALS:
            print(f"\n{'='*50}")
            print(f"下载 {interval} 数据...")
            klines = await download_klines(session, SYMBOL, interval, start_ms, end_ms)
            if not klines:
                print(f"  无数据，跳过")
                continue

            df = pd.DataFrame(klines, columns=['open_time','open_price','high_price','low_price','close_price','volume','close_time','quote_volume','trades','taker_buy_base','taker_buy_quote','ignore'])
            df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
            df = df[['open_time','open_price','high_price','low_price','close_price','volume']]
            output_file = os.path.join(OUTPUT_DIR, f"solusdt_{interval}.csv")
            df.to_csv(output_file, index=False)
            print(f"  已保存: {output_file} ({len(df)} 行, {df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]})")

    print("\n完成!")

if __name__ == "__main__":
    asyncio.run(main())