"""
HRS 策略回测 - 通用版，支持任意交易对
基于服务器下载的本地K线数据，运行HRS评分和形态检测
用法：
  python3 quick_backtest.py --symbol SAHARAUSDT
  python3 quick_backtest.py --symbol LABUSDT
"""
import sys
import os
import json
import argparse
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional

import yaml
import pandas as pd

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from backtest.hrs.ssh_config import SERVER_IP, SERVER_USER, SSH_KEY

from strategies.hrs.pattern import PatternRecognizer
from strategies.hrs.scoring_engine import ScoringEngine


def _get_display_threshold(score_result, scoring: ScoringEngine) -> float:
    """根据入场模式返回对应的显示阈值"""
    if score_result is None:
        return scoring.entry_threshold
    if score_result.entry_mode == "emm":
        return scoring.emm_entry_threshold
    elif score_result.entry_mode == "semi_emm":
        return scoring.semi_emm_entry_threshold
    return scoring.entry_threshold


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


def load_cache(cache_path: str) -> Optional[Dict]:
    """加载OI和费率缓存"""
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_cache(cache_path: str, data: Dict):
    """保存OI和费率缓存"""
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(data, f, indent=2)


# ============================================================
# 通过服务器拉取OI/费率
# ============================================================
def fetch_from_server(endpoint: str) -> Optional[Dict]:
    """通过服务器代理获取Binance数据"""
    import subprocess
    import base64

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
    ssh_command = f'ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10 {SERVER_USER}@{SERVER_IP} "{ssh_cmd}"'

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
def analyze_symbol(symbol: str) -> Optional[Dict]:
    """
    分析指定交易对是否满足HRS策略条件

    Args:
        symbol: 交易对，如 LABUSDT

    Returns:
        分析结果字典，包含所有维度的评分和筛选结果；数据加载失败返回 None
    """
    print("=" * 70)
    print(f"  HRS 策略 - {symbol} 回测分析")
    print("=" * 70)

    config_path = os.path.join(project_root, "strategies", "hrs", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    pattern = PatternRecognizer(config)
    scoring = ScoringEngine(config)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(script_dir, "data")
    symbol_lower = symbol.lower()
    csv_path = os.path.join(data_dir, f"{symbol_lower}_1h.csv")
    cache_path = os.path.join(data_dir, f"{symbol_lower}_oi_cache.json")

    print("\n📊 加载数据...")
    klines_1h = load_local_klines(csv_path)

    if not klines_1h:
        print(f"❌ 未找到本地数据: {csv_path}")
        print(f"   请先运行: python3 backtest/hrs/download_klines.py --symbol {symbol}")
        return None

    print(f"   本地数据: {len(klines_1h)} 根1h K线")
    if klines_1h:
        start_dt = datetime.fromtimestamp(klines_1h[0]["open_time"] / 1000, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(klines_1h[-1]["open_time"] / 1000, tz=timezone.utc)
        print(f"   时间范围: {start_dt} ~ {end_dt}")

    current_price = klines_1h[-1]["close"]
    print(f"   最新收盘价: {current_price:.6f} USDT")

    # 获取OI和资金费率
    oi_usd = 0
    funding_rate = 0.0
    volume_24h = 0

    print("\n📊 获取实时数据...")
    oi_data = fetch_from_server(f"/fapi/v1/openInterest?symbol={symbol}")
    funding_data = fetch_from_server(f"/fapi/v1/premiumIndex?symbol={symbol}")
    ticker_data = fetch_from_server(f"/fapi/v1/ticker/24hr?symbol={symbol}")

    if oi_data:
        oi_usd = float(oi_data.get("openInterest", 0))
        print(f"   OI (服务器): {oi_usd:,.0f} USDT")
    else:
        cache = load_cache(cache_path)
        if cache:
            oi_usd = cache.get("oi_usd", 0)
            funding_rate = cache.get("funding_rate", 0)
            volume_24h = cache.get("volume_24h", 0)
            print(f"   OI (缓存): {oi_usd:,.0f} USDT")
        else:
            print("   ⚠️ 无法获取OI数据，使用近似估算")
            total_volume = sum(k.get("quote_volume", 0) for k in klines_1h[-24:])
            oi_estimation_ratio = config.get("candidate_pool", {}).get("oi_estimation_ratio", 0.3)
            oi_usd = total_volume * oi_estimation_ratio
            print(f"   OI (估算): {oi_usd:,.0f} USDT")

    if funding_data:
        funding_rate = float(funding_data.get("lastFundingRate", 0))
        print(f"   资金费率 (服务器): {funding_rate:.6f}")
    elif not funding_rate:
        print(f"   资金费率: 使用默认值 0")

    price_change_24h = 0.0
    if ticker_data:
        volume_24h = float(ticker_data.get("quoteVolume", 0))
        price_change_24h = float(ticker_data.get("priceChangePercent", 0))
        print(f"   24h成交额: {volume_24h:,.0f} USDT")
        print(f"   24h涨跌: {price_change_24h:.2f}%")
    elif volume_24h:
        print(f"   24h成交额 (缓存): {volume_24h:,.0f} USDT")
    else:
        volume_24h = sum(k.get("quote_volume", 0) for k in klines_1h[-24:])
        print(f"   24h成交额 (估算): {volume_24h:,.0f} USDT")

    save_cache(cache_path, {
        "oi_usd": oi_usd,
        "funding_rate": funding_rate,
        "volume_24h": volume_24h,
        "cached_at": datetime.now().isoformat(),
    })

    # 合成4h K线（间隔从配置读取）
    interval_hours = config.get("kline", {}).get("synthetic_4h_interval", 4)
    klines_4h = synthesize_4h_klines(klines_1h, interval_hours=interval_hours)
    print(f"   4h K线(合成): {len(klines_4h)} 根")

    # EMA(4h)周期从配置读取
    close_prices_4h = [k["close"] for k in klines_4h]
    ema_period = config.get("kline", {}).get("ema_period", 20)
    ema20_4h = calc_ema(close_prices_4h, ema_period)
    deviation_4h = 0
    if ema20_4h > 0:
        deviation_4h = (current_price - ema20_4h) / ema20_4h * 100
        print(f"   EMA20(4h): {ema20_4h:.6f}")
        print(f"   偏离EMA20: {deviation_4h:+.2f}%")

    oi_market_cap_ratio = oi_usd / volume_24h if volume_24h > 0 else 0
    print(f"   OI/24h成交额: {oi_market_cap_ratio:.4f}")

    # 计算ATR（用于止盈止损）
    atr_period = config.get("atr", {}).get("period", 14)
    atr = calculate_atr(klines_1h, period=atr_period)
    if atr > 0:
        print(f"   ATR({atr_period}): {atr:.6f}")

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

    short_trade_levels = None

    # 准备最近K线（用于形态检测和K线回顾）
    recent_klines = klines_1h[-pattern.window_size:]

    pool_config = config.get("candidate_pool", {})
    liquidity_config = pool_config.get("liquidity", {})
    short_config = pool_config.get("short", {})
    min_oi = liquidity_config.get("min_oi_usd", 10_000_000)
    min_funding = short_config.get("funding_rate_annual", 0.80) * 100
    ema20_dev = short_config.get("ema20_deviation", 0.08)

    # V2.0-C：先检查 EMM 极端市场模式
    emm_config = config.get("emm", {})
    emm_short_config = emm_config.get("short", {})
    is_emm_short, emm_short_details = scoring.check_emm_conditions(
        direction="short",
        price_change_24h=price_change_24h,
        funding_rate=funding_rate,
        oi_market_cap_ratio=oi_market_cap_ratio,
    )

    if is_emm_short:
        print(f"\n  ⚡ EMM 极端市场模式触发！（做空）")
        print(f"     24h涨跌幅: {price_change_24h:+.2f}% (阈值: >= {emm_short_config.get('price_change_24h', 20)}%)")
        print(f"     {'✅' if emm_short_details['price_change_ok'] else '❌'} 涨跌幅条件")
        print(f"     年化资金费率: {emm_short_details['funding_rate_annual']:.2f}% (阈值: >= {emm_short_config.get('funding_rate_annual', 150)}%)")
        print(f"     {'✅' if emm_short_details['funding_rate_ok'] else '❌'} 资金费率条件")
        print(f"     OI/市值比: {oi_market_cap_ratio:.4f} (阈值: >= {emm_short_config.get('oi_market_cap_ratio', 0.25)})")
        print(f"     {'✅' if emm_short_details['oi_market_cap_ok'] else '❌'} OI/市值比条件")

        print(f"\n  📊 EMM综合评分（做空，跳过候选池和形态检测）:")
        try:
            short_score_result = scoring.score(
                symbol=symbol,
                direction="short",
                oi_market_cap_ratio=oi_market_cap_ratio,
                patterns={},
                funding_rate=funding_rate,
                price_change_24h=price_change_24h,
            )
            short_should_enter = scoring.should_entry(short_score_result)
            print(f"     合约数据评分: {short_score_result.contract_score:.2f} (权重: {scoring.contract_weight:.0%})")
            print(f"     技术面评分(固定): {short_score_result.technical_score:.2f} (权重: {scoring.technical_weight:.0%})")
            print(f"     情绪面评分:   {short_score_result.sentiment_score:.2f} (权重: {scoring.sentiment_weight:.0%})")
            print(f"     ─────────────────────────────")
            # 根据入场模式显示对应阈值
            display_threshold = _get_display_threshold(short_score_result, scoring)
            print(f"     总分: {short_score_result.total_score:.2f} (阈值: {display_threshold})")
            print(f"     入场判断: {'✅ 建议入场' if short_should_enter else '❌ 不建议入场'}")
            if short_score_result.veto:
                print(f"     ❌ 否决: {short_score_result.veto_reason}")
        except Exception as e:
            print(f"     ❌ 评分失败: {e}")
            traceback.print_exc()
            short_score_result = None
            short_should_enter = False
        candidate_short = None  # EMM模式跳过候选池
        short_pattern_result = {}
    else:
        print(f"\n  📋 候选池筛选条件:")
        oi_ok = oi_usd >= min_oi
        funding_ok = annual_funding >= min_funding
        ema20_ok = deviation_4h >= ema20_dev * 100 if ema20_4h > 0 else False

        print(f"     OI >= {min_oi:,.0f} USDT: {oi_usd:,.0f} {'✅' if oi_ok else '❌'}")
        print(f"     年化费率 >= {min_funding:.2f}%: {annual_funding:.4f}% {'✅' if funding_ok else '❌'}")
        print(f"     偏离EMA20 >= {ema20_dev * 100:.0f}%: {deviation_4h:+.2f}% {'✅' if ema20_ok else '❌'}")

        candidate_short = oi_ok and funding_ok and ema20_ok
        print(f"\n  📌 做空候选: {'✅ 通过' if candidate_short else '❌ 不满足'}")

        if candidate_short:
            print(f"\n  📊 形态检测（做空，最近{pattern.window_size}根K线）:")
            short_pattern_result = pattern.detect_short_patterns(recent_klines)

            short_patterns_info = [
                ("三次冲顶", "three_tops"),
                ("双顶(替代)", "double_top"),
                ("V型反转(替代)", "v_reversal_short"),
                ("长上影线", "long_upper_shadow"),
                ("放量滞涨", "volume_stagnation"),
            ]
            for name, key in short_patterns_info:
                detected, score_val = short_pattern_result.get(key, (False, 0))
                print(f"     {name}: {'✅ 检测到' if detected else '❌ 未检测'} (得分: {score_val})")

            print(f"\n  📊 综合评分（做空）:")
            try:
                short_score_result = scoring.score(
                    symbol=symbol,
                    direction="short",
                    oi_market_cap_ratio=oi_market_cap_ratio,
                    patterns=short_pattern_result,
                    funding_rate=funding_rate,
                    price_change_24h=price_change_24h,
                )
                short_should_enter = scoring.should_entry(short_score_result)
                print(f"     合约数据评分: {short_score_result.contract_score:.2f} (权重: {scoring.contract_weight:.0%})")
                print(f"     技术面评分:   {short_score_result.technical_score:.2f} (权重: {scoring.technical_weight:.0%})")
                print(f"     情绪面评分:   {short_score_result.sentiment_score:.2f} (权重: {scoring.sentiment_weight:.0%})")
                print(f"     ─────────────────────────────")
                # 根据入场模式显示对应阈值
                display_threshold = _get_display_threshold(short_score_result, scoring)
                print(f"     总分: {short_score_result.total_score:.2f} (阈值: {display_threshold})")
                print(f"     入场判断: {'✅ 建议入场' if short_should_enter else '❌ 不建议入场'}")
                if short_score_result.veto:
                    print(f"     ❌ 否决: {short_score_result.veto_reason}")
                if short_score_result and short_score_result.entry_mode == "semi_emm":
                    print(f"     ⚡ 半EMM模式：跳过形态门槛")
            except Exception as e:
                print(f"     ❌ 评分失败: {e}")
                traceback.print_exc()
                short_score_result = None
                short_should_enter = False
        else:
            short_score_result = None
            short_should_enter = False
            short_pattern_result = {}

        # 做空信号触发时展示交易水平
        short_trade_levels = None
        if short_should_enter and short_score_result is not None:
            short_trade_levels = calculate_trade_levels(
                direction="short",
                entry_price=current_price,
                atr=atr,
                config=config,
            )
            _print_trade_levels(short_trade_levels, "short")

    # ==============================================
    # 做多分析
    # ==============================================
    print("\n" + "=" * 70)
    print("  🔍 做多方向分析")
    print("=" * 70)

    long_trade_levels = None

    long_config = pool_config.get("long", {})
    min_oi_long = liquidity_config.get("min_oi_usd", 10_000_000)
    max_funding = long_config.get("funding_rate_annual", -0.20) * 100
    long_ema20_deviation = long_config.get("ema20_deviation", -0.06)

    # V2.0-C：先检查 EMM 极端市场模式
    emm_long_config = emm_config.get("long", {})
    is_emm_long, emm_long_details = scoring.check_emm_conditions(
        direction="long",
        price_change_24h=price_change_24h,
        funding_rate=funding_rate,
        oi_market_cap_ratio=oi_market_cap_ratio,
    )

    if is_emm_long:
        print(f"\n  ⚡ EMM 极端市场模式触发！（做多）")
        print(f"     24h涨跌幅: {price_change_24h:+.2f}% (阈值: <= {emm_long_config.get('price_change_24h', -20)}%)")
        print(f"     {'✅' if emm_long_details['price_change_ok'] else '❌'} 涨跌幅条件")
        print(f"     年化资金费率: {emm_long_details['funding_rate_annual']:.2f}% (阈值: <= {emm_long_config.get('funding_rate_annual', -50)}%)")
        print(f"     {'✅' if emm_long_details['funding_rate_ok'] else '❌'} 资金费率条件")
        print(f"     OI/市值比: {oi_market_cap_ratio:.4f} (阈值: >= {emm_long_config.get('oi_market_cap_ratio', 0.15)})")
        print(f"     {'✅' if emm_long_details['oi_market_cap_ok'] else '❌'} OI/市值比条件")

        print(f"\n  📊 EMM综合评分（做多，跳过候选池和形态检测）:")
        try:
            long_score_result = scoring.score(
                symbol=symbol,
                direction="long",
                oi_market_cap_ratio=oi_market_cap_ratio,
                patterns={},
                funding_rate=funding_rate,
                price_change_24h=price_change_24h,
            )
            long_should_enter = scoring.should_entry(long_score_result)
            print(f"     合约数据评分: {long_score_result.contract_score:.2f} (权重: {scoring.contract_weight:.0%})")
            print(f"     技术面评分(固定): {long_score_result.technical_score:.2f} (权重: {scoring.technical_weight:.0%})")
            print(f"     情绪面评分:   {long_score_result.sentiment_score:.2f} (权重: {scoring.sentiment_weight:.0%})")
            print(f"     ─────────────────────────────")
            # 根据入场模式显示对应阈值
            display_threshold = _get_display_threshold(long_score_result, scoring)
            print(f"     总分: {long_score_result.total_score:.2f} (阈值: {display_threshold})")
            print(f"     入场判断: {'✅ 建议入场' if long_should_enter else '❌ 不建议入场'}")
            if long_score_result.veto:
                print(f"     ❌ 否决: {long_score_result.veto_reason}")
        except Exception as e:
            print(f"     ❌ 评分失败: {e}")
            traceback.print_exc()
            long_score_result = None
            long_should_enter = False
        candidate_long = None  # EMM模式跳过候选池
        long_pattern_result = {}
    else:
        print(f"\n  📋 候选池筛选条件:")
        oi_ok_long = oi_usd >= min_oi_long
        funding_ok_long = annual_funding <= max_funding
        ema20_ok_long = deviation_4h <= long_ema20_deviation * 100 if ema20_4h > 0 else False

        print(f"     OI >= {min_oi_long:,.0f} USDT: {oi_usd:,.0f} {'✅' if oi_ok_long else '❌'}")
        print(f"     年化费率 <= {max_funding:.2f}%: {annual_funding:.4f}% {'✅' if funding_ok_long else '❌'}")
        print(f"     偏离EMA20 <= {long_ema20_deviation * 100:.0f}%: {deviation_4h:+.2f}% {'✅' if ema20_ok_long else '❌'}")

        candidate_long = oi_ok_long and funding_ok_long and ema20_ok_long
        print(f"\n  📌 做多候选: {'✅ 通过' if candidate_long else '❌ 不满足'}")

        if candidate_long:
            print(f"\n  📊 形态检测（做多）:")
            long_pattern_result = pattern.detect_long_patterns(recent_klines)

            long_patterns_info = [
                ("三次探底", "three_bottoms"),
                ("双底(替代)", "double_bottom"),
                ("V型反转(替代)", "v_reversal_long"),
                ("长下影线", "long_lower_shadow"),
                ("放量止跌", "volume_reversal"),
            ]
            for name, key in long_patterns_info:
                detected, score_val = long_pattern_result.get(key, (False, 0))
                print(f"     {name}: {'✅ 检测到' if detected else '❌ 未检测'} (得分: {score_val})")

            print(f"\n  📊 综合评分（做多）:")
            try:
                long_score_result = scoring.score(
                    symbol=symbol,
                    direction="long",
                    oi_market_cap_ratio=oi_market_cap_ratio,
                    patterns=long_pattern_result,
                    funding_rate=funding_rate,
                    price_change_24h=price_change_24h,
                )
                long_should_enter = scoring.should_entry(long_score_result)
                print(f"     合约数据评分: {long_score_result.contract_score:.2f} (权重: {scoring.contract_weight:.0%})")
                print(f"     技术面评分:   {long_score_result.technical_score:.2f} (权重: {scoring.technical_weight:.0%})")
                print(f"     情绪面评分:   {long_score_result.sentiment_score:.2f} (权重: {scoring.sentiment_weight:.0%})")
                print(f"     ─────────────────────────────")
                # 根据入场模式显示对应阈值
                display_threshold = _get_display_threshold(long_score_result, scoring)
                print(f"     总分: {long_score_result.total_score:.2f} (阈值: {display_threshold})")
                print(f"     入场判断: {'✅ 建议入场' if long_should_enter else '❌ 不建议入场'}")
                if long_score_result.veto:
                    print(f"     ❌ 否决: {long_score_result.veto_reason}")
                if long_score_result and long_score_result.entry_mode == "semi_emm":
                    print(f"     ⚡ 半EMM模式：跳过形态门槛")
            except Exception as e:
                print(f"     ❌ 评分失败: {e}")
                traceback.print_exc()
                long_score_result = None
                long_should_enter = False
        else:
            long_score_result = None
            long_should_enter = False
            long_pattern_result = {}

        # 做多信号触发时展示交易水平
        long_trade_levels = None
        if long_should_enter and long_score_result is not None:
            long_trade_levels = calculate_trade_levels(
                direction="long",
                entry_price=current_price,
                atr=atr,
                config=config,
            )
            _print_trade_levels(long_trade_levels, "long")

    # ==============================================
    # 最终结论
    # ==============================================
    print("\n" + "=" * 70)
    print("  🎯 最终结论")
    print("=" * 70)

    # 根据入场模式动态获取阈值（F+G：半EMM阈值为5.0）
    short_display_threshold = _get_display_threshold(short_score_result, scoring)
    long_display_threshold = _get_display_threshold(long_score_result, scoring)

    print(f"\n  📉 做空方向:")
    if is_emm_short:
        # EMM模式：跳过候选池，直接看评分结果
        if short_score_result is None:
            print(f"     ❌ 评分失败")
        elif short_score_result.veto:
            print(f"     ❌ 被否决: {short_score_result.veto_reason}")
        elif not short_should_enter:
            print(f"     ❌ EMM综合评分不满足: {short_score_result.total_score:.2f}/{short_display_threshold}")
        else:
            print(f"     ⚡⚡⚡ EMM极端市场模式 满足做空条件！总分: {short_score_result.total_score:.2f} >= {short_display_threshold}")
    elif not candidate_short:
        print(f"     ❌ 候选池筛选不通过")
    elif short_score_result is None:
        print(f"     ❌ 评分失败")
    elif short_score_result.veto:
        print(f"     ❌ 被否决: {short_score_result.veto_reason}")
    elif not short_should_enter:
        print(f"     ❌ 综合评分不满足: {short_score_result.total_score:.2f}/{short_display_threshold}")
    else:
        print(f"     ✅✅✅ 满足做空条件！总分: {short_score_result.total_score:.2f} >= {short_display_threshold}")
        if short_score_result.entry_mode == "semi_emm":
            print(f"     ⚡ 半EMM模式：跳过形态门槛")

    print(f"\n  📈 做多方向:")
    if is_emm_long:
        # EMM模式：跳过候选池，直接看评分结果
        if long_score_result is None:
            print(f"     ❌ 评分失败")
        elif long_score_result.veto:
            print(f"     ❌ 被否决: {long_score_result.veto_reason}")
        elif not long_should_enter:
            print(f"     ❌ EMM综合评分不满足: {long_score_result.total_score:.2f}/{long_display_threshold}")
        else:
            print(f"     ⚡⚡⚡ EMM极端市场模式 满足做多条件！总分: {long_score_result.total_score:.2f} >= {long_display_threshold}")
    elif not candidate_long:
        print(f"     ❌ 候选池筛选不通过")
    elif long_score_result is None:
        print(f"     ❌ 评分失败")
    elif long_score_result.veto:
        print(f"     ❌ 被否决: {long_score_result.veto_reason}")
    elif not long_should_enter:
        print(f"     ❌ 综合评分不满足: {long_score_result.total_score:.2f}/{long_display_threshold}")
    else:
        print(f"     ✅✅✅ 满足做多条件！总分: {long_score_result.total_score:.2f} >= {long_display_threshold}")
        if long_score_result.entry_mode == "semi_emm":
            print(f"     ⚡ 半EMM模式：跳过形态门槛")

    print("\n" + "=" * 70)

    # K线回顾
    print(f"\n📈 最近5根1h K线:")
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

    # 构建返回结果
    start_dt = datetime.fromtimestamp(klines_1h[0]["open_time"] / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(klines_1h[-1]["open_time"] / 1000, tz=timezone.utc)

    return {
        "symbol": symbol,
        "current_price": current_price,
        "oi_usd": oi_usd,
        "funding_rate": funding_rate,
        "annual_funding": annual_funding,
        "volume_24h": volume_24h,
        "price_change_24h": price_change_24h,
        "oi_market_cap_ratio": oi_market_cap_ratio,
        "deviation_4h": deviation_4h,
        "ema20_4h": ema20_4h,
        "klines_1h_count": len(klines_1h),
        "klines_4h_count": len(klines_4h),
        "data_start_time": start_dt.isoformat(),
        "data_end_time": end_dt.isoformat(),
        # 做空
        "candidate_short": candidate_short,
        "is_emm_short": is_emm_short,
        "short_pattern_result": short_pattern_result,
        "short_score_result": short_score_result.to_dict() if short_score_result else None,
        "short_should_enter": short_should_enter,
        # 做多
        "candidate_long": candidate_long,
        "is_emm_long": is_emm_long,
        "long_pattern_result": long_pattern_result,
        "long_score_result": long_score_result.to_dict() if long_score_result else None,
        "long_should_enter": long_should_enter,
        # 交易水平
        "short_trade_levels": short_trade_levels,
        "long_trade_levels": long_trade_levels,
        "atr": atr,
    }


# ============================================================
# 交易模拟模块
# ============================================================
def calculate_atr(klines: List[Dict], period: int = 14) -> float:
    """计算 ATR（平均真实波幅）"""
    if len(klines) < period + 1:
        return 0
    tr_values = []
    for i in range(1, len(klines)):
        high = klines[i]["high"]
        low = klines[i]["low"]
        prev_close = klines[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    if len(tr_values) < period:
        return sum(tr_values) / len(tr_values)
    return sum(tr_values[-period:]) / period


def calculate_trade_levels(
    direction: str,
    entry_price: float,
    atr: float,
    config: Dict,
) -> Dict:
    """
    计算交易的开仓价、止损价、止盈价

    Args:
        direction: 'short' 或 'long'
        entry_price: 入场价格
        atr: ATR 值
        config: 策略配置

    Returns:
        {
            "entry_price": 入场价,
            "stop_loss": 止损价,
            "take_profit_1": 第一止盈目标,
            "take_profit_2": 第二止盈目标,
            "risk_percent": 风险百分比,
            "reward_1_percent": 第一目标收益百分比,
            "reward_2_percent": 第二目标收益百分比,
        }
    """
    trading_config = config.get("trading", {})
    sl_config = trading_config.get("stop_loss", {})
    tp_config = trading_config.get("batch_take_profit", {})

    atr_multiplier = sl_config.get("atr_multiplier", 2.5)
    min_abs_pct = sl_config.get("min_absolute_percent", 0.05)
    max_stop_pct = sl_config.get("max_stop_percent", 0.20)  # 止损最多20%
    tp1_mult = tp_config.get("target1_atr_multiplier", 1.5)
    tp2_mult = tp_config.get("target2_atr_multiplier", 3.5)

    if direction == "short":
        # 做空：止损在入场价上方，止盈在入场价下方
        # ATR基于止损（受上限约束）
        sl_atr_pct = atr * atr_multiplier / entry_price if entry_price > 0 else 0.05
        sl_pct = min(sl_atr_pct, max_stop_pct)  # 不超过最大止损百分比
        sl_pct = max(sl_pct, min_abs_pct)  # 不低于最小止损百分比
        stop_loss = entry_price * (1 + sl_pct)

        # ATR基于止盈（受上限约束，确保止盈价为正）
        tp1_atr_pct = atr * tp1_mult / entry_price if entry_price > 0 else 0.02
        tp2_atr_pct = atr * tp2_mult / entry_price if entry_price > 0 else 0.05
        tp1_pct = min(tp1_atr_pct, max_stop_pct * 0.75)  # 止盈不超过止损的75%
        tp2_pct = min(tp2_atr_pct, max_stop_pct * 1.5)
        take_profit_1 = max(entry_price * (1 - tp1_pct), entry_price * 0.001)  # 不低于0.1%价格
        take_profit_2 = max(entry_price * (1 - tp2_pct), entry_price * 0.001)

        risk_pct = (stop_loss - entry_price) / entry_price * 100
        reward_1_pct = (entry_price - take_profit_1) / entry_price * 100
        reward_2_pct = (entry_price - take_profit_2) / entry_price * 100
    else:
        # 做多：止损在入场价下方，止盈在入场价上方
        sl_atr_pct = atr * atr_multiplier / entry_price if entry_price > 0 else 0.05
        sl_pct = min(sl_atr_pct, max_stop_pct)
        sl_pct = max(sl_pct, min_abs_pct)
        stop_loss = entry_price * (1 - sl_pct)

        tp1_atr_pct = atr * tp1_mult / entry_price if entry_price > 0 else 0.02
        tp2_atr_pct = atr * tp2_mult / entry_price if entry_price > 0 else 0.05
        tp1_pct = min(tp1_atr_pct, max_stop_pct * 0.75)
        tp2_pct = min(tp2_atr_pct, max_stop_pct * 1.5)
        take_profit_1 = entry_price * (1 + tp1_pct)
        take_profit_2 = entry_price * (1 + tp2_pct)

        risk_pct = (entry_price - stop_loss) / entry_price * 100
        reward_1_pct = (take_profit_1 - entry_price) / entry_price * 100
        reward_2_pct = (take_profit_2 - entry_price) / entry_price * 100

    return {
        "entry_price": round(entry_price, 8),
        "stop_loss": round(stop_loss, 8),
        "take_profit_1": round(take_profit_1, 8),
        "take_profit_2": round(take_profit_2, 8),
        "risk_percent": round(risk_pct, 2),
        "reward_1_percent": round(reward_1_pct, 2),
        "reward_2_percent": round(reward_2_pct, 2),
        "risk_reward_1": round(reward_1_pct / risk_pct, 2) if risk_pct > 0 else 0,
        "risk_reward_2": round(reward_2_pct / risk_pct, 2) if risk_pct > 0 else 0,
    }


def simulate_trade_forward(
    klines: List[Dict],
    start_idx: int,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit_1: float,
    take_profit_2: float,
    tp1_close_percent: float = 0.30,
    tp2_close_percent: float = 0.40,
    max_holding_hours: int = 72,
) -> Dict:
    """
    前向模拟交易：从 start_idx 开始遍历后续K线，判断止损/止盈触发

    Args:
        klines: 完整K线数据
        start_idx: 入场K线索引
        direction: 'short' 或 'long'
        entry_price: 入场价
        stop_loss: 止损价
        take_profit_1: 第一止盈目标
        take_profit_2: 第二止盈目标
        tp1_close_percent: 第一目标平仓比例
        tp2_close_percent: 第二目标平仓比例
        max_holding_hours: 最大持仓时间（小时）

    Returns:
        {
            "exit_price": 最终退出价,
            "exit_reason": "stop_loss" | "take_profit_1" | "take_profit_2" | "time_stop" | "still_open",
            "pnl_percent": 盈亏百分比,
            "holding_hours": 持仓小时数,
            "bars_held": 持仓K线数,
            "hit_tp1": bool,
            "hit_tp2": bool,
            "hit_sl": bool,
        }
    """
    remaining = klines[start_idx + 1:]  # 入场后的K线
    if not remaining:
        return {
            "exit_price": entry_price,
            "exit_reason": "still_open",
            "pnl_percent": 0,
            "holding_hours": 0,
            "bars_held": 0,
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_sl": False,
        }

    hit_tp1 = False
    hit_tp2 = False
    hit_sl = False
    exit_price = entry_price
    exit_reason = "still_open"
    bars_held = 0

    for i, k in enumerate(remaining):
        bars_held = i + 1
        high = k["high"]
        low = k["low"]
        close = k["close"]

        if direction == "short":
            # 做空：价格下跌盈利，上涨亏损
            if high >= stop_loss:
                exit_price = stop_loss
                exit_reason = "stop_loss"
                hit_sl = True
                break
            elif low <= take_profit_2:
                exit_price = take_profit_2
                exit_reason = "take_profit_2"
                hit_tp2 = True
                break
            elif low <= take_profit_1:
                exit_price = take_profit_1
                exit_reason = "take_profit_1"
                hit_tp1 = True
                break
        else:
            # 做多：价格上涨盈利，下跌亏损
            if low <= stop_loss:
                exit_price = stop_loss
                exit_reason = "stop_loss"
                hit_sl = True
                break
            elif high >= take_profit_2:
                exit_price = take_profit_2
                exit_reason = "take_profit_2"
                hit_tp2 = True
                break
            elif high >= take_profit_1:
                exit_price = take_profit_1
                exit_reason = "take_profit_1"
                hit_tp1 = True
                break

        # 时间止损
        if bars_held >= max_holding_hours:
            exit_price = close
            exit_reason = "time_stop"
            break

    if exit_reason == "still_open":
        exit_price = remaining[-1]["close"]

    # 计算盈亏
    if direction == "short":
        pnl_pct = (entry_price - exit_price) / entry_price * 100
    else:
        pnl_pct = (exit_price - entry_price) / entry_price * 100

    return {
        "exit_price": round(exit_price, 8),
        "exit_reason": exit_reason,
        "pnl_percent": round(pnl_pct, 2),
        "holding_hours": bars_held,
        "bars_held": bars_held,
        "hit_tp1": hit_tp1,
        "hit_tp2": hit_tp2,
        "hit_sl": hit_sl,
    }


def backtest_historical(
    symbol: str,
    klines_1h: List[Dict],
    config: Dict,
    pattern: PatternRecognizer,
    scoring: ScoringEngine,
) -> List[Dict]:
    """
    历史数据回测：遍历历史K线，逐点运行评分并模拟交易

    每4小时（4根1h K线）运行一次评分，触发信号后前向模拟交易。

    Args:
        symbol: 交易对
        klines_1h: 1h K线数据
        config: 策略配置
        pattern: 形态识别器
        scoring: 评分引擎

    Returns:
        交易记录列表
    """
    trades = []
    min_klines = config.get("kline", {}).get("min_klines_for_analysis", 24)
    step = 4  # 每4小时检查一次

    if len(klines_1h) < min_klines + 10:
        return trades

    # 注意：历史回测无法获取历史OI和费率，只能使用当前值近似
    # 这里我们仅做信号触发频率的统计，不做完整交易模拟
    # 完整的交易模拟需要历史OI/费率数据

    # 实际上，对于完整的历史回测，我们需要做的事情比较有限
    # 因为我们没有历史OI和费率数据
    # 这里先跳过，直接用当前数据做单点分析+交易水平展示

    return trades


def _print_trade_levels(trade_levels: Dict, direction: str):
    """打印交易水平（开仓价、止盈止损）"""
    dir_label = "做空" if direction == "short" else "做多"
    print(f"\n  💰 {dir_label}交易水平:")
    print(f"     入场价格:     {trade_levels['entry_price']:.8f}")
    print(f"     止损价格:     {trade_levels['stop_loss']:.8f} "
          f"(风险: {trade_levels['risk_percent']:.2f}%)")
    print(f"     止盈目标1:    {trade_levels['take_profit_1']:.8f} "
          f"(收益: {trade_levels['reward_1_percent']:.2f}%, R:R=1:{trade_levels['risk_reward_1']})")
    print(f"     止盈目标2:    {trade_levels['take_profit_2']:.8f} "
          f"(收益: {trade_levels['reward_2_percent']:.2f}%, R:R=1:{trade_levels['risk_reward_2']})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HRS策略回测分析")
    parser.add_argument("--symbol", default="LABUSDT", help="交易对（如 LABUSDT, SAHARAUSDT）")
    args = parser.parse_args()

    analyze_symbol(symbol=args.symbol)