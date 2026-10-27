"""MTPCS 激进版等价提取用的冻结夹具（characterization / golden）。

口径（计划 D3 / D4）：
- 输入（配置子树 + K 线序列）在此**冻结**，不随生产配置变更而漂移；
- 输出（逐字段）冻结在 ``btc_eth_aggressive_golden_expected.json``；
- 所有 K 线均为**确定性合成序列**，只能证明「提取前后逐字段一致」，
  **不得**记为「实盘金样对账通过」。
"""

from __future__ import annotations

import json
import math
import os
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

UTC = timezone.utc
FIXTURE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(FIXTURE_DIR, "btc_eth_aggressive_config_golden.json")
EXPECTED_PATH = os.path.join(FIXTURE_DIR, "btc_eth_aggressive_golden_expected.json")

SCENARIOS = ("trend_up", "trend_down", "ranging", "trend_pullback", "divergence")


# ---------------------------------------------------------------------------
# 配置：读取冻结子树并包装成策略构造期的最小 config 结构
# ---------------------------------------------------------------------------


def frozen_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def config_dict() -> Dict[str, Any]:
    raw = frozen_config()
    return {
        "strategy": {
            "timeframes": raw["timeframes"],
            "scoring": raw["scoring"],
            "symbol_config": raw["symbol_config"],
            "risk": raw["risk"],
        }
    }


# ---------------------------------------------------------------------------
# K 线：确定性合成（无随机数，重复执行逐字节一致）
# ---------------------------------------------------------------------------


def _row(
    open_time: datetime,
    step_hours: int,
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float,
    volume: float,
) -> Dict[str, Any]:
    return {
        "open_time": open_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "close_time": (open_time + timedelta(hours=step_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": round(float(open_p), 6),
        "high": round(float(high_p), 6),
        "low": round(float(low_p), 6),
        "close": round(float(close_p), 6),
        "volume": round(float(volume), 6),
        "quote_volume": round(float(volume * close_p), 6),
        "trades": 100,
    }


def make_klines(
    count: int,
    step_hours: int,
    seed_price: float,
    drift: float,
    wave_amp: float,
    wave_len: float,
    volume_base: float = 100.0,
    overrides: Optional[Dict[int, Dict[str, float]]] = None,
    start: str = "2026-01-01T00:00:00Z",
) -> List[Dict[str, Any]]:
    """生成 ``count`` 根 K 线；``overrides`` 以「距末尾第几根」覆盖 OHLC。"""
    cursor = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    rows: List[Dict[str, Any]] = []
    prev_close = seed_price
    for index in range(count):
        center = seed_price + drift * index + wave_amp * math.sin(2 * math.pi * index / wave_len)
        open_p = prev_close
        close_p = center
        high_p = max(open_p, close_p) + abs(drift) + wave_amp * 0.05
        low_p = min(open_p, close_p) - abs(drift) - wave_amp * 0.05
        volume = volume_base * (1.0 + 0.3 * math.sin(2 * math.pi * index / 7.0))
        rows.append(_row(cursor, step_hours, open_p, high_p, low_p, close_p, volume))
        prev_close = close_p
        cursor = cursor + timedelta(hours=step_hours)

    for offset, patch in (overrides or {}).items():
        row = rows[-offset]
        for field in ("open", "high", "low", "close", "volume"):
            if field in patch:
                row[field] = round(float(patch[field]), 6)
    return rows


def _pin_close_near_ma(
    rows: List[Dict[str, Any]], window: int = 21, factor: float = 1.0
) -> None:
    """把最后一根收盘价定在「前 window 根均值」的 factor 倍。

    用于稳定覆盖「价格贴均线」分支：改动只落在末根 close，high/low 保证包含它。
    """
    previous = [float(row["close"]) for row in rows[-window - 1 : -1]]
    target = round(sum(previous) / len(previous) * factor, 6)
    last = rows[-1]
    last["close"] = target
    last["high"] = round(max(float(last["high"]), target), 6)
    last["low"] = round(min(float(last["low"]), target), 6)


def _divergence_overrides() -> Dict[int, Dict[str, float]]:
    """构造 1h 末 20 根的「底背离」路径：价格创新低但 RSI 低点抬高。"""
    # 与基座（约 104 震荡）衔接，避免首根跳空把 RSI 抬高
    closes = [104.0 - 1.0 * index for index in range(10)]  # 104 → 95：稳步下行
    closes += [101.0 - 0.8 * index for index in range(10)]  # 101 → 93.8：反抽后缓跌
    overrides: Dict[int, Dict[str, float]] = {}
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        overrides[20 - index] = {
            "open": previous,
            "high": max(previous, close) + 0.4,
            "low": min(previous, close) - 0.4,
            "close": close,
            "volume": 500.0,
        }
    return overrides


def _scenario_klines(scenario: str) -> Dict[str, List[Dict[str, Any]]]:
    """确定性场景：单边上涨 / 单边下跌 / 区间震荡（含吞没）/ 回调贴均线 / 背离。"""
    if scenario == "trend_up":
        return {
            "1h": make_klines(120, 1, 100.0, 0.06, 0.35, 18.0, volume_base=90.0),
            "4h": make_klines(140, 4, 96.0, 0.30, 0.60, 16.0, volume_base=360.0),
            "1d": make_klines(80, 24, 90.0, 1.20, 0.80, 14.0, volume_base=2200.0),
        }
    if scenario == "trend_down":
        return {
            "1h": make_klines(120, 1, 140.0, -0.06, 0.35, 18.0, volume_base=90.0),
            "4h": make_klines(140, 4, 145.0, -0.30, 0.60, 16.0, volume_base=360.0),
            "1d": make_klines(80, 24, 150.0, -1.20, 0.80, 14.0, volume_base=2200.0),
        }
    if scenario == "trend_pullback":
        # 日线强趋势 + 4h 横盘贴 MA21 + 1h 站上 MA21 → 覆盖 trend_filter 通过分支
        klines_1h = make_klines(120, 1, 100.0, 0.15, 4.0, 18.0, volume_base=90.0)
        klines_4h = make_klines(140, 4, 99.0, 0.02, 0.60, 16.0, volume_base=360.0)
        _pin_close_near_ma(klines_1h, window=21, factor=1.01)
        _pin_close_near_ma(klines_4h, window=21, factor=1.0)
        return {
            "1h": klines_1h,
            "4h": klines_4h,
            "1d": make_klines(80, 24, 90.0, 1.20, 0.80, 14.0, volume_base=2200.0),
        }
    if scenario == "divergence":
        # 底背离：1h 末 20 根按 _divergence_overrides 的价格路径覆盖
        return {
            "1h": make_klines(
                120, 1, 104.0, 0.0, 1.30, 12.0, volume_base=90.0,
                overrides=_divergence_overrides(),
            ),
            "4h": make_klines(140, 4, 103.0, 0.0, 1.80, 10.0, volume_base=360.0),
            "1d": make_klines(80, 24, 102.0, 0.02, 2.20, 9.0, volume_base=2200.0),
        }
    # ranging：末端构造「看涨吞没」最后两根，用于覆盖形态分支
    return {
        "1h": make_klines(
            120,
            1,
            104.0,
            0.0,
            1.30,
            12.0,
            volume_base=90.0,
            overrides={
                2: {"open": 104.6, "high": 105.0, "low": 103.8, "close": 104.0, "volume": 40.0},
                1: {"open": 103.7, "high": 105.6, "low": 103.6, "close": 105.4, "volume": 60.0},
            },
        ),
        "4h": make_klines(
            140,
            4,
            103.0,
            0.0,
            1.80,
            10.0,
            volume_base=360.0,
            overrides={
                2: {"open": 103.9, "high": 104.4, "low": 102.9, "close": 103.2, "volume": 200.0},
                1: {"open": 103.0, "high": 105.1, "low": 102.8, "close": 104.9, "volume": 220.0},
            },
        ),
        "1d": make_klines(80, 24, 102.0, 0.02, 2.20, 9.0, volume_base=2200.0),
    }


def scenario_data(scenario: str) -> Dict[str, Any]:
    """返回该场景的 klines 与指标（每次重建，避免跨用例共享可变状态）。"""
    klines = _scenario_klines(scenario)
    return {"klines": klines}


# ---------------------------------------------------------------------------
# 目标构造：同一组用例分别打到「实盘实现」与「core 纯函数」
# ---------------------------------------------------------------------------


class _StubBinance:
    """禁止入场条件中第 2–4 项走网络的桩：一律抛错，走异常兜底分支。"""

    async def get_funding_rate(self, symbol: str) -> float:
        raise RuntimeError("stub-no-network")

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        raise RuntimeError("stub-no-network")

    async def get_orderbook(self, symbol: str, limit: int = 5) -> Dict[str, Any]:
        raise RuntimeError("stub-no-network")


def _norm(value: Any) -> Any:
    """统一成可比对的 JSON 值：Decimal→str、float→9 位、tuple→list。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _norm(value[key]) for key in sorted(value)}
    item = getattr(value, "item", None)
    if callable(item):
        # numpy 标量（np.bool_ / np.float64 / np.int64）统一降为 Python 标量
        return _norm(item())
    return value


def _make_live_strategy():
    from strategies.btc_eth_aggressive.strategy import BTCEthStrategy

    cfg = config_dict()
    strategy = object.__new__(BTCEthStrategy)
    strategy.config = cfg
    strategy.timeframes = cfg["strategy"]["timeframes"]
    strategy.risk_config = cfg["strategy"]["risk"]
    strategy.scoring_config = cfg["strategy"]["scoring"]
    strategy.symbol_config = cfg["strategy"]["symbol_config"]
    strategy.symbol_extreme_pause_until = {}
    strategy.binance = _StubBinance()
    return strategy


def _position(**kwargs):
    from strategies.btc_eth_aggressive.strategy import PositionState

    position = PositionState()
    for key, value in kwargs.items():
        setattr(position, key, value)
    return position


async def _capture_time_stop(strategy, grade: str, hours: float) -> Optional[Dict[str, Any]]:
    """把 ``_check_time_stop`` 的决策轨迹归一成与 core 相同的决策字典。"""
    captured: List[Dict[str, Any]] = []

    async def _record(symbol, position, close_ratio, reason, **kwargs):
        captured.append(
            {
                "action": "close",
                "close_ratio": str(close_ratio),
                "reason": reason,
                "review_exhausted": bool(kwargs.get("review_exhausted", False)),
                "set_tp1_hit": bool(kwargs.get("set_tp1_hit", False)),
            }
        )

    position = _position(
        direction="LONG",
        entry_price=Decimal("100"),
        atr=Decimal("1.5"),
        grade=grade,
        tp1_hit=False,
        current_quantity=Decimal("1"),
        time_stop_review_done=False,
        entry_time=datetime.now() - timedelta(hours=hours),
    )
    strategy._do_time_stop_close = _record  # type: ignore[assignment]

    async def _no_data(symbol):
        return None, None

    strategy._get_review_market_data = _no_data  # type: ignore[assignment]
    await strategy._check_time_stop("BTCUSDT", position)
    return captured[0] if captured else None



def build_live_targets() -> Dict[str, Any]:
    """以**当前实盘实现**计算全部金样用例（提取前 = 冻结基线）。"""
    import asyncio

    import pandas as pd

    from strategies.btc_eth_aggressive.strategy import _safe_last

    strategy = _make_live_strategy()
    cfg = strategy.config["strategy"]
    scoring = cfg["scoring"]
    risk = cfg["risk"]
    out: Dict[str, Any] = {}

    # --- 指标与安全读取 ---
    out["safe_last/missing_timeframe"] = _safe_last({}, "1h", "RSI")
    out["safe_last/empty_series"] = _safe_last({"1h": {"RSI": pd.Series([])}}, "1h", "RSI")
    out["safe_last/nan_value"] = _safe_last(
        {"1h": {"RSI": pd.Series([1.0, float("nan")])}}, "1h", "RSI"
    )
    out["safe_last_klines/empty"] = strategy._safe_last_klines_value({}, "1h", "close")
    out["safe_last_klines/ok"] = strategy._safe_last_klines_value(
        {"1h": _scenario_klines("ranging")["1h"]}, "1h", "close"
    )

    indicator_fields = (
        "MA21",
        "MA55",
        "EMA21",
        "EMA55",
        "RSI",
        "ATR",
        "ATR_long",
        "ADX",
        "MACD",
        "MACD_Hist",
        "BB_Upper",
        "BB_Middle",
        "BB_Lower",
        "Volume_MA",
    )

    for scenario in SCENARIOS:
        data = scenario_data(scenario)
        klines = data["klines"]
        indicators = strategy._build_indicators(klines)
        for timeframe in ("1h", "4h", "1d"):
            frame = indicators[timeframe]
            out["indicators/{0}/{1}".format(scenario, timeframe)] = {
                field: _norm(_safe_last(indicators, timeframe, field))
                for field in indicator_fields
            }

        # --- 评分三件套 + 综合分（趋势市 / 震荡市两套权重） ---
        out["score/{0}/trend".format(scenario)] = _norm(
            strategy._calculate_trend_strength_score(indicators)
        )
        out["score/{0}/pattern".format(scenario)] = _norm(
            strategy._calculate_pattern_quality_score(indicators, klines)
        )
        out["score/{0}/momentum".format(scenario)] = _norm(
            strategy._calculate_momentum_divergence_score(indicators, klines)
        )
        out["score/{0}/total_strong_trend".format(scenario)] = _norm(
            strategy._calculate_score(indicators, klines, "STRONG_TREND")
        )
        out["score/{0}/total_ranging".format(scenario)] = _norm(
            strategy._calculate_score(indicators, klines, "RANGING")
        )

        # --- 形态检测（逐布尔） ---
        recent = pd.DataFrame(klines["1h"][-5:])
        for col in ("open", "high", "low", "close"):
            recent[col] = pd.to_numeric(recent[col])
        out["pattern/{0}/bullish_engulfing".format(scenario)] = strategy._detect_bullish_engulfing(
            recent
        )
        out["pattern/{0}/bearish_engulfing".format(scenario)] = strategy._detect_bearish_engulfing(
            recent
        )
        out["pattern/{0}/breakout_pullback".format(scenario)] = strategy._detect_breakout_pullback(
            recent, indicators
        )
        out["pattern/{0}/divergence".format(scenario)] = strategy._detect_divergence(
            indicators, klines
        )

        # --- 方向 / 等级 ---
        out["direction/{0}".format(scenario)] = strategy._determine_direction(indicators)
        score_value = strategy._calculate_score(indicators, klines, "STRONG_TREND")
        for symbol in ("BTCUSDT", "SOLUSDT", "XRPUSDT"):
            out["grade/{0}/{1}".format(scenario, symbol)] = strategy._determine_grade(
                score_value, symbol
            )
        out["grade/{0}/no_symbol".format(scenario)] = strategy._determine_grade(score_value)

        # --- 趋势过滤 / 禁止入场 / 成交量过滤 ---
        trend_ok, trend_reason = strategy._check_trend_filter("BTCUSDT", indicators, klines)
        out["trend_filter/{0}".format(scenario)] = [trend_ok, trend_reason]
        out["prohibited/{0}".format(scenario)] = _norm(
            asyncio.run(strategy._check_prohibited_conditions("BTCUSDT", klines))
        )
        for grade in ("S", "A", "B", "C"):
            out["volume_filter/{0}/{1}".format(scenario, grade)] = _norm(
                strategy._check_volume_filter("SOLUSDT", grade, klines, 0.2)
            )

        # --- 震荡侧投票与三机制 ---
        ranging_config = risk["ranging_strategy"]
        entry_conditions = ranging_config["entry_conditions"]
        vote = strategy._vote_ranging_direction(indicators, klines, entry_conditions)
        out["ranging_vote/{0}".format(scenario)] = _norm(vote)
        bb_votes = strategy._bb_touch_votes(
            pd.DataFrame(klines["4h"]), indicators, entry_conditions
        )
        out["bb_touch_votes/{0}".format(scenario)] = _norm(bb_votes)
        out["rsi_extreme_votes/{0}".format(scenario)] = _norm(
            strategy._rsi_extreme_votes(indicators, entry_conditions)
        )
        out["reversal_pattern_votes/{0}".format(scenario)] = _norm(
            strategy._reversal_pattern_votes(pd.DataFrame(klines["4h"]), entry_conditions)
        )
        for direction in ("LONG", "SHORT"):
            out["direction_alignment/{0}/{1}".format(scenario, direction)] = _norm(
                strategy._check_direction_alignment(direction, indicators, ranging_config)
            )
            out["volume_confirm/{0}/{1}".format(scenario, direction)] = _norm(
                strategy._check_volume_confirm(direction, indicators, klines, ranging_config)
            )
            out["overheat/{0}/{1}".format(scenario, direction)] = strategy._evaluate_overheat(
                direction, indicators, klines, ranging_config["overheat_protection"]
            )
            out["overheat_ban/{0}/{1}".format(scenario, direction)] = _norm(
                strategy._check_overheat_ban(direction, indicators, klines, ranging_config)
            )
            for grade in ("S", "A", "B", "C"):
                out[
                    "overheat_downgrade/{0}/{1}/{2}".format(scenario, direction, grade)
                ] = strategy._apply_overheat_downgrade(
                    grade, direction, indicators, klines, ranging_config
                )

        strategy.symbol_extreme_pause_until = {}
        out["vol_regime/{0}".format(scenario)] = _norm(
            strategy._check_volatility_regime("BTCUSDT", indicators, ranging_config)
        )
        strategy.symbol_extreme_pause_until = {}
        out["keep_position/{0}".format(scenario)] = _norm(
            asyncio.run(
                strategy._should_keep_position(
                    "BTCUSDT",
                    types.SimpleNamespace(direction="LONG"),
                    indicators,
                    klines,
                )
            )
        )

    # --- 退出价格计算（各等级 × 方向 × 档位） ---
    for grade in ("S", "A", "B", "C"):
        grade_risk = strategy._get_grade_risk(grade)
        out["grade_risk/{0}".format(grade)] = _norm(
            {
                "stop_mult": grade_risk.get("stop_loss_atr_multiplier", 1.5),
                "tp1": grade_risk["partial_take_profit"]["tp1_atr_multiplier"],
                "tp2": grade_risk["partial_take_profit"]["tp2_atr_multiplier"],
            }
        )
        for direction in ("LONG", "SHORT"):
            entry = Decimal("100")
            atr = Decimal("1.25")
            for level in (1, 2):
                out["tp/{0}/{1}/{2}".format(grade, direction, level)] = _norm(
                    strategy._calculate_tp_price(entry, atr, direction, level, grade)
                )
            position = _position(
                direction=direction,
                entry_price=entry,
                atr=atr,
                grade=grade,
            )
            out["stop/{0}/{1}".format(grade, direction)] = _norm(
                strategy._calculate_stop_price(position)
            )
    out["limit_offset/LONG"] = _norm(
        strategy._apply_limit_offset(Decimal("100"), Decimal("0.002"), "LONG")
    )
    out["limit_offset/SHORT"] = _norm(
        strategy._apply_limit_offset(Decimal("100"), Decimal("0.002"), "SHORT")
    )

    # --- 时间退出决策（按等级 × 若干持仓时长，以 _check_time_stop 的调用轨迹冻结） ---
    for grade in ("S", "A", "B", "C"):
        for hours in (0.0, 50.0, 95.0, 96.0, 120.0, 200.0):
            out["time_stop/{0}/{1}".format(grade, hours)] = _norm(
                asyncio.run(_capture_time_stop(strategy, grade, hours))
            )

    # --- 移动止损价格（波动率调节因子固定为 1.0，去掉 IO 依赖） ---
    async def _fixed_vol_adjustment(symbol, position):
        return 1.0

    trailing_cases = (
        ("LONG", "101.50", "103.00", "1.5", None),
        ("LONG", "99.00", "103.00", "1.2", "100.40"),
        ("SHORT", "102.00", "97.00", "1.5", None),
        ("SHORT", "104.00", "97.00", "1.2", "98.10"),
    )
    for index, (direction, entry, reference, atr, prior) in enumerate(trailing_cases):
        strategy._get_volatility_adjustment = _fixed_vol_adjustment  # type: ignore[assignment]
        position = _position(
            direction=direction,
            entry_price=Decimal(entry),
            atr=Decimal(atr),
            grade="A",
            highest_price=Decimal(reference),
            lowest_price=Decimal(reference),
            trailing_stop_price=Decimal(prior) if prior else None,
        )
        out["trailing/{0}".format(index)] = _norm(
            asyncio.run(
                strategy._calculate_dynamic_trailing_stop(
                    "BTCUSDT", position, Decimal(reference)
                )
            )
        )

    return _norm(out)


def build_core_targets() -> Optional[Dict[str, Any]]:
    """以**等价提取后的 core 纯函数**计算同一组用例；未提取时返回 None。"""
    try:
        from strategies.btc_eth_aggressive.core import common, direction as direction_mod
        from strategies.btc_eth_aggressive.core import exits, filters, scoring
    except Exception:
        return None

    cfg = config_dict()["strategy"]
    scoring_cfg = cfg["scoring"]
    risk = cfg["risk"]
    symbol_cfg = cfg["symbol_config"]
    timeframes = cfg["timeframes"]
    out: Dict[str, Any] = {}

    out["safe_last/missing_timeframe"] = common.safe_last({}, "1h", "RSI")
    out["safe_last/empty_series"] = common.safe_last(
        {"1h": {"RSI": __import__("pandas").Series([])}}, "1h", "RSI"
    )
    out["safe_last/nan_value"] = common.safe_last(
        {"1h": {"RSI": __import__("pandas").Series([1.0, float("nan")])}}, "1h", "RSI"
    )
    out["safe_last_klines/empty"] = common.safe_last_klines_value({}, "1h", "close")
    out["safe_last_klines/ok"] = common.safe_last_klines_value(
        {"1h": _scenario_klines("ranging")["1h"]}, "1h", "close"
    )

    indicator_fields = (
        "MA21",
        "MA55",
        "EMA21",
        "EMA55",
        "RSI",
        "ATR",
        "ATR_long",
        "ADX",
        "MACD",
        "MACD_Hist",
        "BB_Upper",
        "BB_Middle",
        "BB_Lower",
        "Volume_MA",
    )

    import pandas as pd

    for scenario in SCENARIOS:
        data = scenario_data(scenario)
        klines = data["klines"]
        indicators = common.build_indicators(klines, risk)
        for timeframe in ("1h", "4h", "1d"):
            out["indicators/{0}/{1}".format(scenario, timeframe)] = {
                field: _norm(common.safe_last(indicators, timeframe, field))
                for field in indicator_fields
            }

        out["score/{0}/trend".format(scenario)] = _norm(
            scoring.trend_strength_score(indicators, scoring_cfg)
        )
        out["score/{0}/pattern".format(scenario)] = _norm(
            scoring.pattern_quality_score(indicators, klines, scoring_cfg)
        )
        out["score/{0}/momentum".format(scenario)] = _norm(
            scoring.momentum_divergence_score(indicators, klines, scoring_cfg)
        )
        out["score/{0}/total_strong_trend".format(scenario)] = _norm(
            scoring.calculate_score(indicators, klines, scoring_cfg, risk, "STRONG_TREND")
        )
        out["score/{0}/total_ranging".format(scenario)] = _norm(
            scoring.calculate_score(indicators, klines, scoring_cfg, risk, "RANGING")
        )

        recent = pd.DataFrame(klines["1h"][-5:])
        for col in ("open", "high", "low", "close"):
            recent[col] = pd.to_numeric(recent[col])
        out["pattern/{0}/bullish_engulfing".format(scenario)] = scoring.detect_bullish_engulfing(
            recent
        )
        out["pattern/{0}/bearish_engulfing".format(scenario)] = scoring.detect_bearish_engulfing(
            recent
        )
        out["pattern/{0}/breakout_pullback".format(scenario)] = scoring.detect_breakout_pullback(
            recent, indicators, scoring_cfg
        )
        out["pattern/{0}/divergence".format(scenario)] = scoring.detect_divergence(
            indicators, klines
        )

        out["direction/{0}".format(scenario)] = direction_mod.determine_direction(
            indicators, timeframes
        )
        score_value = scoring.calculate_score(indicators, klines, scoring_cfg, risk, "STRONG_TREND")
        for symbol in ("BTCUSDT", "SOLUSDT", "XRPUSDT"):
            out["grade/{0}/{1}".format(scenario, symbol)] = scoring.determine_grade(
                score_value, scoring_cfg, symbol_cfg, symbol
            )
        out["grade/{0}/no_symbol".format(scenario)] = scoring.determine_grade(
            score_value, scoring_cfg, symbol_cfg
        )

        trend_ok, trend_reason = filters.check_trend_filter(indicators, klines, risk)
        out["trend_filter/{0}".format(scenario)] = [trend_ok, trend_reason]
        spike_reason = filters.check_kline_spike(klines, risk["prohibition"])
        out["prohibited/{0}".format(scenario)] = [spike_reason is None, spike_reason or ""]
        for grade in ("S", "A", "B", "C"):
            out["volume_filter/{0}/{1}".format(scenario, grade)] = _norm(
                filters.check_volume_filter("SOLUSDT", grade, klines, risk, symbol_cfg, 0.2)
            )

        ranging_config = risk["ranging_strategy"]
        entry_conditions = ranging_config["entry_conditions"]
        out["ranging_vote/{0}".format(scenario)] = _norm(
            direction_mod.vote_ranging_direction(indicators, klines, entry_conditions)
        )
        out["bb_touch_votes/{0}".format(scenario)] = _norm(
            direction_mod.bb_touch_votes(pd.DataFrame(klines["4h"]), indicators, entry_conditions)
        )
        out["rsi_extreme_votes/{0}".format(scenario)] = _norm(
            direction_mod.rsi_extreme_votes(indicators, entry_conditions)
        )
        out["reversal_pattern_votes/{0}".format(scenario)] = _norm(
            direction_mod.reversal_pattern_votes(
                pd.DataFrame(klines["4h"]), entry_conditions
            )
        )
        for direction in ("LONG", "SHORT"):
            out["direction_alignment/{0}/{1}".format(scenario, direction)] = _norm(
                direction_mod.check_direction_alignment(direction, indicators, ranging_config)
            )
            out["volume_confirm/{0}/{1}".format(scenario, direction)] = _norm(
                filters.check_volume_confirm(direction, indicators, klines, ranging_config)
            )
            out["overheat/{0}/{1}".format(scenario, direction)] = filters.evaluate_overheat(
                direction, indicators, klines, ranging_config["overheat_protection"]
            )
            out["overheat_ban/{0}/{1}".format(scenario, direction)] = _norm(
                filters.check_overheat_ban(direction, indicators, klines, ranging_config)
            )
            for grade in ("S", "A", "B", "C"):
                out[
                    "overheat_downgrade/{0}/{1}/{2}".format(scenario, direction, grade)
                ] = filters.apply_overheat_downgrade(
                    grade, direction, indicators, klines, ranging_config
                )

        vol_ok, vol_reason, _ratio = filters.evaluate_volatility_regime(
            indicators, ranging_config
        )
        out["vol_regime/{0}".format(scenario)] = _norm([vol_ok, vol_reason])
        out["keep_position/{0}".format(scenario)] = _norm(
            filters.should_keep_position("LONG", indicators, klines, risk)
        )

    for grade in ("S", "A", "B", "C"):
        grade_risk = exits.get_grade_risk(grade, risk)
        out["grade_risk/{0}".format(grade)] = _norm(
            {
                "stop_mult": grade_risk.get("stop_loss_atr_multiplier", 1.5),
                "tp1": grade_risk["partial_take_profit"]["tp1_atr_multiplier"],
                "tp2": grade_risk["partial_take_profit"]["tp2_atr_multiplier"],
            }
        )
        for direction in ("LONG", "SHORT"):
            entry = Decimal("100")
            atr = Decimal("1.25")
            for level in (1, 2):
                out["tp/{0}/{1}/{2}".format(grade, direction, level)] = _norm(
                    exits.calculate_tp_price(entry, atr, direction, level, grade, risk)
                )
            out["stop/{0}/{1}".format(grade, direction)] = _norm(
                exits.calculate_stop_price(direction, entry, atr, grade, risk)
            )
    out["limit_offset/LONG"] = _norm(
        exits.apply_limit_offset(Decimal("100"), Decimal("0.002"), "LONG")
    )
    out["limit_offset/SHORT"] = _norm(
        exits.apply_limit_offset(Decimal("100"), Decimal("0.002"), "SHORT")
    )

    for grade in ("S", "A", "B", "C"):
        grade_risk = exits.get_grade_risk(grade, risk)
        for hours in (0.0, 50.0, 95.0, 96.0, 120.0, 200.0):
            out["time_stop/{0}/{1}".format(grade, hours)] = _norm(
                exits.time_stop_decision(hours, grade_risk)
            )

    trailing_cases = (
        ("LONG", "101.50", "103.00", "1.5", None),
        ("LONG", "99.00", "103.00", "1.2", "100.40"),
        ("SHORT", "102.00", "97.00", "1.5", None),
        ("SHORT", "104.00", "97.00", "1.2", "98.10"),
    )
    for index, (direction, entry, reference, atr, prior) in enumerate(trailing_cases):
        out["trailing/{0}".format(index)] = _norm(
            exits.trailing_stop_price(
                direction=direction,
                entry_price=Decimal(entry),
                reference_price=Decimal(reference),
                retrace_ratio=_trailing_retrace_ratio(entry, reference),
                vol_adjustment=1.0,
                hard_stop_multiplier=exits.get_grade_risk("A", risk).get(
                    "stop_loss_atr_multiplier", 1.5
                ),
                atr=Decimal(atr),
                prior_trailing=Decimal(prior) if prior else None,
            )
        )

    return _norm(out)


def _trailing_retrace_ratio(entry: str, reference: str) -> float:
    """按冻结配置的回归阶梯，由参考价推出应命中的档位回撤比例。"""
    tiers = frozen_config()["risk"]["signal_levels"]["A"].get("dynamic_trailing", {}).get(
        "regression_tiers"
    )
    if not tiers:
        # 冻结配置未包含 dynamic_trailing 时使用 A 级公开阶梯（与 config.yaml 一致）
        tiers = [
            {"profit_ceiling": 1.5, "retrace_ratio": 0.0},
            {"profit_ceiling": 4.0, "retrace_ratio": 0.5},
            {"profit_ceiling": 8.0, "retrace_ratio": 0.35},
            {"profit_ceiling": 999.0, "retrace_ratio": 0.25},
        ]
    entry_dec = Decimal(entry)
    reference_dec = Decimal(reference)
    pct = float((reference_dec - entry_dec) / entry_dec) * 100
    if pct < 0:
        pct = 0.0
    for tier in tiers:
        if pct < float(tier["profit_ceiling"]):
            return float(tier["retrace_ratio"])
    return float(tiers[-1]["retrace_ratio"])


# ---------------------------------------------------------------------------
# 期望值文件读写
# ---------------------------------------------------------------------------


def write_expected(payload: Dict[str, Any]) -> None:
    with open(EXPECTED_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def read_expected() -> Dict[str, Any]:
    with open(EXPECTED_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)
