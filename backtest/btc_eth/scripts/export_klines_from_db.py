#!/usr/bin/env python3
"""从服务器 PostgreSQL 数据库导出 K 线数据到本地 CSV"""
import subprocess
import os
import sys

SERVER = "root@43.156.242.184"
CONTAINER = "common_service_postgres"
DB = "binance_data"
USER = "binance"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "TRXUSDT"]
INTERVALS = ["1h", "4h", "1d"]
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

os.makedirs(OUTPUT_DIR, exist_ok=True)

for symbol in SYMBOLS:
    for interval in INTERVALS:
        table = f"kline_{symbol.lower()}_{interval}"
        output_file = os.path.join(OUTPUT_DIR, f"{symbol.lower()}_{interval}.csv")
        
        print(f"导出 {table} -> {output_file} ...", end=" ", flush=True)
        
        # 导出近 3 个月数据（2026-03-23 ~ 2026-06-23）
        sql = f"""\\COPY (
            SELECT open_time, open_price, high_price, low_price, close_price, volume
            FROM {table}
            WHERE open_time >= '2026-03-23 00:00:00'
            ORDER BY open_time ASC
        ) TO STDOUT WITH CSV HEADER"""
        
        cmd = [
            "ssh", "-o", "ConnectTimeout=10", SERVER,
            f"docker exec {CONTAINER} psql -U {USER} -d {DB} -c \"{sql}\""
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and result.stdout.strip():
                with open(output_file, "w") as f:
                    f.write(result.stdout)
                lines = result.stdout.strip().count("\n")
                print(f"OK ({lines} 行)")
            else:
                print(f"FAILED: {result.stderr[:100]}")
        except subprocess.TimeoutExpired:
            print("TIMEOUT")
        except Exception as e:
            print(f"ERROR: {e}")

print("\n完成！")