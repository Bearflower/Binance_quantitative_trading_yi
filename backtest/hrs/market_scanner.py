#!/usr/bin/env python3
"""
全市场快速扫描模块
通过阿里云服务器 SSH 中转调用 Binance API，获取全市场数据并筛选候选币种。

用法：
  python3 market_scanner.py                     # 输出扫描结果到控制台
  python3 market_scanner.py --json              # 输出 JSON 格式结果
"""
import subprocess
import base64
import json
import sys
import os
import argparse
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from backtest.hrs.ssh_config import SERVER_IP, SERVER_USER, SSH_KEY


def _load_config() -> Dict:
    """加载 HRS 策略配置文件"""
    config_path = os.path.join(PROJECT_ROOT, "strategies", "hrs", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_exclude_set(config: Dict) -> set:
    """
    根据配置文件构建排除集合

    Args:
        config: HRS 配置字典

    Returns:
        需要排除的 symbol 集合（大写）
    """
    exclude_config = config.get("candidate_pool", {}).get("exclude", {})
    exclude_set = set()

    # 排除指定交易对
    for s in exclude_config.get("symbols", []):
        exclude_set.add(s.upper())

    # 排除稳定币（以稳定币为 base 的交易对）
    stablecoins = exclude_config.get("stablecoins", [])
    # 稳定币交易对如 USDCUSDT，但我们只关心以 USDT 为 quote 的，所以不需要排除 base 为稳定币的

    return exclude_set


def _build_exclude_keywords(config: Dict) -> List[str]:
    """
    根据配置文件构建排除关键词列表（仅用于杠杆代币等子串匹配）

    Args:
        config: HRS 配置字典

    Returns:
        排除关键词列表（如 BULL, BEAR, UP, DOWN）
    """
    exclude_config = config.get("candidate_pool", {}).get("exclude", {})
    keywords = []

    # 杠杆代币关键词（子串匹配）
    for kw in exclude_config.get("leverage_tokens", []):
        keywords.append(kw.upper())

    return keywords


def _build_stablecoin_set(config: Dict) -> set:
    """
    根据配置文件构建稳定币 base asset 集合

    用于检查以 USDT 为 quote 的交易对中，base asset 是否为稳定币。
    例如 USDCUSDT 的 base 是 USDC，应被排除。

    Args:
        config: HRS 配置字典

    Returns:
        稳定币 base asset 大写集合
    """
    exclude_config = config.get("candidate_pool", {}).get("exclude", {})
    return {s.upper() for s in exclude_config.get("stablecoins", [])}


def scan_market(config: Optional[Dict] = None) -> Dict:
    """
    通过服务器执行全市场扫描，筛选候选币种

    执行流程：
    1. SSH 到服务器，调用 Binance API 获取全市场数据
    2. 在服务器端完成初步筛选（减少数据传输量）
    3. 返回做空和做多候选列表

    Args:
        config: HRS 配置字典，为 None 时自动加载

    Returns:
        {
            "scan_time": "2026-06-12T10:00:00",
            "total_symbols": 300,
            "short_candidates": [{symbol, price, oi_usd, funding_rate, annual_funding, volume_24h, price_change_24h}],
            "long_candidates": [{...}],
            "market_stats": {"total_trading_pairs": 300, "short_passed": 5, "long_passed": 3},
            "errors": []
        }
    """
    if config is None:
        config = _load_config()

    pool_config = config.get("candidate_pool", {})

    # 从配置中提取筛选参数
    short_config = pool_config.get("short", {})
    long_config = pool_config.get("long", {})
    liquidity_config = pool_config.get("liquidity", {})

    # 做空筛选阈值
    short_min_oi = liquidity_config.get("min_oi_usd", 10_000_000)
    short_min_funding_annual = short_config.get("funding_rate_annual", 0.80)  # 0.80 = 80%
    short_min_price_change = short_config.get("price_change_24h", 0.12)  # 0.12 = 12%

    # 做多筛选阈值
    long_min_oi = liquidity_config.get("min_oi_usd", 10_000_000)
    long_max_funding_annual = long_config.get("funding_rate_annual", -0.20)  # -0.20 = -20%
    long_max_price_change = long_config.get("price_change_24h", -0.10)  # -0.10 = -10%

    # 流动性门槛
    min_volume_24h = liquidity_config.get("min_volume_24h", 50_000_000)

    # 排除列表
    exclude_set = _build_exclude_set(config)
    exclude_keywords = _build_exclude_keywords(config)
    stablecoin_set = _build_stablecoin_set(config)

    # 资金费率计算参数
    funding_config = config.get("funding_rate", {})
    settlements_per_day = funding_config.get("settlements_per_day", 3)
    days_per_year = funding_config.get("days_per_year", 365)

    print("=" * 70)
    print("  全市场扫描 - HRS 候选池筛选")
    print("=" * 70)
    print(f"  扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  做空阈值: OI≥{short_min_oi/1e6:.0f}M, 年化费率≥{short_min_funding_annual*100:.0f}%, 24h涨≥{short_min_price_change*100:.0f}%")
    print(f"  做多阈值: OI≥{long_min_oi/1e6:.0f}M, 年化费率≤{long_max_funding_annual*100:.0f}%, 24h跌≤{long_max_price_change*100:.0f}%")
    print(f"  流动性门槛: 24h成交额≥{min_volume_24h/1e6:.0f}M")
    print(f"  排除交易对: {len(exclude_set)} 个")
    print(f"  排除稳定币(base): {sorted(stablecoin_set)}")
    print(f"  排除关键词: {exclude_keywords}")
    print()

    # 构建服务器端 Python 脚本
    server_script = _build_server_script(
        short_min_oi=short_min_oi,
        short_min_funding_annual=short_min_funding_annual,
        short_min_price_change=short_min_price_change,
        long_min_oi=long_min_oi,
        long_max_funding_annual=long_max_funding_annual,
        long_max_price_change=long_max_price_change,
        min_volume_24h=min_volume_24h,
        exclude_set=list(exclude_set),
        exclude_keywords=exclude_keywords,
        stablecoin_set=list(stablecoin_set),
        settlements_per_day=settlements_per_day,
        days_per_year=days_per_year,
    )

    # 通过 SSH 执行服务器脚本
    print("正在通过 SSH 连接服务器执行扫描...")
    result = _execute_ssh_script(server_script, timeout=300)

    if result is None:
        return {
            "scan_time": datetime.now().isoformat(),
            "total_symbols": 0,
            "short_candidates": [],
            "long_candidates": [],
            "market_stats": {"total_trading_pairs": 0, "short_passed": 0, "long_passed": 0},
            "errors": ["SSH 执行失败或返回数据异常"],
        }

    # 解析结果
    try:
        scan_result = json.loads(result)
    except json.JSONDecodeError as e:
        print(f"❌ JSON 解析失败: {e}")
        print(f"   原始输出(前500字符): {result[:500]}")
        return {
            "scan_time": datetime.now().isoformat(),
            "total_symbols": 0,
            "short_candidates": [],
            "long_candidates": [],
            "market_stats": {"total_trading_pairs": 0, "short_passed": 0, "long_passed": 0},
            "errors": [f"JSON 解析失败: {str(e)}"],
        }

    # 打印扫描摘要
    stats = scan_result.get("market_stats", {})
    total = stats.get("total_trading_pairs", 0)
    short_count = len(scan_result.get("short_candidates", []))
    long_count = len(scan_result.get("long_candidates", []))

    print(f"\n📊 扫描结果摘要:")
    print(f"   全市场 USDT 永续合约: {total} 个")
    print(f"   做空候选: {short_count} 个")
    print(f"   做多候选: {long_count} 个")

    if scan_result.get("errors"):
        print(f"   ⚠️ 扫描过程中出现 {len(scan_result['errors'])} 个错误")

    # 打印做空候选
    short_candidates = scan_result.get("short_candidates", [])
    if short_candidates:
        print(f"\n📉 做空候选列表:")
        print(f"  {'交易对':<14} {'价格':>10} {'OI(USDT)':>12} {'年化费率':>10} {'24h成交额':>12} {'24h涨跌':>8}")
        for c in short_candidates:
            print(f"  {c['symbol']:<14} {c['price']:>10.6f} {c['oi_usd']:>12,.0f} "
                  f"{c['annual_funding']:>9.2f}% {c['volume_24h']:>12,.0f} {c['price_change_24h']:>7.2f}%")

    # 打印做多候选
    long_candidates = scan_result.get("long_candidates", [])
    if long_candidates:
        print(f"\n📈 做多候选列表:")
        print(f"  {'交易对':<14} {'价格':>10} {'OI(USDT)':>12} {'年化费率':>10} {'24h成交额':>12} {'24h涨跌':>8}")
        for c in long_candidates:
            print(f"  {c['symbol']:<14} {c['price']:>10.6f} {c['oi_usd']:>12,.0f} "
                  f"{c['annual_funding']:>9.2f}% {c['volume_24h']:>12,.0f} {c['price_change_24h']:>7.2f}%")

    print(f"\n✅ 扫描完成")

    # 添加本地时间戳
    scan_result["scan_time"] = datetime.now().isoformat()
    scan_result["total_symbols"] = total

    return scan_result


def _build_server_script(
    short_min_oi: float,
    short_min_funding_annual: float,
    short_min_price_change: float,
    long_min_oi: float,
    long_max_funding_annual: float,
    long_max_price_change: float,
    min_volume_24h: float,
    exclude_set: List[str],
    exclude_keywords: List[str],
    stablecoin_set: List[str],
    settlements_per_day: int,
    days_per_year: int,
) -> str:
    """
    构建在服务器端执行的 Python 扫描脚本

    将筛选参数嵌入脚本中，服务器端完成数据获取和筛选，只返回结果。
    """
    # 将 Python 列表转为 JSON 字符串嵌入脚本
    exclude_set_json = json.dumps(exclude_set)
    exclude_keywords_json = json.dumps(exclude_keywords)
    stablecoin_set_json = json.dumps(stablecoin_set)

    script = f"""
import json
import ssl
import urllib.request
import sys
import time
from datetime import datetime

ssl._create_default_https_context = ssl._create_unverified_context

# ============================================================
# 筛选参数（从本地配置传入）
# ============================================================
SHORT_MIN_OI = {short_min_oi}
SHORT_MIN_FUNDING_ANNUAL = {short_min_funding_annual}
SHORT_MIN_PRICE_CHANGE = {short_min_price_change}
LONG_MIN_OI = {long_min_oi}
LONG_MAX_FUNDING_ANNUAL = {long_max_funding_annual}
LONG_MAX_PRICE_CHANGE = {long_max_price_change}
MIN_VOLUME_24H = {min_volume_24h}
EXCLUDE_SET = set({exclude_set_json})
EXCLUDE_KEYWORDS = {exclude_keywords_json}
STABLECOIN_SET = set({stablecoin_set_json})
SETTLEMENTS_PER_DAY = {settlements_per_day}
DAYS_PER_YEAR = {days_per_year}

# API 基础地址
BASE_URL = "https://fapi.binance.com"

def http_get(endpoint, timeout=30):
    \"\"\"发送 HTTP GET 请求，返回解析后的 JSON\"\"\"
    url = f"{{BASE_URL}}{{endpoint}}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"ERROR: 请求 {{endpoint}} 失败: {{e}}", file=sys.stderr)
        return None

def should_exclude(symbol):
    \"\"\"检查交易对是否应该被排除\"\"\"
    # 精确匹配排除
    if symbol in EXCLUDE_SET:
        return True
    # 杠杆代币关键词（子串匹配，如 XXXBULLUSDT）
    for kw in EXCLUDE_KEYWORDS:
        if kw in symbol:
            return True
    # 稳定币检查：去掉 USDT 后缀后，检查 base asset 是否为稳定币
    if symbol.endswith("USDT"):
        base = symbol[:-4]  # 去掉尾部 "USDT"
        if base in STABLECOIN_SET:
            return True
    return False

def calc_annual_funding(funding_rate):
    \"\"\"计算年化资金费率（百分比）\"\"\"
    return funding_rate * SETTLEMENTS_PER_DAY * DAYS_PER_YEAR * 100

# ============================================================
# 阶段1：获取交易对列表
# ============================================================
print("STEP1: 获取交易对列表...", file=sys.stderr)
exchange_info = http_get("/fapi/v1/exchangeInfo")
if exchange_info is None:
    result = {{"error": "无法获取交易对信息", "short_candidates": [], "long_candidates": [], "market_stats": {{}}}}
    print("RESULT_START")
    print(json.dumps(result))
    print("RESULT_END")
    sys.exit(1)

usdt_perpetuals = []
for s in exchange_info.get("symbols", []):
    if (s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"):
        symbol = s["symbol"]
        if not should_exclude(symbol):
            usdt_perpetuals.append(symbol)

total_symbols = len(usdt_perpetuals)
print(f"STEP1: 共 {{total_symbols}} 个 USDT 永续合约（已排除指定交易对、稳定币、杠杆代币）", file=sys.stderr)

if not usdt_perpetuals:
    result = {{"error": "无符合条件的交易对", "short_candidates": [], "long_candidates": [], "market_stats": {{"total_trading_pairs": 0}}}}
    print("RESULT_START")
    print(json.dumps(result))
    print("RESULT_END")
    sys.exit(0)

# ============================================================
# 阶段2：批量获取 ticker/24hr 和 premiumIndex
# ============================================================
print("STEP2: 获取全市场 ticker/24hr...", file=sys.stderr)
ticker_data = http_get("/fapi/v1/ticker/24hr")
if ticker_data is None:
    result = {{"error": "无法获取 ticker 数据", "short_candidates": [], "long_candidates": [], "market_stats": {{}}}}
    print("RESULT_START")
    print(json.dumps(result))
    print("RESULT_END")
    sys.exit(1)

print("STEP2: 获取全市场 premiumIndex...", file=sys.stderr)
premium_data = http_get("/fapi/v1/premiumIndex")
if premium_data is None:
    result = {{"error": "无法获取 premiumIndex 数据", "short_candidates": [], "long_candidates": [], "market_stats": {{}}}}
    print("RESULT_START")
    print(json.dumps(result))
    print("RESULT_END")
    sys.exit(1)

# 构建 symbol -> ticker 映射
ticker_map = {{}}
for t in ticker_data:
    symbol = t.get("symbol", "")
    if symbol in usdt_perpetuals:
        ticker_map[symbol] = t

# 构建 symbol -> funding_rate 映射
funding_map = {{}}
for p in premium_data:
    symbol = p.get("symbol", "")
    if symbol in usdt_perpetuals:
        try:
            funding_map[symbol] = float(p.get("lastFundingRate", 0))
        except (ValueError, TypeError):
            funding_map[symbol] = 0.0

print(f"STEP2: ticker 覆盖 {{len(ticker_map)}} 个, funding 覆盖 {{len(funding_map)}} 个", file=sys.stderr)

# ============================================================
# 阶段3：初筛（基于 ticker 数据，不依赖 OI）
# ============================================================
print("STEP3: 初筛（基于24h成交额和价格）...", file=sys.stderr)

pre_filtered = []
for symbol in usdt_perpetuals:
    t = ticker_map.get(symbol)
    if t is None:
        continue

    try:
        price = float(t.get("lastPrice", 0))
        volume_24h = float(t.get("quoteVolume", 0))
        price_change = float(t.get("priceChangePercent", 0))
    except (ValueError, TypeError):
        continue

    # 流动性门槛：价格 > 0 且 24h 成交额达标
    if price <= 0 or volume_24h < MIN_VOLUME_24H:
        continue

    pre_filtered.append({{
        "symbol": symbol,
        "price": price,
        "volume_24h": volume_24h,
        "price_change_24h": price_change,
        "funding_rate": funding_map.get(symbol, 0.0),
    }})

print(f"STEP3: 初筛后剩余 {{len(pre_filtered)}} 个交易对", file=sys.stderr)

# ============================================================
# 阶段4：逐币种获取 OI 并完成最终筛选
# ============================================================
print(f"STEP4: 逐币种获取 OI 数据（共 {{len(pre_filtered)}} 个）...", file=sys.stderr)

short_candidates = []
long_candidates = []
errors = []
fetched_count = 0

for i, item in enumerate(pre_filtered):
    symbol = item["symbol"]

    # 获取 OI
    oi_data = http_get(f"/fapi/v1/openInterest?symbol={{symbol}}", timeout=15)
    if oi_data is None:
        errors.append(f"获取 {{symbol}} OI 失败")
        continue

    try:
        oi_usd = float(oi_data.get("openInterest", 0))
    except (ValueError, TypeError):
        continue

    fetched_count += 1
    if fetched_count % 50 == 0:
        print(f"STEP4: 已获取 {{fetched_count}}/{{len(pre_filtered)}} 个 OI...", file=sys.stderr)

    # 计算年化费率
    funding_rate = item["funding_rate"]
    annual_funding = calc_annual_funding(funding_rate)

    # 构建候选条目
    candidate = {{
        "symbol": symbol,
        "price": item["price"],
        "oi_usd": oi_usd,
        "funding_rate": funding_rate,
        "annual_funding": round(annual_funding, 4),
        "volume_24h": item["volume_24h"],
        "price_change_24h": item["price_change_24h"],
    }}

    # 做空方向筛选
    if (oi_usd >= SHORT_MIN_OI
            and annual_funding >= SHORT_MIN_FUNDING_ANNUAL * 100
            and item["price_change_24h"] >= SHORT_MIN_PRICE_CHANGE * 100):
        short_candidates.append(candidate)

    # 做多方向筛选
    if (oi_usd >= LONG_MIN_OI
            and annual_funding <= LONG_MAX_FUNDING_ANNUAL * 100
            and item["price_change_24h"] <= LONG_MAX_PRICE_CHANGE * 100):
        long_candidates.append(candidate)

print(f"STEP4: OI获取完成, 做空候选={{len(short_candidates)}}, 做多候选={{len(long_candidates)}}", file=sys.stderr)

# 按 OI 降序排序
short_candidates.sort(key=lambda x: x["oi_usd"], reverse=True)
long_candidates.sort(key=lambda x: x["oi_usd"], reverse=True)

# ============================================================
# 输出结果
# ============================================================
result = {{
    "short_candidates": short_candidates,
    "long_candidates": long_candidates,
    "market_stats": {{
        "total_trading_pairs": total_symbols,
        "pre_filtered": len(pre_filtered),
        "short_passed": len(short_candidates),
        "long_passed": len(long_candidates),
        "oi_fetched": fetched_count,
    }},
    "errors": errors,
}}

print("RESULT_START")
print(json.dumps(result))
print("RESULT_END")
""".strip()

    return script


def _execute_ssh_script(script: str, timeout: int = 300) -> Optional[str]:
    """
    通过 SSH 在服务器上执行 Python 脚本

    Args:
        script: Python 脚本内容
        timeout: 超时时间（秒）

    Returns:
        RESULT_START 和 RESULT_END 之间的 JSON 字符串，失败返回 None
    """
    encoded = base64.b64encode(script.encode()).decode()
    ssh_cmd = f"echo {encoded} | base64 -d | python3 -"
    ssh_command = (
        f'ssh -i {SSH_KEY} -o StrictHostKeyChecking=no '
        f'-o ConnectTimeout=15 {SERVER_USER}@{SERVER_IP} "{ssh_cmd}"'
    )

    try:
        proc = subprocess.run(
            ssh_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        # stderr 包含服务器端 Python 脚本的进度输出
        if proc.stderr:
            stderr_lines = proc.stderr.strip().split("\n")
            for line in stderr_lines:
                # 过滤掉 SSH 连接信息，只打印脚本的进度输出
                if line.startswith("STEP") or line.startswith("ERROR:"):
                    print(f"  [服务器] {line}")

        if proc.returncode != 0:
            print(f"❌ SSH 执行失败 (returncode={proc.returncode})")
            if proc.stderr:
                print(f"   stderr: {proc.stderr[:500]}")
            return None

        output = proc.stdout

        if "RESULT_START" not in output or "RESULT_END" not in output:
            print(f"❌ 输出格式异常，未找到 RESULT_START/RESULT_END 标记")
            print(f"   输出(前500字符): {output[:500]}")
            return None

        json_start = output.index("RESULT_START") + len("RESULT_START")
        json_end = output.index("RESULT_END")
        json_str = output[json_start:json_end].strip()

        return json_str

    except subprocess.TimeoutExpired:
        print(f"❌ SSH 执行超时 ({timeout}s)")
        return None
    except Exception as e:
        print(f"❌ SSH 执行异常: {e}")
        traceback.print_exc()
        return None


def get_candidate_symbols(scan_result: Dict) -> List[str]:
    """
    从扫描结果中提取所有候选交易对（去重）

    Args:
        scan_result: scan_market() 返回的结果字典

    Returns:
        去重后的交易对列表
    """
    symbols = set()
    for c in scan_result.get("short_candidates", []):
        symbols.add(c["symbol"])
    for c in scan_result.get("long_candidates", []):
        symbols.add(c["symbol"])
    return sorted(list(symbols))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="全市场快速扫描")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    args = parser.parse_args()

    result = scan_market()

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))