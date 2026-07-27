"""
HRS 策略回测 LABUSDT - 基于本地数据
使用服务器下载的本地K线数据，运行HRS评分和形态检测
用法：
  python3 quick_backtest_labusdt.py            # 自动检测数据源
  python3 quick_backtest_labusdt.py --local    # 强制使用本地数据
"""
import sys
import os
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path

import yaml
import pandas as pd

# 添加项目根目录
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from strategies.hrs.pattern import PatternRecognizer
from strategies.hrs.scoring_engine import ScoringEngine


# ============================================================
# 本地数据加载
# ============================================================
def load_local_klines(csv_path: str) -> List[Dict]:
    """从本地CSV加载K线数据"""
    if not os.path.exists(csv_path):
        return []

    df = pd.read_csv(csv_path)
    if df.empty:
        return []

    klines = []
    for _, row in df.iterrows():
        dt = pd.to_datetime(row["open_time"])
        klines.append({
            "open_time": int(dt.timestamp() * 1000),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "quote_volume": float(row.get("quote_volume", 0)),
            "close_time": 0,
            "trades": 0,
        })
    return klines


def load_oi_cache(cache_path: str) -> Optional[Dict]:
    """加载OI和费率缓存"""
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_oi_cache(cache_path: str, data: Dict):
    """保存OI和费率缓存"""
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(data, f, indent=2)


# ============================================================
# 通过服务器K线服务拉取OI/费率（服务器有Binance API访问）
# ============================================================
def fetch_from_server(endpoint: str) -> Optional[Dict]:
    """通过服务器K线服务代理获取Binance数据"""
    import subprocess
    import base64

    server_ip = "43.156.242.184"
    ssh_key = "/Users/yl/vscode/inspection_automation/docs/only.pem"

    python_script = f"""
import json, ssl, urllib.request
ssl._create_default_https_context = ssl._create_unverified_context

endpoint = "{endpoint}"
url = f"https://fapi.binance.com{{endpoint}}"

try:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
        print("RESULT_START")
        print(json.dumps(data))
        print("RESULT_END")
except Exception as e:
    print(f"ERROR: {{e}}")
""".strip()

    encoded = base64.b64encode(python_script.encode()).decode()
    ssh_cmd = f"echo {encoded} | base64 -d | python3 -"
    ssh_command = f'ssh -i {ssh_key} -o StrictHostKeyChecking=no -o ConnectTimeout=10 {server_ip} "{ssh_cmd}"'

    try:
        result = subprocess.run(
            ssh_command, shell=True, capture_output=True, text=True, timeout=30
        )
        output = result.stdout

        if "RESULT_START" in output:
            json_start = output.index("RESULT_START") + len("RESULT_START")
            json_end = output.index("RESULT_END")
            return json.loads(output[json_start:json_end].strip())
    except Exception as e:
        print(f"  ⚠️ 服务器请求失败: {e}")

    return None


# ============================================================
# 合成4h K线
# ============================================================
def synthesize_4h_klines(klines_1h: List[Dict], interval_hours: int = 4) -> List[Dict]:
    """从1h K线合成4h K线"""
    if not klines_1h:
        return []

    klines_4h = []
    slot_klines = []

    for k in klines_1h:
        dt = datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc)
        slot_hour = (dt.hour // interval_hours) * interval_hours
        slot_key = f"{dt.strftime('%Y%m%d')}_{slot_hour:02d}"

        if not slot_klines or slot_klines[-1].get("slot_key") != slot_key:
            if slot_klines:
                klines_4h.append(_merge_slot(slot_klines))
            slot_klines = [k]
            slot_klines[-1]["slot_key"] = slot_key
        else:
            slot_klines.append(k)

    if slot_klines:
        klines_4h.append(_merge_slot(slot_klines))

    return klines_4h


def _merge_slot(slot_klines: List[Dict]) -> Dict:
    """合并同一4h槽位的K线"""
    return {
        "open_time": slot_klines[0]["open_time"],
        "open": slot_klines[0]["open"],
        "high": max(k["high"] for k in slot_klines),
        "low": min(k["low"] for k in slot_klines),
        "close": slot_klines[-1]["close"],
        "volume": sum(k["volume"] for k in slot_klines),
        "quote_volume": sum(k.get("quote_volume", 0) for k in slot_klines),
        "close_time": slot_klines[-1].get("close_time", 0),
    }


def calc_ema(data: List[float], period: int) -> float:
    """计算EMA"""
    if len(data) < period:
        return 0
    multiplier = 2.0 / (period + 1)
    ema = data[0]
    for price in data[1:]:
        ema = (price - ema) * multiplier + ema
    return ema


# ============================================================
# 主分析逻辑
# ============================================================
def analyze_labusdt(use_local: bool = False):
    """分析LABUSDT是否满足HRS策略条件"""
    print("=" * 70)
    print("  HRS 策略 - LABUSDT 回测分析")
    print("=" * 70)

    # 加载配置
    config_path = os.path.join(project_root, "strategies", "hrs", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 初始化组件
    pattern = PatternRecognizer(config)
    scoring = ScoringEngine(config)

    # 数据目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(script_dir, "data")
    csv_path = os.path.join(data_dir, "labusdt_1h.csv")
    cache_path = os.path.join(data_dir, "labusdt_oi_cache.json")

    # 加载K线数据
    print("\n📊 加载数据...")
    klines_1h = load_local_klines(csv_path)

    if not klines_1h:
        print(f"❌ 未找到本地数据: {csv_path}")
        print("   请先运行 download_labusdt.py 下载数据")
        return

    print(f"   本地数据: {len(klines_1h)} 根1h K线")
    if klines_1h:
        start_dt = datetime.fromtimestamp(klines_1h[0]["open_time"] / 1000, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(klines_1h[-1]["open_time"] / 1000, tz=timezone.utc)
        print(f"   时间范围: {start_dt} ~ {end_dt}")

    current_price = klines_1h[-1]["close"]
    print(f"   最新收盘价: {current_price:.6f} USDT")

    # 获取OI和资金费率（尝试从服务器获取，失败则使用缓存）
    oi_usd = 0
    funding_rate = 0.0
    volume_24h = 0

    print("\n📊 获取实时数据...")
    oi_data = fetch_from_server("/fapi/v1/openInterest?symbol=LABUSDT")
    funding_data = fetch_from_server("/fapi/v1/premiumIndex?symbol=LABUSDT")
    ticker_data = fetch_from_server("/fapi/v1/ticker/24hr?symbol=LABUSDT")

    if oi_data:
        oi_usd = float(oi_data.get("openInterest", 0))
        print(f"   OI (服务器): {oi_usd:,.0f} USDT")
    else:
        # 检查缓存
        cache = load_oi_cache(cache_path)
        if cache:
            oi_usd = cache.get("oi_usd", 0)
            funding_rate = cache.get("funding_rate", 0)
            volume_24h = cache.get("volume_24h", 0)
            print(f"   OI (缓存): {oi_usd:,.0f} USDT")
        else:
            print("   ⚠️ 无法获取OI数据，使用近似估算")
            # 用成交量近似估算 OI
            total_volume = sum(k.get("quote_volume", 0) for k in klines_1h[-24:])
            oi_usd = total_volume * 0.3  # 粗略估算
            print(f"   OI (估算): {oi_usd:,.0f} USDT")

    if funding_data:
        funding_rate = float(funding_data.get("lastFundingRate", 0))
        print(f"   资金费率 (服务器): {funding_rate:.6f}")
    elif not funding_rate:
        print(f"   资金费率: 使用默认值 0")

    if ticker_data:
        volume_24h = float(ticker_data.get("quoteVolume", 0))
        price_change = float(ticker_data.get("priceChangePercent", 0))
        print(f"   24h成交额: {volume_24h:,.0f} USDT")
        print(f"   24h涨跌: {price_change:.2f}%")
    elif volume_24h:
        print(f"   24h成交额 (缓存): {volume_24h:,.0f} USDT")
    else:
        volume_24h = sum(k.get("quote_volume", 0) for k in klines_1h[-24:])
        print(f"   24h成交额 (估算): {volume_24h:,.0f} USDT")

    # 缓存OI数据
    save_oi_cache(cache_path, {
        "oi_usd": oi_usd,
        "funding_rate": funding_rate,
        "volume_24h": volume_24h,
        "cached_at": datetime.now().isoformat(),
    })

    # 合成4h K线
    klines_4h = synthesize_4h_klines(klines_1h)
    print(f"   4h K线(合成): {len(klines_4h)} 根")

    # 计算EMA20(4h)
    close_prices_4h = [k["close"] for k in klines_4h]
    ema20_4h = calc_ema(close_prices_4h, 20)
    deviation_4h = 0
    if ema20_4h > 0:
        deviation_4h = (current_price - ema20_4h) / ema20_4h * 100
        print(f"   EMA20(4h): {ema20_4h:.6f}")
        print(f"   偏离EMA20: {deviation_4h:+.2f}%")

    # OI/市值比
    oi_market_cap_ratio = oi_usd / volume_24h if volume_24h > 0 else 0
    print(f"   OI/24h成交额: {oi_market_cap_ratio:.4f}")

    # 年化资金费率
    funding_config = config.get("funding_rate", {})
    settlements_per_day = funding_config.get("settlements_per_day", 3)
    days_per_year = funding_config.get("days_per_year", 365)
    annual_funding = funding_rate * settlements_per_day * days_per_year * 100
    print(f"   年化资金费率: {annual_funding:.4f}%")

    # ==============================================
    # 做空分析
    # ==============================================
    print("\n" + "=" * 70)
    print("  🔍 做空方向分析")
    print("=" * 70)

    pool_config = config.get("candidate_pool", {})
    short_config = pool_config.get("short", {})
    min_oi = short_config.get("min_oi_usd", 10_000_000)
    min_funding = short_config.get("funding_rate", 0) * 100
    ema20_dev = short_config.get("ema20_deviation", 0.08)

    print(f"\n  📋 候选池筛选条件:")
    oi_ok = oi_usd >= min_oi
    funding_ok = annual_funding >= min_funding
    ema20_ok = deviation_4h >= ema20_dev * 100 if ema20_4h > 0 else False

    print(f"     OI >= {min_oi:,.0f} USDT: {oi_usd:,.0f} {'✅' if oi_ok else '❌'}")
    print(f"     年化费率 >= {min_funding:.2f}%: {annual_funding:.4f}% {'✅' if funding_ok else '❌'}")
    print(f"     偏离EMA20 >= {ema20_dev * 100:.0f}%: {deviation_4h:+.2f}% {'✅' if ema20_ok else '❌'}")

    candidate_short = oi_ok and funding_ok and ema20_ok
    print(f"\n  📌 做空候选: {'✅ 通过' if candidate_short else '❌ 不满足'}")

    # 形态检测
    print(f"\n  📊 形态检测（做空，最近{pattern.window_size}根K线）:")
    recent_klines = klines_1h[-pattern.window_size:]
    short_pattern_result = pattern.detect_short_patterns(recent_klines)

    short_patterns_info = [
        ("三次冲顶", "three_tops"),
        ("长上影线", "long_upper_shadow"),
        ("放量滞涨", "volume_stagnation"),
    ]
    for name, key in short_patterns_info:
        detected, score_val = short_pattern_result.get(key, (False, 0))
        print(f"     {name}: {'✅ 检测到' if detected else '❌ 未检测'} (得分: {score_val})")

    # 评分
    print(f"\n  📊 综合评分（做空）:")
    try:
        short_score_result = scoring.score(
            symbol="LABUSDT",
            direction="short",
            oi_market_cap_ratio=oi_market_cap_ratio,
            patterns=short_pattern_result,
            funding_rate=funding_rate,
        )
        short_should_enter = scoring.should_entry(short_score_result)
        print(f"     合约数据评分: {short_score_result.contract_score:.2f} (权重: {scoring.contract_weight:.0%})")
        print(f"     技术面评分:   {short_score_result.technical_score:.2f} (权重: {scoring.technical_weight:.0%})")
        print(f"     情绪面评分:   {short_score_result.sentiment_score:.2f} (权重: {scoring.sentiment_weight:.0%})")
        print(f"     ─────────────────────────────")
        print(f"     总分: {short_score_result.total_score:.2f} (阈值: {scoring.entry_threshold})")
        print(f"     入场判断: {'✅ 建议入场' if short_should_enter else '❌ 不建议入场'}")
        if short_score_result.veto:
            print(f"     ❌ 否决: {short_score_result.veto_reason}")
    except Exception as e:
        print(f"     ❌ 评分失败: {e}")
        import traceback
        traceback.print_exc()
        short_score_result = None
        short_should_enter = False

    # ==============================================
    # 做多分析
    # ==============================================
    print("\n" + "=" * 70)
    print("  🔍 做多方向分析")
    print("=" * 70)

    long_config = pool_config.get("long", {})
    min_oi_long = long_config.get("min_oi_usd", 10_000_000)
    max_funding = long_config.get("funding_rate", 0) * 100
    long_ema20_deviation = long_config.get("long_ema20_deviation", -0.06)

    print(f"\n  📋 候选池筛选条件:")
    oi_ok_long = oi_usd >= min_oi_long
    funding_ok_long = annual_funding <= max_funding
    ema20_ok_long = deviation_4h <= long_ema20_deviation * 100 if ema20_4h > 0 else False

    print(f"     OI >= {min_oi_long:,.0f} USDT: {oi_usd:,.0f} {'✅' if oi_ok_long else '❌'}")
    print(f"     年化费率 <= {max_funding:.2f}%: {annual_funding:.4f}% {'✅' if funding_ok_long else '❌'}")
    print(f"     偏离EMA20 <= {long_ema20_deviation * 100:.0f}%: {deviation_4h:+.2f}% {'✅' if ema20_ok_long else '❌'}")

    candidate_long = oi_ok_long and funding_ok_long and ema20_ok_long
    print(f"\n  📌 做多候选: {'✅ 通过' if candidate_long else '❌ 不满足'}")

    # 形态检测（做多）
    print(f"\n  📊 形态检测（做多）:")
    long_pattern_result = pattern.detect_long_patterns(recent_klines)

    long_patterns_info = [
        ("三次探底", "three_bottoms"),
        ("长下影线", "long_lower_shadow"),
        ("放量止跌", "volume_reversal"),
    ]
    for name, key in long_patterns_info:
        detected, score_val = long_pattern_result.get(key, (False, 0))
        print(f"     {name}: {'✅ 检测到' if detected else '❌ 未检测'} (得分: {score_val})")

    # 评分（做多）
    print(f"\n  📊 综合评分（做多）:")
    try:
        long_score_result = scoring.score(
            symbol="LABUSDT",
            direction="long",
            oi_market_cap_ratio=oi_market_cap_ratio,
            patterns=long_pattern_result,
            funding_rate=funding_rate,
        )
        long_should_enter = scoring.should_entry(long_score_result)
        print(f"     合约数据评分: {long_score_result.contract_score:.2f} (权重: {scoring.contract_weight:.0%})")
        print(f"     技术面评分:   {long_score_result.technical_score:.2f} (权重: {scoring.technical_weight:.0%})")
        print(f"     情绪面评分:   {long_score_result.sentiment_score:.2f} (权重: {scoring.sentiment_weight:.0%})")
        print(f"     ─────────────────────────────")
        print(f"     总分: {long_score_result.total_score:.2f} (阈值: {scoring.entry_threshold})")
        print(f"     入场判断: {'✅ 建议入场' if long_should_enter else '❌ 不建议入场'}")
        if long_score_result.veto:
            print(f"     ❌ 否决: {long_score_result.veto_reason}")
    except Exception as e:
        print(f"     ❌ 评分失败: {e}")
        import traceback
        traceback.print_exc()
        long_score_result = None
        long_should_enter = False

    # ==============================================
    # 最终结论
    # ==============================================
    print("\n" + "=" * 70)
    print("  🎯 最终结论")
    print("=" * 70)

    entry_threshold = config.get("scoring", {}).get("entry_threshold", 6.5)

    print(f"\n  📉 做空方向:")
    if not candidate_short:
        print(f"     ❌ 候选池筛选不通过")
    elif short_score_result is None:
        print(f"     ❌ 评分失败")
    elif short_score_result.veto:
        print(f"     ❌ 被否决: {short_score_result.veto_reason}")
    elif not short_should_enter:
        print(f"     ❌ 综合评分不满足: {short_score_result.total_score:.2f}/{entry_threshold}")
    else:
        print(f"     ✅✅✅ 满足做空条件！总分: {short_score_result.total_score:.2f} >= {entry_threshold}")

    print(f"\n  📈 做多方向:")
    if not candidate_long:
        print(f"     ❌ 候选池筛选不通过")
    elif long_score_result is None:
        print(f"     ❌ 评分失败")
    elif long_score_result.veto:
        print(f"     ❌ 被否决: {long_score_result.veto_reason}")
    elif not long_should_enter:
        print(f"     ❌ 综合评分不满足: {long_score_result.total_score:.2f}/{entry_threshold}")
    else:
        print(f"     ✅✅✅ 满足做多条件！总分: {long_score_result.total_score:.2f} >= {entry_threshold}")

    print("\n" + "=" * 70)

    # K线回顾
    print(f"\n📈 最近5根1h K线 ({datetime.fromtimestamp(recent_klines[0]['open_time']/1000, tz=timezone.utc).strftime('%m-%d %H:%M')} ~):")
    print(f"  {'时间':<16} {'开':>10} {'高':>10} {'低':>10} {'收':>10} {'量':>12}")
    for k in recent_klines:
        dt = datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc)
        print(f"  {dt.strftime('%m-%d %H:%M'):<16} "
              f"{k['open']:>10.6f} {k['high']:>10.6f} {k['low']:>10.6f} "
              f"{k['close']:>10.6f} {k['volume']:>12.0f}")

    print(f"\n📈 最近5根4h K线(合成):")
    klines_4h_tail = klines_4h[-5:]
    if klines_4h_tail:
        print(f"  {'时间':<16} {'开':>10} {'高':>10} {'低':>10} {'收':>10} {'量':>12}")
        for k in klines_4h_tail:
            dt = datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc)
            print(f"  {dt.strftime('%m-%d %H:%M'):<16} "
                  f"{k['open']:>10.6f} {k['high']:>10.6f} {k['low']:>10.6f} "
                  f"{k['close']:>10.6f} {k['volume']:>12.0f}")

    print("\n✅ 分析完成")


if __name__ == "__main__":
    use_local = "--local" in sys.argv
    analyze_labusdt(use_local=use_local)