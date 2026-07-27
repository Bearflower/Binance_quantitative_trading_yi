"""
测试 CostTracker Token 用量跟踪器

覆盖以下核心功能：
- 缓存命中/未命中定价（_calc_cost）
- 不启用缓存定价时忽略 cache_hit 参数
- 月度汇总（get_monthly_cost）
- 策略汇总（get_strategy_cost）
- 总成本（get_total_cost / get_summary）
"""

import sys
from datetime import datetime
from unittest.mock import patch

import pytest

sys.path.insert(0, ".")

from ai_tuner.engine.cost_tracker import CostTracker

# ---------------------------------------------------------------------------
# 定价参数（与 config.yaml 中 deepseek.pricing 保持一致）
# ---------------------------------------------------------------------------
INPUT_PRICE = 1.74          # 输入价格（美元/百万Token）
OUTPUT_PRICE = 3.48         # 输出价格（美元/百万Token）
CACHE_HIT_PRICE = 0.174     # 缓存命中输入价格（美元/百万Token）


# ===========================================================================
# 第 1 类：缓存命中定价
# ===========================================================================


class TestCacheHitPricing:
    """测试 _calc_cost 在 cache_hit=True 时的定价行为"""

    def test_cache_hit_uses_reduced_price(self):
        """cache_hit=True 时应使用缓存命中输入价格"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        # prompt=1M, completion=500K
        # expected: (1e6/1e6)*0.174 + (5e5/1e6)*3.48 = 0.174 + 1.740 = 1.914
        cost = tracker._calc_cost(1_000_000, 500_000, cache_hit=True)
        expected = (1_000_000 / 1_000_000) * CACHE_HIT_PRICE + (500_000 / 1_000_000) * OUTPUT_PRICE
        assert cost == pytest.approx(expected)

    def test_cache_hit_small_tokens(self):
        """小 Token 数下缓存命中价格计算正确"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        cost = tracker._calc_cost(100, 50, cache_hit=True)
        expected = (100 / 1_000_000) * CACHE_HIT_PRICE + (50 / 1_000_000) * OUTPUT_PRICE
        assert cost == pytest.approx(expected)

    def test_cache_hit_zero_output(self):
        """输出 Token 为 0 时缓存命中价格只计算输入部分"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        cost = tracker._calc_cost(1_000_000, 0, cache_hit=True)
        expected = (1_000_000 / 1_000_000) * CACHE_HIT_PRICE
        assert cost == pytest.approx(expected)


# ===========================================================================
# 第 2 类：缓存未命中定价
# ===========================================================================


class TestCacheMissPricing:
    """测试 _calc_cost 在 cache_hit=False 时的定价行为"""

    def test_cache_miss_uses_standard_price(self):
        """cache_hit=False 时应使用标准输入价格"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        # prompt=1M, completion=500K
        # expected: (1e6/1e6)*1.74 + (5e5/1e6)*3.48 = 1.740 + 1.740 = 3.480
        cost = tracker._calc_cost(1_000_000, 500_000, cache_hit=False)
        expected = (1_000_000 / 1_000_000) * INPUT_PRICE + (500_000 / 1_000_000) * OUTPUT_PRICE
        assert cost == pytest.approx(expected)

    def test_cache_miss_default_param(self):
        """不传 cache_hit 参数时默认使用 False（标准价格）"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        cost = tracker._calc_cost(1_000_000, 500_000)
        expected = (1_000_000 / 1_000_000) * INPUT_PRICE + (500_000 / 1_000_000) * OUTPUT_PRICE
        assert cost == pytest.approx(expected)

    def test_cache_miss_vs_hit_price_difference(self):
        """缓存命中与未命中的价格差应为 10 倍输入价格差异"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        prompt = 500_000
        completion = 200_000
        miss_cost = tracker._calc_cost(prompt, completion, cache_hit=False)
        hit_cost = tracker._calc_cost(prompt, completion, cache_hit=True)
        # 输出部分相同，输入部分差 10 倍：(prompt/1e6)*(1.74-0.174)
        input_diff = (prompt / 1_000_000) * (INPUT_PRICE - CACHE_HIT_PRICE)
        assert miss_cost - hit_cost == pytest.approx(input_diff)


# ===========================================================================
# 第 3 类：不启用缓存定价时忽略 cache_hit 参数
# ===========================================================================


class TestDisabledCachePricing:
    """测试 input_cache_hit_price=0 时 cache_hit 被忽略"""

    def test_cache_hit_ignored_when_disabled(self):
        """input_cache_hit_price=0 时即使 cache_hit=True 也使用标准价格"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, input_cache_hit_price=0.0)
        cost_with_hit = tracker._calc_cost(1_000_000, 500_000, cache_hit=True)
        cost_without_hit = tracker._calc_cost(1_000_000, 500_000, cache_hit=False)
        # 两者应相等
        assert cost_with_hit == pytest.approx(cost_without_hit)

    def test_disabled_cache_default_constructor(self):
        """不传 input_cache_hit_price 时默认为 0，缓存定价不启用"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE)
        cost = tracker._calc_cost(1_000_000, 500_000, cache_hit=True)
        expected = (1_000_000 / 1_000_000) * INPUT_PRICE + (500_000 / 1_000_000) * OUTPUT_PRICE
        assert cost == pytest.approx(expected)

    def test_disabled_cache_multiple_calls(self):
        """多次调用且缓存定价禁用时每次价格一致"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, input_cache_hit_price=0.0)
        costs = [
            tracker._calc_cost(1000, 500, cache_hit=True),
            tracker._calc_cost(1000, 500, cache_hit=True),
            tracker._calc_cost(1000, 500, cache_hit=False),
        ]
        # 三次结果应完全相同
        assert costs[0] == pytest.approx(costs[1])
        assert costs[0] == pytest.approx(costs[2])

    def test_negative_cache_price_defaults_to_standard(self):
        """input_cache_hit_price 为负值（配置错误）时应使用标准价格"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, input_cache_hit_price=-0.5)
        cost = tracker._calc_cost(1_000_000, 500_000, cache_hit=True)
        expected = (1_000_000 / 1_000_000) * INPUT_PRICE + (500_000 / 1_000_000) * OUTPUT_PRICE
        # 注意：实际逻辑是 input_cache_hit_price > 0 才启用，负值应不启用
        assert cost == pytest.approx(expected)


# ===========================================================================
# 第 4 类：月度汇总
# ===========================================================================


class TestMonthlyAggregation:
    """测试 get_monthly_cost 月度汇总功能"""

    def test_single_month_single_record(self):
        """单月单条记录"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        with patch("ai_tuner.engine.cost_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 15, 10, 30, 0)
            tracker.record_usage("deepseek-v4", 1000, 500, 1500, strategy_id="strat_a")

        monthly = tracker.get_monthly_cost()
        assert "2026-06" in monthly
        assert monthly["2026-06"]["prompt_tokens"] == 1000
        assert monthly["2026-06"]["completion_tokens"] == 500
        assert monthly["2026-06"]["total_tokens"] == 1500
        assert monthly["2026-06"]["calls"] == 1

    def test_multiple_records_same_month(self):
        """同月多条记录应累加"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        with patch("ai_tuner.engine.cost_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 1, 0, 0, 0)
            tracker.record_usage("model-a", 1000, 500, 1500)
            tracker.record_usage("model-a", 2000, 1000, 3000)
            tracker.record_usage("model-a", 3000, 1500, 4500)

        monthly = tracker.get_monthly_cost()
        assert "2026-06" in monthly
        assert monthly["2026-06"]["prompt_tokens"] == 6000    # 1000+2000+3000
        assert monthly["2026-06"]["completion_tokens"] == 3000  # 500+1000+1500
        assert monthly["2026-06"]["total_tokens"] == 9000       # 1500+3000+4500
        assert monthly["2026-06"]["calls"] == 3

    def test_multiple_months(self):
        """跨月记录应正确归入不同月份"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        with patch("ai_tuner.engine.cost_tracker.datetime") as mock_dt:
            # 2026-01
            mock_dt.now.return_value = datetime(2026, 1, 15, 10, 0, 0)
            tracker.record_usage("model-a", 1000, 500, 1500)
            # 2026-02
            mock_dt.now.return_value = datetime(2026, 2, 10, 14, 0, 0)
            tracker.record_usage("model-a", 2000, 1000, 3000)
            # 2026-03
            mock_dt.now.return_value = datetime(2026, 3, 5, 8, 0, 0)
            tracker.record_usage("model-a", 4000, 2000, 6000)

        monthly = tracker.get_monthly_cost()
        assert len(monthly) == 3
        assert monthly["2026-01"]["prompt_tokens"] == 1000
        assert monthly["2026-02"]["prompt_tokens"] == 2000
        assert monthly["2026-03"]["prompt_tokens"] == 4000
        assert monthly["2026-01"]["calls"] == 1
        assert monthly["2026-02"]["calls"] == 1
        assert monthly["2026-03"]["calls"] == 1

    def test_monthly_cost_calculation(self):
        """月度成本应累计所有记录的成本"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        with patch("ai_tuner.engine.cost_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 1, 0, 0, 0)
            # record_usage 调用 _calc_cost 时不传 cache_hit，默认 False
            tracker.record_usage("model-a", 1_000_000, 500_000, 1_500_000)
            tracker.record_usage("model-a", 500_000, 250_000, 750_000)

        monthly = tracker.get_monthly_cost()
        # 第一条成本: (1e6/1e6)*1.74 + (5e5/1e6)*3.48 = 3.48
        # 第二条成本: (5e5/1e6)*1.74 + (2.5e5/1e6)*3.48 = 0.87 + 0.87 = 1.74
        expected_cost = 3.48 + 1.74
        assert monthly["2026-06"]["cost"] == pytest.approx(expected_cost)


# ===========================================================================
# 第 5 类：策略汇总
# ===========================================================================


class TestStrategyAggregation:
    """测试 get_strategy_cost 按策略汇总功能"""

    def test_single_strategy(self):
        """单个策略多条记录应正确累加"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        tracker.record_usage("model-a", 1000, 500, 1500, strategy_id="strat_a")
        tracker.record_usage("model-a", 2000, 1000, 3000, strategy_id="strat_a")

        result = tracker.get_strategy_cost("strat_a")
        assert result["prompt_tokens"] == 3000
        assert result["completion_tokens"] == 1500
        assert result["total_tokens"] == 4500
        assert result["calls"] == 2

    def test_multiple_strategies(self):
        """多个策略应独立汇总"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        tracker.record_usage("model-a", 1000, 500, 1500, strategy_id="strat_a")
        tracker.record_usage("model-a", 2000, 1000, 3000, strategy_id="strat_b")
        tracker.record_usage("model-a", 4000, 2000, 6000, strategy_id="strat_a")

        strat_a = tracker.get_strategy_cost("strat_a")
        strat_b = tracker.get_strategy_cost("strat_b")
        assert strat_a["prompt_tokens"] == 5000   # 1000+4000
        assert strat_b["prompt_tokens"] == 2000
        assert strat_a["calls"] == 2
        assert strat_b["calls"] == 1

    def test_strategy_not_found_returns_zero(self):
        """查询不存在的策略应返回全零汇总"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        result = tracker.get_strategy_cost("nonexistent")
        assert result["prompt_tokens"] == 0
        assert result["completion_tokens"] == 0
        assert result["total_tokens"] == 0
        assert result["cost"] == pytest.approx(0.0)
        assert result["calls"] == 0

    def test_empty_strategy_id_not_tracked(self):
        """strategy_id 为空字符串时不记入策略汇总"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        tracker.record_usage("model-a", 1000, 500, 1500, strategy_id="")
        # 空 strategy_id 不应出现在策略汇总中
        result = tracker.get_strategy_cost("")
        assert result["prompt_tokens"] == 0
        assert result["calls"] == 0


# ===========================================================================
# 第 6 类：总成本
# ===========================================================================


class TestTotalCost:
    """测试 get_total_cost 和 get_summary 总成本功能"""

    def test_total_cost_multiple_strategies(self):
        """多个策略的总成本应正确累加"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        # strat_a 成本
        tracker.record_usage("model-a", 1_000_000, 500_000, 1_500_000, strategy_id="strat_a")
        # strat_b 成本
        tracker.record_usage("model-a", 500_000, 250_000, 750_000, strategy_id="strat_b")
        # strat_a 额外成本
        tracker.record_usage("model-a", 2_000_000, 1_000_000, 3_000_000, strategy_id="strat_a")

        total = tracker.get_total_cost()
        # strat_a: (1e6/1e6)*1.74 + (5e5/1e6)*3.48 + (2e6/1e6)*1.74 + (1e6/1e6)*3.48 = 3.48 + 6.96 = 10.44
        # strat_b: (5e5/1e6)*1.74 + (2.5e5/1e6)*3.48 = 0.87 + 0.87 = 1.74
        # total: 10.44 + 1.74 = 12.18
        assert total == pytest.approx(12.18)

    def test_total_cost_no_records(self):
        """无记录时总成本为 0"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        assert tracker.get_total_cost() == pytest.approx(0.0)

    def test_summary_contains_all_fields(self):
        """get_summary 应包含完整的汇总信息"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        tracker.record_usage("model-a", 1_000_000, 500_000, 1_500_000, strategy_id="strat_a")
        tracker.record_usage("model-a", 2_000_000, 1_000_000, 3_000_000, strategy_id="strat_b")

        summary = tracker.get_summary()
        assert "total_cost_usd" in summary
        assert "total_calls" in summary
        assert "monthly" in summary
        assert "by_strategy" in summary
        assert summary["total_calls"] == 2
        # strat_a: 3.48, strat_b: 6.96, total: 10.44
        assert summary["total_cost_usd"] == pytest.approx(round(3.48 + 6.96, 4))

    def test_total_calls_count(self):
        """total_calls 应统计所有记录（含无 strategy_id 的）"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        tracker.record_usage("model-a", 1000, 500, 1500, strategy_id="strat_a")
        tracker.record_usage("model-a", 2000, 1000, 3000, strategy_id="")  # 无策略ID
        tracker.record_usage("model-a", 3000, 1500, 4500, strategy_id="strat_b")

        summary = tracker.get_summary()
        # total_calls = 3（全部记录）
        assert summary["total_calls"] == 3

    def test_summary_cost_rounded_to_4_decimal(self):
        """get_summary 的总成本应四舍五入到 4 位小数"""
        tracker = CostTracker(INPUT_PRICE, OUTPUT_PRICE, CACHE_HIT_PRICE)
        # 使用非常小的 Token 数产生多位小数的成本
        tracker.record_usage("model-a", 1, 1, 2, strategy_id="strat_a")
        tracker.record_usage("model-a", 2, 1, 3, strategy_id="strat_b")

        summary = tracker.get_summary()
        # 验证 rounding
        raw_total = tracker.get_total_cost()
        assert summary["total_cost_usd"] == round(raw_total, 4)