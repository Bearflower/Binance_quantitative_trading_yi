#!/usr/bin/env python3
"""[在服务器运行] 拉取 L0 cash-and-carry 所需全量数据

用途：
    为验证"相对价值 L0（现货 vs 永续，吃资金费率）"是否立项，按全量 C 方案
    从币安拉取每个永续合约的：永续 1h K线、现货 1h K线（同名 USDT 对）、
    funding 历史。仅下载行情，本地再回测（守"禁止服务器回测"规则）。

关键约束（针对服务器 3.6G 内存 / 1.3G 可用）：
    - 逐币流式处理，任一时刻只持有一个币的数据在内存，峰值 <100MB
    - 断点续拉：每个币拉完三份数据后写 .done 标记，重跑自动跳过已完成
    - 限速 + 429 退避重试，防被币安限频封 IP

用法：
    python3 l0_download_on_server.py <OUTPUT_DIR> [--limit N] [--test]

自包含、无项目依赖（仅 requests），保证服务器可直接运行。

输出结构：
    <OUTPUT_DIR>/symbols.json         有效配对清单（现货∩永续同时存在）
    <OUTPUT_DIR>/sym/{SYM}/perp_1h.csv   永续 1h K线（6列，open_time为UTC字符串）
    <OUTPUT_DIR>/sym/{SYM}/spot_1h.csv   现货 1h K线（6列）
    <OUTPUT_DIR>/sym/{SYM}/funding.csv   资金费率历史（time, rate）
    <OUTPUT_DIR>/sym/{SYM}/.done         完成标记
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

# ==================== 配置区 ====================
OUTPUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/cashcarry"
# 支持 --limit 小样本验证 / --test 仅拉 3 币
LIMIT = None
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])
TEST_MODE = "--test" in sys.argv

SYM_DIR = os.path.join(OUTPUT_DIR, "sym")
MANIFEST_PATH = os.path.join(OUTPUT_DIR, "symbols.json")

FAPI_BASE = "https://fapi.binance.com"
SPOT_BASE = "https://api.binance.com"

KLINE_INTERVAL = "1h"
KLINE_LIMIT = 1500            # 1h 单请求上限≈62天，需要按 startTime 翻页
LOOKBACK_MS = 730 * 24 * 3600 * 1000  # 拉取近 730 天（2 年）
FUNDING_LIMIT = 1000          # funding 单请求上限，8h 一次，需翻页拉满 2 年(~2190条)
FUNDING_START = 730 * 24 * 3600 * 1000  # funding 回溯窗口（2 年）

# 请求节奏（毫秒）：永续/现货均限速，防限频
REQUEST_INTERVAL_S = 0.15
# 429 退避参数
MAX_RETRIES = 5
BACKOFF_BASE = 1.0

# 冷热分离：优先热门币（大市值永续），限频下先保证主流覆盖
MAINSTREAM_PREFIX = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX",
                     "LINK", "TRX", "LTC", "DOT", "MATIC", "XLM", "SUI"]


def log(msg: str) -> None:
    print(msg, flush=True)


def ms_to_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def now_ms() -> int:
    return int(time.time() * 1000)


def fetch_json(base: str, endpoint: str, params: Optional[Dict] = None,
               retries: int = MAX_RETRIES) -> Optional[Any]:
    """GET 请求并解析 JSON，429/5xx 指数退避重试

    Args:
        base: API base（fapi 或现货）
        endpoint: 端点路径
        params: 查询参数
        retries: 重试次数

    Returns:
        解析后的 JSON；最终失败返回 None
    """
    url = f"{base}{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = BACKOFF_BASE * (2 ** attempt) + (r.headers.get("Retry-After", "0") or "0")
                try:
                    wait = max(wait, float(r.headers.get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
                log(f"  [限频429] {endpoint} 退避 {wait:.1f}s")
                time.sleep(wait)
                continue
            log(f"  [警告] {endpoint} HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:  # noqa: BLE001
            log(f"  [警告] {endpoint} 异常: {str(e)[:100]}")
        time.sleep(BACKOFF_BASE * (2 ** attempt))
    log(f"  [错误] {endpoint} 重试 {retries} 次后仍失败")
    return None


def request_tick() -> None:
    """速率控制：每次请求前 sleep，保持稳定节奏"""
    time.sleep(REQUEST_INTERVAL_S)


def build_universe(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """构建有效配对清单：永续存在 且 同名 USDT 现货存在

    Args:
        limit: 若给定位数，只取前 limit 个（用于小样本验证）

    Returns:
        有效配对列表，每项含 symbol / onboard / perp_symbol / spot_symbol
    """
    request_tick()
    fi = fetch_json(FAPI_BASE, "/fapi/v1/exchangeInfo")
    if not fi:
        log("❌ 无法获取永续 exchangeInfo")
        return []
    request_tick()
    si = fetch_json(SPOT_BASE, "/api/v3/exchangeInfo")
    if not si:
        log("❌ 无法获取现货 exchangeInfo")
        return []

    perp_syms = {
        s["symbol"]
        for s in fi["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
    }
    spot_syms = {
        s["symbol"]
        for s in si["symbols"]
        if s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
    }
    # onboarding 时间（永续）
    onboard = {s["symbol"]: s.get("onboardDate") for s in fi["symbols"]}

    valid = []
    for symbol in sorted(perp_syms):
        if symbol not in spot_syms:
            continue  # 必须现货∩永续同时存在，否则无法 cash-and-carry 对冲
        valid.append({
            "symbol": symbol,
            "perp_symbol": symbol,
            "spot_symbol": symbol,
            "onboard_date": onboard.get(symbol),
        })

    # 热门主流币优先（限频下先覆盖流动性好的），其余按字序
    def sort_key(sym: str) -> tuple:
        is_main = any(sym.startswith(p) for p in MAINSTREAM_PREFIX)
        return (0 if is_main else 1, sym)

    valid.sort(key=lambda x: sort_key(x["symbol"]))
    if limit:
        valid = valid[:limit]
    return valid


def fetch_klines(base: str, endpoint: str, symbol: str, start_ms: int,
                 end_ms: int, out_path: str, page_limit: int = KLINE_LIMIT) -> bool:
    """分页拉取 1h K线到 CSV（6列：open_time,open,high,low,close,volume）

    Args:
        base: API base
        endpoint: klines 端点路径（永续 /fapi/v1/klines，现货 /api/v3/klines）
        symbol: 交易对
        start_ms: 起始时间戳
        end_ms: 结束时间戳
        out_path: 输出 CSV 路径
        page_limit: 单请求 limit。注意现货 /api/v3/klines 上限仅 1000，
                    永续 /fapi/v1/klines 上限 1500，必须按端传入否则提前断页。

    Returns:
        是否成功
    """
    cur = start_ms
    rows = []
    while cur < end_ms:
        request_tick()
        data = fetch_json(base, endpoint,
                          params={"symbol": symbol, "interval": KLINE_INTERVAL,
                                  "startTime": cur, "limit": page_limit})
        if not data:
            return False
        for k in data:
            rows.append([ms_to_utc(k[0]), k[1], k[2], k[3], k[4], k[5]])
            if k[0] >= cur:
                cur = k[0] + 1
        # 返回不足整页才结束翻页；页面数由 page_limit 决定（现货1000/永续1500）
        if len(data) < page_limit:
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
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume"])
        w.writerows(uniq)
    return True


def fetch_funding(symbol: str, out_path: str) -> bool:
    """拉取资金费率历史到 CSV（2列：time, rate），按 startTime 翻页拉满 730 天

    Args:
        symbol: 永续交易对
        out_path: 输出 CSV 路径

    Returns:
        是否成功
    """
    end_ms = now_ms()
    start_ms = end_ms - FUNDING_START
    cur = start_ms
    rates = set()
    while cur < end_ms:
        request_tick()
        data = fetch_json(FAPI_BASE, "/fapi/v1/fundingRate",
                          params={"symbol": symbol, "limit": FUNDING_LIMIT,
                                  "startTime": cur})
        if not data:
            return False
        for r in data:
            rates.add((r["fundingTime"], r["fundingRate"]))
            if r["fundingTime"] >= cur:
                cur = r["fundingTime"] + 1
        if len(data) < FUNDING_LIMIT:
            break
    # 按时间升序写盘（去重）
    rows = sorted(rates, key=lambda x: x[0])
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "rate"])
        for ts, rt in rows:
            w.writerow([ms_to_utc(ts), rt])
    return True


def download_symbol(symbol: str) -> bool:
    """下载单个币的三份数据（逐币，内存峰值小）"""
    sym_out = os.path.join(SYM_DIR, symbol)
    done_path = os.path.join(sym_out, ".done")
    if os.path.exists(done_path):
        log(f"  [跳过] {symbol} 已完成")
        return True
    os.makedirs(sym_out, exist_ok=True)

    end_ms = now_ms()
    start_ms = end_ms - LOOKBACK_MS

    ok = fetch_klines(FAPI_BASE, "/fapi/v1/klines", symbol, start_ms, end_ms,
                      os.path.join(sym_out, "perp_1h.csv"), page_limit=KLINE_LIMIT)
    if not ok:
        log(f"  [失败] {symbol} 永续 K线")
        return False
    # 现货 /api/v3/klines 单请求上限仅 1000，必须单独指定 page_limit
    ok = fetch_klines(SPOT_BASE, "/api/v3/klines", symbol, start_ms, end_ms,
                      os.path.join(sym_out, "spot_1h.csv"), page_limit=1000)
    if not ok:
        log(f"  [失败] {symbol} 现货 K线")
        return False
    ok = fetch_funding(symbol, os.path.join(sym_out, "funding.csv"))
    if not ok:
        log(f"  [失败] {symbol} funding")
        return False

    with open(done_path, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())
    return True


def main() -> int:
    os.makedirs(SYM_DIR, exist_ok=True)
    log("=" * 70)
    log("L0 cash-and-carry 全量数据拉取")
    log(f"输出目录: {OUTPUT_DIR}  测试模式: {TEST_MODE}  限额: {LIMIT}")
    log("=" * 70)

    universe = build_universe(LIMIT)
    if not universe:
        log("❌ 有效配对为空，终止")
        return 1
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(universe, f, indent=2)
    log(f"有效配对（现货∩永续）: {len(universe)} 个，已写 {MANIFEST_PATH}")

    if TEST_MODE and len(universe) > 0:
        universe = universe[: min(3, len(universe))]

    ok_count = 0
    skip_count = 0
    for i, sym in enumerate(universe, 1):
        symbol = sym["symbol"]
        # 断点：已 .done 的跳过
        if os.path.exists(os.path.join(SYM_DIR, symbol, ".done")):
            skip_count += 1
            continue
        if download_symbol(symbol):
            ok_count += 1
        else:
            log(f"  [!] {symbol} 拉取失败，跳过（可重跑续拉）")
        if i % 20 == 0:
            log(f"  进度: {i}/{len(universe)}  成功累计: {ok_count}")

    log("=" * 70)
    log(f"完成。新增成功: {ok_count}  跳过(断点): {skip_count}  总计: {len(universe)}")
    if ok_count < len(universe):
        log("存在未完成项，可重跑本脚本断点续拉。")
    log(f"打包指令: cd {OUTPUT_DIR} && tar -czf .tar.gz sym symbols.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())