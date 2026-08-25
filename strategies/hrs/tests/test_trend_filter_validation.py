"""
趋势过滤验证测试

验证目标：在连续上涨行情中，标准模式会产生做空信号，
但 4h EMA20 趋势过滤应能正确阻断这些做空信号。

测试流程：
1. 模拟连续上涨的 4h K 线数据（价格远高于 EMA20）
2. 模拟"顶部形态"做空信号（当前标准模式会通过）
3. 验证趋势过滤阻断做空、允许做多
"""
import pytest
import yaml
from pathlib import Path

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

from strategies.hrs.scoring_engine import ScoringEngine, ScoringResult


def make_kline(open_p, high, low, close, volume):
    """创建模拟K线数据"""
    return {
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def make_4h_uptrend_klines(base_price: float, count: int = 20) -> list:
    """
    构造连续上涨的 4h K 线数据

    价格从 base_price 持续上涨，每根 K 线涨幅约 1.5%，
    模拟强劲的单边上涨行情。
    """
    klines = []
    price = base_price
    for i in range(count):
        close = price * (1 + 0.015)  # 每根涨 1.5%
        open_p = price
        high = close * 1.005  # 略高于收盘
        low = price * 0.995   # 略低于开盘
        volume = 1000 * (1 + i * 0.05)  # 成交量递增
        klines.append(make_kline(open_p, high, low, close, volume))
        price = close
    return klines


def make_top_pattern_klines(peak_price: float, count: int = 5) -> list:
    """
    构造"见顶形态"的 1h K 线数据

    在 peak_price 附近构造多次冲顶失败 + 长上影线，
    模拟标准模式会检测到的做空形态。
    """
    # 前两根：大幅拉升后出现长上影线
    klines = [
        make_kline(peak_price * 0.98, peak_price * 1.02, peak_price * 0.97, peak_price * 1.01, 2000),
        make_kline(peak_price * 1.01, peak_price * 1.03, peak_price * 0.98, peak_price * 0.99, 3000),
    ]
    # 后三根：高点逐步降低，形成三次冲顶
    for i in range(3):
        high = peak_price * (1.01 - i * 0.005)
        close = peak_price * (0.99 - i * 0.003)
        klines.append(make_kline(
            peak_price * 0.99, high, peak_price * 0.98, close, 2500
        ))
    return klines


class TestTrendFilterConcept:
    """验证趋势过滤概念的正确性"""

    @pytest.fixture
    def engine(self):
        return ScoringEngine(CONFIG)

    @pytest.fixture
    def lv_rm_trend_config(self):
        """从配置中读取 LV-RM 趋势过滤配置"""
        return CONFIG.get("lv_rm", {}).get("entry", {}).get("trend_filter", {})

    # ==========================================
    # 场景A：当前标准模式行为（对照）
    # ==========================================

    def test_标准模式在见顶形态下产生做空信号(self, engine):
        """
        当前标准模式行为：形态检测到顶部 → 开空

        这是在连续上涨行情中可能出现的问题场景：
        标准模式只看局部形态，不关心大趋势方向。
        """
        # 构造顶部形态的 patterns
        patterns = {
            "three_tops": (True, 4.0),          # 三次冲顶满分
            "long_upper_shadow": (True, 3.0),    # 长上影线满分
            "volume_stagnation": (True, 3.0),    # 放量滞涨满分
        }

        result = engine.score(
            symbol="TESTUSDT",
            direction="short",
            oi_market_cap_ratio=0.25,   # 中高 OI/市值比
            patterns=patterns,
            funding_rate=0.001,          # 正费率，支持做空
            has_market_cap=True,
        )

        # 标准模式：总分高，技术分高，应通过 should_entry
        assert result.total_score >= 6.0, f"总分 {result.total_score} 应 ≥ 6.0"
        assert result.technical_score >= 6.0, f"技术分 {result.technical_score} 应 ≥ 6.0"
        assert result.veto is False
        assert engine.should_entry(result) is True, \
            "当前标准模式：见顶形态下应允许做空（对照行为）"

    # ==========================================
    # 场景B：趋势过滤阻断做空
    # ==========================================

    def test_趋势过滤在连续上涨中阻断做空(self, engine, lv_rm_trend_config):
        """
        验证趋势过滤：在连续上涨行情中，做空信号应被阻断

        模拟场景：4h EMA20 = 100，当前价格 = 108（远高于 EMA20）
        做空条件要求价格 < EMA20（空头排列），但价格远高于 EMA20，
        所以做空应被否决。
        """
        ema_4h = 100.0
        current_price = 108.0  # 价格远高于 EMA20（上涨趋势中）

        # 直接调用现有的 _check_lv_rm_trend_filter 方法
        ok, reason = engine._check_lv_rm_trend_filter(
            direction="short",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            config=lv_rm_trend_config,
        )

        assert ok is False, f"趋势过滤应阻断做空，但返回了通过"
        assert "不做空" in reason or "摸顶" in reason, \
            f"阻断原因应包含'不做空'或'摸顶'，实际: {reason}"

    def test_趋势过滤在连续上涨中允许做多(self, engine, lv_rm_trend_config):
        """
        验证趋势过滤：在连续上涨行情中，做多信号应被允许

        模拟场景：4h EMA20 = 100，当前价格 = 108（远高于 EMA20）
        做多条件要求价格 > EMA20（多头排列），价格远高于 EMA20，
        所以做多应通过。
        """
        ema_4h = 100.0
        current_price = 108.0  # 价格远高于 EMA20（上涨趋势中）

        ok, reason = engine._check_lv_rm_trend_filter(
            direction="long",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            config=lv_rm_trend_config,
        )

        assert ok is True, f"趋势过滤应允许做多，但被阻断: {reason}"

    # ==========================================
    # 场景C：趋势过滤不阻断合理做空
    # ==========================================

    def test_趋势过滤在下跌趋势中允许做空(self, engine, lv_rm_trend_config):
        """
        验证趋势过滤：在下跌趋势中，做空信号应被允许

        模拟场景：4h EMA20 = 100，当前价格 = 95（低于 EMA20，空头排列）
        做空条件要求价格 < EMA20，满足条件，应通过。
        """
        ema_4h = 100.0
        current_price = 95.0  # 价格低于 EMA20（下跌趋势中）

        ok, reason = engine._check_lv_rm_trend_filter(
            direction="short",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            config=lv_rm_trend_config,
        )

        assert ok is True, f"趋势过滤应允许做空（下跌趋势），但被阻断: {reason}"

    # ==========================================
    # 场景D：完整场景验证 - 标准模式+趋势过滤
    # ==========================================

    def test_完整场景_连续上涨行情做空被阻断(self, engine, lv_rm_trend_config):
        """
        完整场景：在连续上涨行情中

        第一步：标准模式检测到顶部形态，产生做空信号
        第二步：趋势过滤检查，发现价格远高于 4h EMA20，阻断做空
        """
        # ---- 第一步：标准模式产生做空信号 ----
        patterns = {
            "three_tops": (True, 4.0),
            "long_upper_shadow": (True, 3.0),
            "volume_stagnation": (True, 3.0),
        }
        score_result = engine.score(
            symbol="TESTUSDT",
            direction="short",
            oi_market_cap_ratio=0.25,
            patterns=patterns,
            funding_rate=0.001,
            has_market_cap=True,
        )

        # 标准模式允许入场
        assert engine.should_entry(score_result) is True, \
            "标准模式应允许入场（对照行为）"

        # ---- 第二步：趋势过滤阻断 ----
        ema_4h = 100.0
        current_price = 108.0  # 上涨趋势中

        ok, reason = engine._check_lv_rm_trend_filter(
            direction="short",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            config=lv_rm_trend_config,
        )

        assert ok is False, \
            f"趋势过滤应阻断做空，实际: {reason}"

        # 验证：标准模式允许入场 + 趋势过滤阻断 = 最终不应入场
        should_final = engine.should_entry(score_result) and ok
        assert should_final is False, \
            "最终判断：趋势过滤阻断后，不应入场"

    def test_完整场景_下跌趋势做空通过(self, engine, lv_rm_trend_config):
        """
        完整场景：在下跌趋势中

        第一步：标准模式检测到顶部形态，产生做空信号（合理）
        第二步：趋势过滤检查，发现价格低于 4h EMA20，允许做空
        """
        patterns = {
            "three_tops": (True, 4.0),
            "long_upper_shadow": (True, 3.0),
            "volume_stagnation": (True, 3.0),
        }
        score_result = engine.score(
            symbol="TESTUSDT",
            direction="short",
            oi_market_cap_ratio=0.25,
            patterns=patterns,
            funding_rate=0.001,
            has_market_cap=True,
        )

        assert engine.should_entry(score_result) is True, \
            "标准模式应允许入场"

        # 下跌趋势：价格低于 EMA20
        ema_4h = 100.0
        current_price = 95.0

        ok, reason = engine._check_lv_rm_trend_filter(
            direction="short",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            config=lv_rm_trend_config,
        )

        assert ok is True, \
            f"趋势过滤应允许做空（下跌趋势），实际被阻断: {reason}"

        # 最终判断：允许入场
        should_final = engine.should_entry(score_result) and ok
        assert should_final is True, \
            "最终判断：趋势过滤通过后，应允许入场"