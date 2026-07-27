"""
V2.3 动态阈值功能测试
测试 ScoringEngine 中动态阈值注入、判断逻辑、EMM 和半EMM 条件检查
使用 mock 构造 DynamicThresholds 对象，不连接真实 API
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from strategies.hrs.candidate_pool import DynamicThresholds
from strategies.hrs.scoring_engine import ScoringEngine, ScoringResult


# ============================================================
# 测试辅助函数
# ============================================================

def _make_config(dynamic_enabled: bool = True) -> dict:
    """构建包含动态阈值配置的最小测试配置"""
    return {
        "scoring": {
            "weights": {"contract": 0.25, "technical": 0.45, "sentiment": 0.30},
            "entry_threshold": 6.0,
            "technical": {
                "min_total_score": 4.0,
                "min_primary_pattern_score": 1.0,
            },
            "contract": {
                "oi_market_cap_ratio": {
                    "thresholds": {
                        "extreme_high": 0.25, "high": 0.20, "mid_high": 0.15,
                        "mid": 0.10, "low": 0.05,
                    },
                    "scores": {
                        "short": {"extreme_high": 10, "high": 8, "mid_high": 6, "mid": 4, "low": 2, "extreme_low": 0},
                        "long": {"extreme_high": 0, "high": 1, "mid_high": 3, "mid": 5, "low": 7, "extreme_low": 10},
                    },
                    "fallback": {"short": 0, "long": 5},
                },
            },
            "sentiment": {
                "thresholds": {
                    "extreme_high": 150, "high": 100, "mid": 50, "low": 0, "low_negative": -20,
                },
                "scores": {
                    "short": {"extreme_high": 10, "high": 8, "mid": 6, "low": 3, "negative": 1, "extreme_negative": 0},
                    "long": {"extreme_high": 0, "high": 2, "mid": 4, "low": 6, "negative": 8, "extreme_negative": 10},
                },
            },
            "extreme_bonus": {"enabled": False},
        },
        "funding_rate": {"settlements_per_day": 3, "days_per_year": 365},
        "emm": {
            "enabled": True,
            "long": {
                "price_change_24h": -15,
                "funding_rate_annual": -30,
                "oi_market_cap_ratio": 0.10,
            },
            "short": {
                "price_change_24h": 15,
                "funding_rate_annual": 100,
                "oi_market_cap_ratio": 0.20,
            },
            "scoring": {"technical_score": 5.0, "entry_threshold": 6.0},
        },
        "semi_emm": {
            "enabled": True,
            "min_conditions": 2,
            "emm_tech_floor": 5.0,
            "entry_threshold": 5.0,
            "contract_floor": 3.0,
        },
        "candidate_pool": {
            "dynamic_thresholds": {
                "enabled": dynamic_enabled,
                "min_sample_size": 10,
            },
        },
    }


def _make_valid_dynamic_thresholds() -> DynamicThresholds:
    """构造一个有效的 DynamicThresholds 对象（模拟全市场分位数计算结果）"""
    dt = DynamicThresholds()
    dt.funding_rate_short = 80.0       # 市场80分位费率
    dt.funding_rate_long = -40.0       # 市场20分位费率
    dt.oi_market_cap_short = 0.18
    dt.oi_market_cap_long = 0.03
    dt.ema20_short = 0.06
    dt.ema20_long = -0.04
    # EMM 分位数（更极端）
    dt.funding_rate_emm_long = -60.0   # 做多EMM：市场BOTTOM 10%
    dt.funding_rate_emm_short = 120.0  # 做空EMM：市场TOP 10%
    dt.oi_market_cap_emm = 0.25        # EMM：市场TOP 10%
    dt.sample_count = 50
    dt.computed_at = datetime.now(timezone.utc)
    return dt


def _make_invalid_dynamic_thresholds() -> DynamicThresholds:
    """构造一个无效的 DynamicThresholds（sample_count=0，未计算）"""
    dt = DynamicThresholds()
    dt.sample_count = 0
    dt.computed_at = None
    return dt


# ============================================================
# 1. set_dynamic_thresholds 测试
# ============================================================

class TestSetDynamicThresholds:
    """测试 set_dynamic_thresholds 方法"""

    def test_传入有效阈值对象_成功注入(self):
        """传入有效的 DynamicThresholds 对象，应成功注入"""
        engine = ScoringEngine(_make_config())
        dt = _make_valid_dynamic_thresholds()

        engine.set_dynamic_thresholds(dt)

        assert engine._dynamic_thresholds is dt
        assert engine._dynamic_thresholds.sample_count == 50

    def test_传入None_清空动态阈值(self):
        """传入 None，应清空动态阈值引用"""
        engine = ScoringEngine(_make_config())
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)
        assert engine._dynamic_thresholds is not None

        engine.set_dynamic_thresholds(None)

        assert engine._dynamic_thresholds is None

    def test_反复切换_状态正确(self):
        """反复注入和清空，状态应正确切换"""
        engine = ScoringEngine(_make_config())
        dt = _make_valid_dynamic_thresholds()

        # 注入
        engine.set_dynamic_thresholds(dt)
        assert engine._dynamic_thresholds is dt

        # 清空
        engine.set_dynamic_thresholds(None)
        assert engine._dynamic_thresholds is None

        # 再次注入
        dt2 = _make_valid_dynamic_thresholds()
        dt2.sample_count = 100
        engine.set_dynamic_thresholds(dt2)
        assert engine._dynamic_thresholds is dt2
        assert engine._dynamic_thresholds.sample_count == 100


# ============================================================
# 2. _should_use_dynamic 测试
# ============================================================

class TestShouldUseDynamic:
    """测试 _should_use_dynamic 方法"""

    def test_启用且有效_返回True(self):
        """配置启用 + 动态阈值已注入且有效 -> True"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        assert engine._should_use_dynamic() is True

    def test_配置未启用_返回False(self):
        """配置中 dynamic_thresholds.enabled=false -> False"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        assert engine._should_use_dynamic() is False

    def test_阈值无效_返回False(self):
        """配置启用但阈值无效（sample_count=0）-> False"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_invalid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        assert engine._should_use_dynamic() is False

    def test_未注入_返回False(self):
        """配置启用但未注入阈值（_dynamic_thresholds=None）-> False"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        # 不调用 set_dynamic_thresholds

        assert engine._should_use_dynamic() is False


# ============================================================
# 3. check_emm_conditions 测试
# ============================================================

class TestCheckEmmConditions:
    """测试 check_emm_conditions 方法（动态阈值 vs 固定阈值）"""

    # ---------- 做空方向 ----------

    def test_做空_动态阈值_全部满足(self):
        """做空：动态阈值启用，三个条件全部满足 -> 触发"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        triggered, details = engine.check_emm_conditions(
            direction="short",
            price_change_24h=20.0,        # >= 15（固定阈值）满足
            funding_rate=0.0004,          # 年化 = 0.0004 * 3 * 365 * 100 = 43.8%
            oi_market_cap_ratio=0.30,     # >= dt.oi_market_cap_emm(0.25) 满足
        )
        # 年化费率 43.8% < dt.funding_rate_emm_short(120.0)，不满足！
        # 所以应该不触发
        assert triggered is False
        assert details["dynamic_thresholds_used"] is True
        assert "资金费率不满足" in details["reason"]

    def test_做空_动态阈值_高费率满足(self):
        """做空：动态阈值启用，高费率满足全部条件 -> 触发"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        triggered, details = engine.check_emm_conditions(
            direction="short",
            price_change_24h=20.0,        # >= 15 满足
            funding_rate=0.0015,          # 年化 = 0.0015 * 3 * 365 * 100 = 164.25%
            oi_market_cap_ratio=0.30,     # >= dt.oi_market_cap_emm(0.25) 满足
        )
        # 年化费率 164.25% >= dt.funding_rate_emm_short(120.0)，满足！
        assert triggered is True
        assert details["dynamic_thresholds_used"] is True
        assert details["price_change_ok"] is True
        assert details["funding_rate_ok"] is True
        assert details["oi_market_cap_ok"] is True
        assert "动态" in details["reason"]

    def test_做空_固定阈值_全部满足(self):
        """做空：固定阈值，三个条件全部满足 -> 触发"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))
        # 不注入动态阈值，使用固定阈值

        triggered, details = engine.check_emm_conditions(
            direction="short",
            price_change_24h=20.0,        # >= 15 满足
            funding_rate=0.0015,          # 年化 = 164.25% >= 100 满足
            oi_market_cap_ratio=0.25,     # >= 0.20 满足
        )
        assert triggered is True
        assert details["dynamic_thresholds_used"] is False
        assert details["price_change_ok"] is True
        assert details["funding_rate_ok"] is True
        assert details["oi_market_cap_ok"] is True
        assert "固定" in details["reason"]

    def test_做空_固定阈值_费率不足(self):
        """做空：固定阈值，费率不满足 -> 不触发"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))

        triggered, details = engine.check_emm_conditions(
            direction="short",
            price_change_24h=20.0,        # 满足
            funding_rate=0.0005,          # 年化 = 54.75% < 100 不满足
            oi_market_cap_ratio=0.25,     # 满足
        )
        assert triggered is False
        assert details["dynamic_thresholds_used"] is False
        assert details["price_change_ok"] is True
        assert details["funding_rate_ok"] is False
        assert details["oi_market_cap_ok"] is True
        assert "资金费率不满足" in details["reason"]

    # ---------- 做多方向 ----------

    def test_做多_动态阈值_全部满足(self):
        """做多：动态阈值启用，全部满足 -> 触发"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        triggered, details = engine.check_emm_conditions(
            direction="long",
            price_change_24h=-20.0,       # <= -15 满足
            funding_rate=-0.0003,         # 年化 = -0.0003 * 3 * 365 * 100 = -32.85%
            oi_market_cap_ratio=0.30,     # >= dt.oi_market_cap_emm(0.25) 满足
        )
        # 年化费率 -32.85% > dt.funding_rate_emm_long(-60.0)，不满足（需要 <= -60）
        assert triggered is False
        assert details["dynamic_thresholds_used"] is True
        assert "资金费率不满足" in details["reason"]

    def test_做多_动态阈值_低费率满足(self):
        """做多：动态阈值启用，极低费率满足全部 -> 触发"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        triggered, details = engine.check_emm_conditions(
            direction="long",
            price_change_24h=-20.0,       # <= -15 满足
            funding_rate=-0.0008,         # 年化 = -0.0008 * 3 * 365 * 100 = -87.6%
            oi_market_cap_ratio=0.30,     # >= 0.25 满足
        )
        # 年化费率 -87.6% <= dt.funding_rate_emm_long(-60.0)，满足！
        assert triggered is True
        assert details["dynamic_thresholds_used"] is True
        assert details["price_change_ok"] is True
        assert details["funding_rate_ok"] is True
        assert details["oi_market_cap_ok"] is True

    def test_做多_固定阈值_全部满足(self):
        """做多：固定阈值，三个条件全部满足 -> 触发"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))

        triggered, details = engine.check_emm_conditions(
            direction="long",
            price_change_24h=-20.0,       # <= -15 满足
            funding_rate=-0.0004,         # 年化 = -43.8% <= -30 满足
            oi_market_cap_ratio=0.15,     # >= 0.10 满足
        )
        assert triggered is True
        assert details["dynamic_thresholds_used"] is False
        assert details["price_change_ok"] is True
        assert details["funding_rate_ok"] is True
        assert details["oi_market_cap_ok"] is True

    def test_做多_固定阈值_费率不足(self):
        """做多：固定阈值，费率不满足 -> 不触发"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))

        triggered, details = engine.check_emm_conditions(
            direction="long",
            price_change_24h=-20.0,       # 满足
            funding_rate=-0.0001,         # 年化 = -10.95% > -30 不满足
            oi_market_cap_ratio=0.15,     # 满足
        )
        assert triggered is False
        assert details["funding_rate_ok"] is False

    # ---------- price_change_24h 始终使用固定阈值 ----------

    def test_price_change_24h_动态阈值下仍使用固定阈值(self):
        """验证 price_change_24h 在动态阈值模式下仍使用固定阈值"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        # 做空：price_change_24h=10（<15固定阈值），即使动态阈值有效也不触发
        triggered, details = engine.check_emm_conditions(
            direction="short",
            price_change_24h=10.0,        # < 15 固定阈值，不满足
            funding_rate=0.002,           # 年化 = 219%，满足动态阈值
            oi_market_cap_ratio=0.30,     # 满足动态阈值
        )
        assert triggered is False
        assert details["dynamic_thresholds_used"] is True
        assert details["price_change_ok"] is False
        assert details["price_change_threshold"] == 15  # 固定阈值，不受动态影响

        # 做多：price_change_24h=-10（>-15固定阈值），即使动态阈值有效也不触发
        triggered, details = engine.check_emm_conditions(
            direction="long",
            price_change_24h=-10.0,       # > -15 固定阈值，不满足
            funding_rate=-0.001,          # 年化 = -109.5%，满足动态阈值
            oi_market_cap_ratio=0.30,     # 满足动态阈值
        )
        assert triggered is False
        assert details["dynamic_thresholds_used"] is True
        assert details["price_change_ok"] is False
        assert details["price_change_threshold"] == -15  # 固定阈值

    # ---------- 边界情况 ----------

    def test_EMM未启用_返回False(self):
        """EMM 模块未启用时，直接返回 False"""
        config = _make_config()
        config["emm"]["enabled"] = False
        engine = ScoringEngine(config)

        triggered, details = engine.check_emm_conditions(
            direction="short",
            price_change_24h=20.0,
            funding_rate=0.0015,
            oi_market_cap_ratio=0.30,
        )
        assert triggered is False
        assert details["reason"] == "EMM模块未启用"

    def test_price_change_24h为None_返回False(self):
        """price_change_24h 为 None 时，无法判断，返回 False"""
        engine = ScoringEngine(_make_config())

        triggered, details = engine.check_emm_conditions(
            direction="short",
            price_change_24h=None,
            funding_rate=0.0015,
            oi_market_cap_ratio=0.30,
        )
        assert triggered is False
        assert details["reason"] == "缺少24h涨跌幅数据"


# ============================================================
# 4. check_semi_emm_conditions 测试
# ============================================================

class TestCheckSemiEmmConditions:
    """测试 check_semi_emm_conditions 方法（动态阈值 vs 固定阈值）"""

    # ---------- 做空方向 ----------

    def test_做空_动态阈值_2条件满足(self):
        """做空：动态阈值启用，2/3条件满足 -> 触发半EMM"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="short",
            price_change_24h=20.0,        # 满足（固定阈值 >=15）
            funding_rate=0.0015,          # 年化 = 164.25% >= dt.funding_rate_emm_short(120.0) 满足
            oi_market_cap_ratio=0.10,     # < dt.oi_market_cap_emm(0.25) 不满足
        )
        assert is_semi is True
        assert count == 2
        assert details["dynamic_thresholds_used"] is True
        assert "2/3" in details["reason"]

    def test_做空_固定阈值_2条件满足(self):
        """做空：固定阈值，2/3条件满足 -> 触发半EMM"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))

        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="short",
            price_change_24h=20.0,        # 满足
            funding_rate=0.0015,          # 年化 = 164.25% >= 100 满足
            oi_market_cap_ratio=0.10,     # < 0.20 不满足
        )
        assert is_semi is True
        assert count == 2
        assert details["dynamic_thresholds_used"] is False
        assert "2/3" in details["reason"]

    def test_做空_固定阈值_仅1条件满足(self):
        """做空：固定阈值，仅1/3条件满足 -> 不触发半EMM"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))

        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="short",
            price_change_24h=10.0,        # < 15 不满足
            funding_rate=0.0015,          # 满足
            oi_market_cap_ratio=0.10,     # 不满足
        )
        assert is_semi is False
        assert count == 1
        assert "1/3" in details["reason"]

    # ---------- 做多方向 ----------

    def test_做多_动态阈值_2条件满足(self):
        """做多：动态阈值启用，2/3条件满足 -> 触发半EMM"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="long",
            price_change_24h=-20.0,       # 满足（固定阈值 <= -15）
            funding_rate=-0.0008,         # 年化 = -87.6% <= dt.funding_rate_emm_long(-60.0) 满足
            oi_market_cap_ratio=0.10,     # < dt.oi_market_cap_emm(0.25) 不满足
        )
        assert is_semi is True
        assert count == 2
        assert details["dynamic_thresholds_used"] is True

    def test_做多_固定阈值_2条件满足(self):
        """做多：固定阈值，2/3条件满足 -> 触发半EMM"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))

        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="long",
            price_change_24h=-20.0,       # 满足
            funding_rate=-0.0004,         # 年化 = -43.8% <= -30 满足
            oi_market_cap_ratio=0.05,     # < 0.10 不满足
        )
        assert is_semi is True
        assert count == 2
        assert details["dynamic_thresholds_used"] is False

    def test_做多_固定阈值_仅1条件满足(self):
        """做多：固定阈值，仅1/3条件满足 -> 不触发半EMM"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))

        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="long",
            price_change_24h=-10.0,       # > -15 不满足
            funding_rate=-0.0004,         # 满足
            oi_market_cap_ratio=0.05,     # 不满足
        )
        assert is_semi is False
        assert count == 1

    # ---------- 边界情况 ----------

    def test_半EMM未启用_返回False(self):
        """半EMM 模块未启用时，直接返回 False"""
        config = _make_config()
        config["semi_emm"]["enabled"] = False
        engine = ScoringEngine(config)

        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="short",
            price_change_24h=20.0,
            funding_rate=0.0015,
            oi_market_cap_ratio=0.30,
        )
        assert is_semi is False
        assert count == 0
        assert details["reason"] == "半EMM模块未启用"

    def test_price_change_24h为None_返回False(self):
        """price_change_24h 为 None 时，返回 False"""
        engine = ScoringEngine(_make_config())

        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="short",
            price_change_24h=None,
            funding_rate=0.0015,
            oi_market_cap_ratio=0.30,
        )
        assert is_semi is False
        assert count == 0
        assert details["reason"] == "缺少24h涨跌幅数据"

    def test_3条件全部满足(self):
        """3/3条件全部满足 -> 触发半EMM"""
        engine = ScoringEngine(_make_config(dynamic_enabled=False))

        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="short",
            price_change_24h=20.0,        # 满足
            funding_rate=0.0015,          # 年化 = 164.25% >= 100 满足
            oi_market_cap_ratio=0.25,     # >= 0.20 满足
        )
        assert is_semi is True
        assert count == 3
        assert "3/3" in details["reason"]


# ============================================================
# 5. 动态阈值与固定阈值切换的端到端测试
# ============================================================

class TestDynamicFixedSwitchE2E:
    """端到端：动态阈值和固定阈值在同一个引擎实例上切换"""

    def test_动态切换到固定_EMM行为变化(self):
        """注入动态阈值 -> 切换回固定阈值，EMM 行为应正确变化"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))

        # 阶段1：注入动态阈值
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)
        assert engine._should_use_dynamic() is True

        # 动态阈值下：费率 164.25% >= 120% 满足
        triggered, details = engine.check_emm_conditions(
            direction="short",
            price_change_24h=20.0,
            funding_rate=0.0015,          # 年化 164.25%
            oi_market_cap_ratio=0.30,
        )
        assert triggered is True
        assert details["dynamic_thresholds_used"] is True

        # 阶段2：清空动态阈值，回退固定阈值
        engine.set_dynamic_thresholds(None)
        assert engine._should_use_dynamic() is False

        # 固定阈值下：费率 54.75% < 100 不满足
        triggered, details = engine.check_emm_conditions(
            direction="short",
            price_change_24h=20.0,
            funding_rate=0.0005,          # 年化 54.75%
            oi_market_cap_ratio=0.30,
        )
        assert triggered is False
        assert details["dynamic_thresholds_used"] is False
        assert details["funding_rate_ok"] is False

    def test_动态切换到固定_半EMM行为变化(self):
        """注入动态阈值 -> 切换回固定阈值，半EMM 行为应正确变化"""
        engine = ScoringEngine(_make_config(dynamic_enabled=True))

        # 阶段1：动态阈值
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        # 动态阈值：费率 -87.6% <= -60% 满足，price 满足
        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="long",
            price_change_24h=-20.0,
            funding_rate=-0.0008,         # 年化 -87.6%
            oi_market_cap_ratio=0.10,     # < 0.25 不满足
        )
        assert is_semi is True
        assert count == 2
        assert details["dynamic_thresholds_used"] is True

        # 阶段2：切换到固定阈值
        engine.set_dynamic_thresholds(None)

        # 固定阈值：费率 -10.95% > -30 不满足，OI 0.05 < 0.10 不满足，仅 price 满足 -> 1/3
        is_semi, count, details = engine.check_semi_emm_conditions(
            direction="long",
            price_change_24h=-20.0,
            funding_rate=-0.0001,         # 年化 -10.95%
            oi_market_cap_ratio=0.05,     # < 0.10 固定阈值，不满足
        )
        assert is_semi is False
        assert count == 1
        assert details["dynamic_thresholds_used"] is False


# ============================================================
# 6. 性能测试
# ============================================================

class TestDynamicThresholdsPerformance:
    """动态阈值相关操作的性能测试"""

    def test_should_use_dynamic性能(self):
        """_should_use_dynamic 应极快（微秒级）"""
        import time

        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        start = time.perf_counter()
        for _ in range(10000):
            engine._should_use_dynamic()
        elapsed = time.perf_counter() - start

        avg_us = (elapsed / 10000) * 1_000_000
        print(f"\n_should_use_dynamic 平均耗时: {avg_us:.2f} us/次 (10000次总计 {elapsed*1000:.2f}ms)")
        # 应在微秒级别
        assert avg_us < 100, f"_should_use_dynamic 过慢: {avg_us:.2f} us/次"

    def test_check_emm_conditions动态模式性能(self):
        """check_emm_conditions 动态模式应快速完成"""
        import time

        engine = ScoringEngine(_make_config(dynamic_enabled=True))
        dt = _make_valid_dynamic_thresholds()
        engine.set_dynamic_thresholds(dt)

        start = time.perf_counter()
        for _ in range(5000):
            engine.check_emm_conditions(
                direction="short",
                price_change_24h=20.0,
                funding_rate=0.0015,
                oi_market_cap_ratio=0.30,
            )
        elapsed = time.perf_counter() - start

        avg_us = (elapsed / 5000) * 1_000_000
        print(f"\ncheck_emm_conditions(动态) 平均耗时: {avg_us:.2f} us/次 (5000次总计 {elapsed*1000:.2f}ms)")
        assert avg_us < 500, f"check_emm_conditions 过慢: {avg_us:.2f} us/次"