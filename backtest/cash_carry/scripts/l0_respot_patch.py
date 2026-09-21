#!/usr/bin/env python3
"""[在服务器运行] 补拉现货 K 线（修复现货单请求 limit 上限 bug）

背景：
    初版脚本对现货 /api/v3/klines 使用 limit=1500，但现货上限仅 1000，
    导致现货只拉到 1 个请求（约 42 天），永续/funding 已完整。
    本脚本按正确 page_limit=1000 重拉所有币的现货 1h K线，覆盖 spot_1h.csv。

用法：
    python3 l0_respot_patch.py <OUTPUT_DIR> [--limit N]

自包含、无项目依赖（仅 requests），复用 l0_download_on_server 的函数。
"""
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from l0_download_on_server import (  # noqa: E402
    OUTPUT_DIR, SYM_DIR, LOOKBACK_MS, SPOT_BASE,
    KLINE_INTERVAL, fetch_json, log, ms_to_utc, now_ms, request_tick,
)

# 现货单请求上限 1000
SPOT_PAGE_LIMIT = 1000


def fetch_spot_klines(symbol: str) -> bool:
    """按 1000/页 翻页拉取现货 1h K 线到 spot_1h.csv"""
    end_ms = now_ms()
    start_ms = end_ms - LOOKBACK_MS
    cur = start_ms
    rows = []
    while cur < end_ms:
        request_tick()
        data = fetch_json(SPOT_BASE, "/api/v3/klines",
                          params={"symbol": symbol, "interval": KLINE_INTERVAL,
                                  "startTime": cur, "limit": SPOT_PAGE_LIMIT})
        if not data:
            log(f"  [失败] {symbol} 现货 K 线请求")
            return False
        for k in data:
            rows.append([ms_to_utc(k[0]), k[1], k[2], k[3], k[4], k[5]])
            if k[0] >= cur:
                cur = k[0] + 1
        if len(data) < SPOT_PAGE_LIMIT:
            break
    # 去重并写盘（按 open_time 升序）
    seen = set()
    uniq = []
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        uniq.append(r)
    uniq.sort(key=lambda r: r[0])
    out = os.path.join(SYM_DIR, symbol, "spot_1h.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume"])
        w.writerows(uniq)
    log(f"  [完成] {symbol} 现货 {len(uniq)} 根")
    return True


def main() -> int:
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    # 收集已下载目录中的币（symbols.json 含全量清单，优先用清单顺序）
    manifest = os.path.join(OUTPUT_DIR, "symbols.json")
    if os.path.exists(manifest):
        import json
        with open(manifest, encoding="utf-8") as f:
            universe = json.load(f)
            symbols = [s["symbol"] for s in universe]
    else:
        symbols = [d for d in os.listdir(SYM_DIR)
                   if os.path.isdir(os.path.join(SYM_DIR, d))]
        symbols.sort()

    if limit:
        symbols = symbols[:limit]

    log("=" * 70)
    log(f"补拉现货（page_limit=1000，修复 42 天截断）: {len(symbols)} 币")
    log("=" * 70)

    ok = 0
    for i, sym in enumerate(symbols, 1):
        if fetch_spot_klines(sym):
            ok += 1
        if i % 30 == 0:
            log(f"  进度: {i}/{len(symbols)}  成功: {ok}")

    log("=" * 70)
    log(f"补拉完成: {ok}/{len(symbols)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())