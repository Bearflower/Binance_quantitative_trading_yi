#!/usr/bin/env python3
"""[在服务器运行] 拉取 BTC/ETH 日线作为全局 regime 锚数据

用途：
    受限池（momentum 回测）已排除 BTC/ETH 等主流币，但市场行情状态机
    需要以 BTC/ETH 为大盘锚来判定全局 regime。本脚本在服务器上拉取
    BTCUSDT / ETHUSDT 日线，下载回本地后与受限池日线合并使用。

    本地 Mac 无法直连币安 API，故由本地编排器 scp 上传本脚本到服务器执行。

自包含、无项目依赖（仅 requests）。

数据输出：
    <OUTPUT_DIR>/btcusdt_1d.csv
    <OUTPUT_DIR>/ethusdt_1d.csv
    格式：open_time,open_price,high_price,low_price,close_price,volume,quote_volume

用法：
    python3 download_anchor_on_server.py <OUTPUT_DIR>  默认 /tmp/regime_anchor
"""
import csv
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, List, Optional

import requests

OUTPUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/regime_anchor"
FAPI_BASE = "https://fapi.binance.com"
ANCHOR_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
KLINE_INTERVAL = "1d"
KLINE_LIMIT = 1000
LOOKBACK_DAYS = 1000


def log(msg: str) -> None:
    print(msg, flush=True)


def ms_to_utc_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def fetch_klines(symbol: str, start_ms: int, retries: int = 3) -> List[List[Any]]:
    """拉取单币种日线，失败退避重试"""
    url = f"{FAPI_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": KLINE_INTERVAL,
              "startTime": start_ms, "limit": KLINE_LIMIT}
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
            log(f"  [警告] {symbol} HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            log(f"  [警告] {symbol} 异常 {str(e)[:100]}")
        time.sleep(1.0 * (2 ** attempt))
    return []


def save_csv(symbol: str, klines: List[List[Any]]) -> int:
    filename = f"{symbol.lower()}_{KLINE_INTERVAL}.csv"
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "open_time", "open_price", "high_price", "low_price",
            "close_price", "volume", "quote_volume",
        ])
        for k in klines:
            writer.writerow([
                ms_to_utc_str(int(k[0])), k[1], k[2], k[3], k[4], k[5], k[7],
            ])
    return len(klines)


def main() -> int:
    log("=" * 62)
    log("全局 regime 锚数据下载（BTC/ETH 日线，服务器端）")
    log(f"输出目录: {OUTPUT_DIR}")
    log("=" * 62)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    start_ms = int((time.time() - LOOKBACK_DAYS * 86400) * 1000)
    for symbol in ANCHOR_SYMBOLS:
        klines = fetch_klines(symbol, start_ms)
        if not klines:
            log(f"  [失败] {symbol} 无数据")
            continue
        count = save_csv(symbol, klines)
        log(f"  [成功] {symbol} 下载 {count} 根日线")
        time.sleep(0.2)
    log("=" * 62)
    log("锚数据下载完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())