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


# ==========================================
# V2.8: EMA 斜率检查测试
# ==========================================


def make_klines_with_ema_slope(slope: float, base_price: float = 100.0, count: int = 30) -> list:
    """
    构造具有特定 EMA20 斜率的 4h K 线数据

    EMA20 的斜率由价格增长速率决定。
    大致关系：若每根 K 线涨幅为 r，则 EMA20 斜率 ≈ r

    Args:
        slope: 目标 EMA20 斜率（正数=上行，负数=下行）
        base_price: 起始价格
        count: K 线数量

    Returns:
        K 线列表
    """
    klines = []
    price = base_price
    # 每根 K 线的价格变化率
    price_change_per_kline = slope * 1.5  # 略放大以补偿 EMA 平滑效应
    for i in range(count):
        if i > 0:
            price = price * (1 + price_change_per_kline)
        close = price
        open_p = price * (1 - price_change_per_kline * 0.5)
        high = max(open_p, close) * 1.005
        low = min(open_p, close) * 0.995
        volume = 1000 + i * 10
        klines.append(make_kline(open_p, high, low, close, volume))
    return klines


class TestEmaSlopeCheck:
    """V2.8: EMA 斜率检查功能测试"""

    @pytest.fixture
    def engine(self):
        return ScoringEngine(CONFIG)

    def test_计算EMA斜率_上行趋势(self, engine):
        """验证上行趋势中 EMA 斜率为正"""
        klines = make_klines_with_ema_slope(0.002, base_price=100.0, count=30)
        slope = engine._calc_ema_slope(klines, period=20, slope_period=3)
        assert slope > 0.0001, f"上行趋势斜率应为正，实际: {slope}"

    def test_计算EMA斜率_下行趋势(self, engine):
        """验证下行趋势中 EMA 斜率为负"""
        klines = make_klines_with_ema_slope(-0.002, base_price=100.0, count=30)
        slope = engine._calc_ema_slope(klines, period=20, slope_period=3)
        assert slope < -0.0001, f"下行趋势斜率应为负，实际: {slope}"

    def test_计算EMA斜率_数据不足返回0(self, engine):
        """验证 K 线数据不足时返回 0.0"""
        klines = make_klines_with_ema_slope(0.001, base_price=100.0, count=5)  # 远少于 period + slope_period
        slope = engine._calc_ema_slope(klines, period=20, slope_period=3)
        assert slope == 0.0, f"数据不足时应返回 0.0，实际: {slope}"

    def test_斜率检查_做多阻断_EMA下行(self, engine):
        """
        验证：EMA 斜率下行（负值）且低于做多阈值时，做多应被阻断

        模拟场景：EMA20 显著下行，做多应被阻断
        """
        klines = make_klines_with_ema_slope(-0.003, base_price=100.0, count=30)

        ok, reason = engine._check_ema_slope(
            direction="long",
            klines_4h=klines,
            ema_period=20,
        )

        # 斜率应 < min_slope_for_long (-0.0005)，做多应被阻断
        assert ok is False, "EMA 下行趋势中做多应被阻断"
        assert "趋势不支持做多" in reason, f"阻断原因错误: {reason}"

    def test_斜率检查_做空阻断_EMA上行(self, engine):
        """
        验证：EMA 斜率上行（正值）且高于做空阈值时，做空应被阻断

        模拟场景：EMA20 显著上行，做空应被阻断
        """
        klines = make_klines_with_ema_slope(0.003, base_price=100.0, count=30)

        ok, reason = engine._check_ema_slope(
            direction="short",
            klines_4h=klines,
            ema_period=20,
        )

        # 斜率应 > max_slope_for_short (0.0005)，做空应被阻断
        assert ok is False, "EMA 上行趋势中做空应被阻断"
        assert "趋势不支持做空" in reason, f"阻断原因错误: {reason}"

    def test_斜率检查_做多通过_EMA走平(self, engine):
        """
        验证：EMA 斜率接近 0（走平/轻微上下）时，做多应通过
        """
        klines = make_klines_with_ema_slope(0.0001, base_price=100.0, count=30)

        ok, reason = engine._check_ema_slope(
            direction="long",
            klines_4h=klines,
            ema_period=20,
        )

        assert ok is True, f"EMA 走平时做多应通过，被阻断: {reason}"

    def test_斜率检查_做空通过_EMA走平(self, engine):
        """
        验证：EMA 斜率接近 0（走平/轻微上下）时，做空应通过
        """
        klines = make_klines_with_ema_slope(-0.0001, base_price=100.0, count=30)

        ok, reason = engine._check_ema_slope(
            direction="short",
            klines_4h=klines,
            ema_period=20,
        )

        assert ok is True, f"EMA 走平时做空应通过，被阻断: {reason}"

    def test_斜率检查_功能关闭时放行(self, engine):
        """验证 _ema_slope_enabled=False 时跳过检查"""
        engine._ema_slope_enabled = False
        ok, reason = engine._check_ema_slope(
            direction="long",
            klines_4h=None,
            ema_period=20,
        )
        assert ok is True, "功能关闭时应放行"

    def test_斜率检查_数据为None时放行(self, engine):
        """验证 klines_4h 为 None 时跳过检查"""
        ok, reason = engine._check_ema_slope(
            direction="long",
            klines_4h=None,
            ema_period=20,
        )
        assert ok is True, "数据为 None 时应放行"

    def test_标准模式_EMA斜率阻断做多(self, engine):
        """
        集成测试：标准模式中，EMA 斜率下行应阻断做多

        验证 _check_standard_trend_filter 中 EMA 斜率检查与价格偏离检查的协作。
        场景：价格高于 EMA20（价格偏离通过），但 EMA20 斜率下行（斜率检查阻断）。
        数据构造：先拉升后大幅回落，使价格仍高于 EMA20 但 EMA 斜率已转负。
        """
        # 构造：15根平盘(100) → 5根拉升(100→116) → 5根回落(116→95)
        klines = []
        price = 100.0
        for i in range(15):
            klines.append(make_kline(price, price * 1.005, price * 0.995, price, 1000))
        # 5根拉升：每次 +3%
        for i in range(5):
            price = price * 1.03
            klines.append(make_kline(price * 0.99, price * 1.005, price * 0.99, price, 2000))
        # 5根回落：每次 -4%（从高位快速回落）
        peak = price
        for i in range(5):
            price = max(peak * (1 - 0.04 * (i + 1)), 95.0)
            klines.append(make_kline(price * 0.99, price * 1.005, price * 0.99, price, 1500))

        current_close = float(klines[-1]["close"])
        ema_4h = engine._calc_ema(klines, 20)
        slope = engine._calc_ema_slope(klines, period=20, slope_period=3)

        # 如果价格高于 EMA20 但斜率不支持做多，验证阻断
        if current_close > ema_4h * 0.99 and slope < engine._ema_slope_min_for_long:
            ok, reason = engine._check_standard_trend_filter(
                direction="long",
                current_price_4h=current_close,
                ema_4h=ema_4h,
                klines_4h=klines,
            )
            assert ok is False, "EMA 下行趋势中做多应被阻断"
            assert "斜率" in reason, f"阻断原因应为 EMA 斜率检查，实际: {reason}"
        else:
            # 如果价格低于 EMA20（第一层过滤阻断），验证阻断原因包含趋势信息
            ok, reason = engine._check_standard_trend_filter(
                direction="long",
                current_price_4h=current_close,
                ema_4h=ema_4h,
                klines_4h=klines,
            )
            assert ok is False, "下行趋势中做多应被阻断"
            # 至少验证 EMA 斜率计算正确
            assert slope < 0, f"EMA 斜率应为负，实际: {slope}"


class TestTrendFilterReversalMode:
    """V2.8-FIX: 反转模式趋势过滤测试"""

    @pytest.fixture
    def engine(self):
        return ScoringEngine(CONFIG)

    def test_反转模式做多_价格低于EMA20_允许(self, engine):
        """反转模式：价格低于 EMA20 时允许做多"""
        config = {
            "enabled": True,
            "long": {"min_price": 1.02, "max_deviation": 0.96},
            "short": {"max_price": 0.98, "max_deviation": 1.04},
        }
        ema_4h = 100.0
        current_price = 99.0  # 低于 EMA20，反转做多区域
        ok, reason = engine._check_lv_rm_trend_filter(
            direction="long",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            config=config,
            reversal_mode=True,
        )
        assert ok is True, f"反转模式：价格低于 EMA20 应允许做多，被阻断: {reason}"

    def test_反转模式做多_价格过高_阻断(self, engine):
        """反转模式：价格高于 EMA20*1.02 时阻断做多"""
        config = {
            "enabled": True,
            "long": {"min_price": 1.02, "max_deviation": 0.96},
        }
        ema_4h = 100.0
        current_price = 103.0  # 高于 EMA20*1.02，太高了
        ok, reason = engine._check_lv_rm_trend_filter(
            direction="long",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            config=config,
            reversal_mode=True,
        )
        assert ok is False, "反转模式：价格过高应阻断做多"

    def test_反转模式做空_价格高于EMA20_允许(self, engine):
        """反转模式：价格高于 EMA20 时允许做空"""
        config = {
            "enabled": True,
            "short": {"max_price": 0.98, "max_deviation": 1.04},
        }
        ema_4h = 100.0
        current_price = 101.0  # 高于 EMA20，反转做空区域
        ok, reason = engine._check_lv_rm_trend_filter(
            direction="short",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            config=config,
            reversal_mode=True,
        )
        assert ok is True, f"反转模式：价格高于 EMA20 应允许做空，被阻断: {reason}"

    def test_反转模式做空_价格过低_阻断(self, engine):
        """反转模式：价格低于 EMA20*0.98 时阻断做空"""
        config = {
            "enabled": True,
            "short": {"max_price": 0.98, "max_deviation": 1.04},
        }
        ema_4h = 100.0
        current_price = 97.0  # 低于 EMA20*0.98，太低了
        ok, reason = engine._check_lv_rm_trend_filter(
            direction="short",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            config=config,
            reversal_mode=True,
        )
        assert ok is False, "反转模式：价格过低应阻断做空"

    # ==========================================
    # V2.8-FIX2: 反转模式跳过 EMA 斜率检查
    # ==========================================

    def test_反转模式_EMA斜率检查_做多跳过(self, engine):
        """
        V2.8-FIX2: 反转模式下 EMA 斜率检查应被跳过

        验证：反转模式做多，价格在反转做多范围内（0.97~1.01倍EMA20），
        即使EMA斜率显著下行，趋势过滤也应通过。

        注：current_price_4h 和 ema_4h 由测试直接指定（价格偏离检查用），
        klines_4h 仅用于 EMA 斜率计算。构造强下行趋势的K线数据，
        验证反转模式下斜率检查被跳过。
        """
        ema_4h = 100.0
        current_price = 99.5  # 在反转做多范围内 (0.995倍EMA20)

        # 构造强下行趋势的K线（EMA斜率应显著为负）
        klines = make_klines_with_ema_slope(-0.003, base_price=100.0, count=30)

        # 验证：反转模式下，即使EMA斜率显著下行，趋势过滤也应通过
        ok, reason = engine._check_standard_trend_filter(
            direction="long",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            klines_4h=klines,
        )

        assert ok is True, f"反转模式：做多应跳过 EMA 斜率检查，被阻断: {reason}"

    def test_反转模式_EMA斜率检查_做空跳过(self, engine):
        """
        V2.8-FIX2: 反转模式下 EMA 斜率检查应被跳过

        验证：反转模式做空，价格在反转做空范围内（0.99~1.03倍EMA20），
        即使EMA斜率显著上行，趋势过滤也应通过。
        """
        ema_4h = 100.0
        current_price = 101.0  # 在反转做空范围内 (1.01倍EMA20)

        # 构造强上行趋势的K线（EMA斜率应显著为正）
        klines = make_klines_with_ema_slope(0.003, base_price=100.0, count=30)

        ok, reason = engine._check_standard_trend_filter(
            direction="short",
            current_price_4h=current_price,
            ema_4h=ema_4h,
            klines_4h=klines,
        )

        assert ok is True, f"反转模式：做空应跳过 EMA 斜率检查，被阻断: {reason}"

    def test_标准模式_EMA斜率检查_仍然生效(self, engine):
        """
        V2.8-FIX2: 标准模式（非反转）下 EMA 斜率检查仍然生效

        验证：当 reversal_mode=False 时，EMA 斜率检查正常工作。
        """
        # 临时关闭反转模式
        engine.trend_filter_reversal_mode = False

        # 构造下行趋势的 4h K 线数据
        klines = make_klines_with_ema_slope(-0.003, base_price=100.0, count=30)
        slope = engine._calc_ema_slope(klines, period=20, slope_period=3)

        # 做多：EMA 斜率显著下行，应被阻断
        ok, reason = engine._check_ema_slope(
            direction="long",
            klines_4h=klines,
            ema_period=20,
        )

        assert ok is False, "标准模式：EMA 下行应阻断做多"
        assert "趋势不支持做多" in reason, f"阻断原因错误: {reason}"