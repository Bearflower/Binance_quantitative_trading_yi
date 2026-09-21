#!/usr/bin/env python3
"""[在服务器运行] 从币安 API 拉取受限池日线 K 线

用途：
    本地 Mac 无法直连币安 API（主网 IP 受限），因此本脚本由本地编排器
    （download_universe_data.py）用 scp 上传到服务器后执行，利用服务器
    走 Binance API 拉取目标数据，再下载回本地做回测。
    （符合项目规则：仅下载数据到本地回测，不在服务器跑回测）

自包含、无项目依赖（仅 requests），保证服务器可直接运行。

数据输出：
    <OUTPUT_DIR>/universe.json          受限池元数据（含 onboard_date / 24h成交额）
    <OUTPUT_DIR>/klines/{symbol}_1d.csv 每币种日线（7列带表头，open_time为UTC字符串）

用法：
    python3 download_klines_on_server.py <OUTPUT_DIR>  默认 /tmp/momentum_klines
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ==================== 配置区 ====================
OUTPUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/momentum_klines"
KLINES_DIR = os.path.join(OUTPUT_DIR, "klines")
UNIVERSE_PATH = os.path.join(OUTPUT_DIR, "universe.json")

FAPI_BASE = "https://fapi.binance.com"
KLINE_INTERVAL = "1d"
KLINE_LIMIT = 1000          # 单请求上限，覆盖约 2.7 年
LOOKBACK_DAYS = 1000        # 拉取起始（往前推的天数）
DAILY_MS = 24 * 60 * 60 * 1000

# 受限池构建参数
MIN_LISTING_DAYS = 90
MIN_DAILY_VOLUME_USDT = 10_000_000

# 受限池排除清单（与现有策略 btc_eth / hrs / new_coin 保持一致）
MAINSTREAM_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT",
]
STABLECOIN_SYMBOLS = [
    "USDTUSDT", "USDCUSDT", "BUSDUSDT", "DAIUSDT", "TUSDUSDT",
    "USDPUSDT", "FDUSDUSDT",
]
LEVERAGE_TOKEN_PATTERNS = ["BULL", "BEAR", "UP", "DOWN"]
EXCLUDED_SYMBOLS = [
    "SOXLUSDT", "HFTUSDT", "SOXSUSDT", "CRCLUSDT", "TSLAUSDT", "AAPLUSDT",
]


def log(msg: str) -> None:
    """统一日志"""
    print(msg, flush=True)


def ms_to_utc_str(ts_ms: int) -> str:
    """毫秒时间戳转 UTC 字符串（YYYY-MM-DD HH:MM:SS）"""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def fetch_json(endpoint: str, params: Optional[Dict] = None, retries: int = 3) -> Any:
    """GET 请求并解析 JSON，失败按退避重试

    Args:
        endpoint: fapi 端点（如 /fapi/v1/exchangeInfo）
        params: 查询参数
        retries: 重试次数

    Returns:
        解析后的 JSON；最终失败返回 None
    """
    url = f"{FAPI_BASE}{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            log(f"  [警告] {endpoint} HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:  # noqa: BLE001
            log(f"  [警告] {endpoint} 请求异常: {str(e)[:120]}")
        time.sleep(1.0 * (2 ** attempt))
    log(f"  [错误] {endpoint} 重试 {retries} 次后仍失败")
    return None


def is_excluded(symbol: str) -> bool:
    """是否命中受限池排除规则"""
    if symbol in MAINSTREAM_SYMBOLS:
        return True
    if symbol in STABLECOIN_SYMBOLS:
        return True
    if symbol in EXCLUDED_SYMBOLS:
        return True
    upper = symbol.upper()
    return any(p in upper for p in LEVERAGE_TOKEN_PATTERNS)


def build_universe() -> List[Dict]:
    """从 exchangeInfo + 24h ticker 构建受限池

    Returns:
        受限池列表，每项含 symbol / onboard_date_ms / quote_volume_24h
    """
    info = fetch_json("/fapi/v1/exchangeInfo")
    if not info:
        log("  [错误] 获取 exchangeInfo 失败，无法构建受限池")
        return []
    ticker = fetch_json("/fapi/v1/ticker/24hr")
    volumes: Dict[str, float] = {}
    if isinstance(ticker, list):
        for t in ticker:
            try:
                volumes[t.get("symbol", "")] = float(t.get("quoteVolume", 0) or 0)
            except (TypeError, ValueError):
                volumes[t.get("symbol", "")] = 0.0

    now_ms = int(time.time() * 1000)
    universe: List[Dict] = []
    for item in info.get("symbols", []):
        symbol = item.get("symbol", "")
        if item.get("quoteAsset") != "USDT":
            continue
        if item.get("contractType") != "PERPETUAL":
            continue
        if item.get("status") != "TRADING":
            continue
        if is_excluded(symbol):
            log(f"  排除 {symbol}: 受限池清单")
            continue
        listing_days = (now_ms - item.get("onboardDate", 0)) / DAILY_MS
        if listing_days < MIN_LISTING_DAYS:
            log(f"  排除 {symbol}: 上线仅 {listing_days:.0f} 天 < {MIN_LISTING_DAYS} 天")
            continue
        vol = volumes.get(symbol, 0.0)
        if vol < MIN_DAILY_VOLUME_USDT:
            log(f"  排除 {symbol}: 24h成交额 {vol / 1e6:.1f}M < "
                f"{MIN_DAILY_VOLUME_USDT / 1e6:.0f}M")
            continue
        universe.append({
            "symbol": symbol,
            "onboard_date_ms": item.get("onboardDate", 0),
            "quote_volume_24h": vol,
        })
    return universe


def save_klines(symbol: str, klines: List[list]) -> int:
    """保存单币种日线 CSV（7列带表头，open_time 为 UTC 字符串）"""
    filename = f"{symbol.lower()}_{KLINE_INTERVAL}.csv"
    filepath = os.path.join(KLINES_DIR, filename)
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
    """服务器端主流程"""
    log("=" * 62)
    log("受限池日线数据下载（服务器端）")
    log(f"输出目录: {OUTPUT_DIR}")
    log(f"限制: 上线≥{MIN_LISTING_DAYS}天 | 24h成交额≥{MIN_DAILY_VOLUME_USDT / 1e6:.0f}M USDT")
    log("=" * 62)

    os.makedirs(KLINES_DIR, exist_ok=True)

    universe = build_universe()
    if not universe:
        log("未构建出受限池，退出")
        return 1
    log(f"受限池共 {len(universe)} 个币种")

    with open(UNIVERSE_PATH, "w", encoding="utf-8") as f:
        json.dump(universe, f, ensure_ascii=False, indent=2)

    start_ms = int((time.time() - LOOKBACK_DAYS * 86400) * 1000)
    ok = fail = empty = total = 0
    for idx, item in enumerate(universe, start=1):
        symbol = item["symbol"]
        klines = fetch_json(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": KLINE_INTERVAL,
             "startTime": start_ms, "limit": KLINE_LIMIT},
        )
        if not isinstance(klines, list) or len(klines) == 0:
            fail += 1
            log(f"  [{idx}/{len(universe)}] {symbol}: 无数据/失败")
            time.sleep(0.1)
            continue
        if len(klines) < MIN_LISTING_DAYS:
            empty += 1
            log(f"  [{idx}/{len(universe)}] {symbol}: 数据不足({len(klines)}根)")
            time.sleep(0.1)
            continue
        count = save_klines(symbol, klines)
        ok += 1
        total += count
        if idx % 25 == 0 or idx == len(universe):
            log(f"  进度 [{idx}/{len(universe)}]")
        time.sleep(0.1)

    log("=" * 62)
    log(f"下载汇总: 成功 {ok} | 数据不足 {empty} | 失败 {fail} | K线总行数 {total}")
    log(f"数据目录: {KLINES_DIR}")
    log("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    import requests  # noqa: F401  确保 requests 注入成功
    sys.exit(main())