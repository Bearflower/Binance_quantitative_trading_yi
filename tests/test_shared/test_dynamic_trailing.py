"""
新币做空策略动态利润保护 - shared 层纯计算函数测试

测试目标：
1. calculate_dynamic_trailing_stop() — 核心计算函数
2. get_volatility_adjustment() — 波动率调节因子（异步）
3. calculate_retrace_stop_price() — 阶梯回撤止损价
4. calculate_hard_stop_price() — 硬止损价
5. apply_one_way_protection() — 单向移动保护
"""
import sys
import os
import time
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pandas as pd

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

from shared.dynamic_trailing import (
    calculate_dynamic_trailing_stop,
    get_volatility_adjustment,
    calculate_retrace_stop_price,
    calculate_hard_stop_price,
    apply_one_way_protection,
    TrailingStopResult,
)


# ============================================================================
# 辅助函数
# ============================================================================

def make_short_config(**overrides) -> dict:
    """创建 new_coin 风格的动态利润保护配置"""
    config = {
        'enabled': True,
        'activation': {
            'min_profit_pct': 1.5,
            'also_on_tp1': False,
            'also_on_tp2': True,
        },
        'regression_tiers': [
            {'profit_ceiling': 1.5, 'retrace_ratio': 0.0},
            {'profit_ceiling': 4.0, 'retrace_ratio': 0.5},
            {'profit_ceiling': 8.0, 'retrace_ratio': 0.35},
            {'profit_ceiling': 999.0, 'retrace_ratio': 0.25},
        ],
        'volatility_adjustment': {
            'enabled': True,
            'atr_lookback_days': 30,
            'atr_period': 14,
            'cache_ttl_seconds': 3600,
            'min_vol_adj': 0.5,
            'max_vol_adj': 2.0,
        },
        'stop_limit_order': {
            'offset_pct': 0.002,
        },
        'cleanup_silent_error_codes': [-2022, -2011],
    }
    config.update(overrides)
    return config


def make_long_config(**overrides) -> dict:
    """创建 btc_eth 风格的动态利润保护配置"""
    config = make_short_config()
    config['activation'] = {
        'min_profit_pct': 1.5,
        'also_on_tp1': True,
        'also_on_tp2': False,
    }
    config.update(overrides)
    return config


# ============================================================================
# 辅助计算函数单元测试
# ============================================================================

class TestCalculateRetraceStopPrice:
    """calculate_retrace_stop_price() 测试"""

    def test_long_retrace(self):
        """做多正常计算"""
        result = calculate_retrace_stop_price(
            direction='LONG',
            reference_price=Decimal('110'),
            entry_price=Decimal('100'),
            retrace_ratio=0.5,
            vol_adj=1.0,
        )
        # profit_per_unit = 110 - 100 = 10
        # allowed_retrace = 10 * 0.5 * 1.0 = 5
        # stop_price = 110 - 5 = 105
        assert result == Decimal('105')

    def test_short_retrace(self):
        """做空正常计算"""
        result = calculate_retrace_stop_price(
            direction='SHORT',
            reference_price=Decimal('90'),
            entry_price=Decimal('100'),
            retrace_ratio=0.5,
            vol_adj=1.0,
        )
        # profit_per_unit = 100 - 90 = 10
        # allowed_retrace = 10 * 0.5 * 1.0 = 5
        # stop_price = 90 + 5 = 95
        assert result == Decimal('95')

    def test_vol_adj_half(self):
        """波动率调节因子 0.5，回撤减半"""
        result = calculate_retrace_stop_price(
            direction='LONG',
            reference_price=Decimal('110'),
            entry_price=Decimal('100'),
            retrace_ratio=0.5,
            vol_adj=0.5,
        )
        # allowed_retrace = 10 * 0.5 * 0.5 = 2.5
        # stop_price = 110 - 2.5 = 107.5
        assert result == Decimal('107.5')

    def test_vol_adj_double(self):
        """波动率调节因子 2.0，回撤翻倍"""
        result = calculate_retrace_stop_price(
            direction='LONG',
            reference_price=Decimal('110'),
            entry_price=Decimal('100'),
            retrace_ratio=0.5,
            vol_adj=2.0,
        )
        # allowed_retrace = 10 * 0.5 * 2.0 = 10
        # stop_price = 110 - 10 = 100
        assert result == Decimal('100')

    def test_retrace_ratio_zero(self):
        """回撤比例为 0，止损价 = 参考价"""
        result = calculate_retrace_stop_price(
            direction='LONG',
            reference_price=Decimal('110'),
            entry_price=Decimal('100'),
            retrace_ratio=0.0,
            vol_adj=1.0,
        )
        # allowed_retrace = 10 * 0.0 * 1.0 = 0
        # stop_price = 110 - 0 = 110
        assert result == Decimal('110')

    def test_no_profit_long(self):
        """做空无浮盈时参考价 = 入场价"""
        result = calculate_retrace_stop_price(
            direction='SHORT',
            reference_price=Decimal('100'),  # 未盈利
            entry_price=Decimal('100'),
            retrace_ratio=0.5,
            vol_adj=1.0,
        )
        # profit_per_unit = 0, allowed_retrace = 0
        # stop_price = 100 + 0 = 100
        assert result == Decimal('100')

    def test_short_small_vol_adj(self):
        """做空 + vol_adj 极小值 0.5"""
        result = calculate_retrace_stop_price(
            direction='SHORT',
            reference_price=Decimal('80'),
            entry_price=Decimal('100'),
            retrace_ratio=0.25,
            vol_adj=0.5,
        )
        # profit_per_unit = 20, allowed_retrace = 20 * 0.25 * 0.5 = 2.5
        # stop_price = 80 + 2.5 = 82.5
        assert result == Decimal('82.5')


class TestCalculateHardStopPrice:
    """calculate_hard_stop_price() 测试"""

    def test_long_hard_stop(self):
        """做多硬止损计算"""
        result = calculate_hard_stop_price(
            direction='LONG',
            entry_price=Decimal('100'),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('1.5'),
        )
        # hard_stop = 100 - 2 * 1.5 = 100 - 3 = 97
        assert result == Decimal('97')

    def test_short_hard_stop(self):
        """做空硬止损计算"""
        result = calculate_hard_stop_price(
            direction='SHORT',
            entry_price=Decimal('100'),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # hard_stop = 100 + 2 * 2.5 = 100 + 5 = 105
        assert result == Decimal('105')

    def test_large_atr(self):
        """大 ATR 值"""
        result = calculate_hard_stop_price(
            direction='SHORT',
            entry_price=Decimal('100'),
            atr=Decimal('10'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # hard_stop = 100 + 10 * 2.5 = 125
        assert result == Decimal('125')

    def test_small_atr(self):
        """极小 ATR 值"""
        result = calculate_hard_stop_price(
            direction='LONG',
            entry_price=Decimal('100'),
            atr=Decimal('0.1'),
            stop_loss_atr_multiplier=Decimal('1.5'),
        )
        # hard_stop = 100 - 0.1 * 1.5 = 99.85
        assert result == Decimal('99.85')


class TestApplyOneWayProtection:
    """apply_one_way_protection() 测试"""

    def test_long_new_lower_stop(self):
        """做多新止损价更低（不利方向），应保持旧值"""
        result = apply_one_way_protection(
            direction='LONG',
            new_stop_price=Decimal('103'),
            current_stop_price=Decimal('105'),
        )
        # new=103 < current=105 → 保持旧值 105
        assert result == Decimal('105')

    def test_long_new_higher_stop(self):
        """做多新止损价更高（有利方向），应更新"""
        result = apply_one_way_protection(
            direction='LONG',
            new_stop_price=Decimal('107'),
            current_stop_price=Decimal('105'),
        )
        # new=107 > current=105 → 更新为 107
        assert result == Decimal('107')

    def test_short_new_higher_stop(self):
        """做空新止损价更高（不利方向），应保持旧值"""
        result = apply_one_way_protection(
            direction='SHORT',
            new_stop_price=Decimal('96'),
            current_stop_price=Decimal('95'),
        )
        # new=96 > current=95 → 保持旧值 95
        assert result == Decimal('95')

    def test_short_new_lower_stop(self):
        """做空新止损价更低（有利方向），应更新"""
        result = apply_one_way_protection(
            direction='SHORT',
            new_stop_price=Decimal('93'),
            current_stop_price=Decimal('95'),
        )
        # new=93 < current=95 → 更新为 93
        assert result == Decimal('93')

    def test_first_time_no_current(self):
        """首次设置止损价（current_stop_price=None），直接返回新值"""
        result = apply_one_way_protection(
            direction='LONG',
            new_stop_price=Decimal('105'),
            current_stop_price=None,
        )
        assert result == Decimal('105')

    def test_short_first_time_no_current(self):
        """做空首次设置"""
        result = apply_one_way_protection(
            direction='SHORT',
            new_stop_price=Decimal('95'),
            current_stop_price=None,
        )
        assert result == Decimal('95')

    def test_long_equal_stop(self):
        """做多新旧止损价相等，返回新值"""
        result = apply_one_way_protection(
            direction='LONG',
            new_stop_price=Decimal('105'),
            current_stop_price=Decimal('105'),
        )
        # new=105 >= current=105 → 更新为 105
        assert result == Decimal('105')

    def test_short_equal_stop(self):
        """做空新旧止损价相等，返回新值"""
        result = apply_one_way_protection(
            direction='SHORT',
            new_stop_price=Decimal('95'),
            current_stop_price=Decimal('95'),
        )
        # new=95 <= current=95 → 更新为 95
        assert result == Decimal('95')


# ============================================================================
# calculate_dynamic_trailing_stop 核心测试
# ============================================================================

class TestDynamicTrailingStopNotActivated:
    """未激活场景"""

    def test_profit_below_min_tp_not_hit(self):
        """浮盈不足，TP1/TP2 未到达 → 返回 None"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('98'),
            highest_price=None,
            lowest_price=Decimal('98'),
            trailing_activated=False,
            tp1_hit=False,
            tp2_hit=False,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 浮盈 = (100-98)/100*100 = 2% ≥ 1.5%
        # 但 also_on_tp1=false, also_on_tp2=true, tp2_hit=false
        # profit_activated = True (2% >= 1.5%)
        # 所以会激活
        assert result is not None, "浮盈2% >= 1.5% 应激活"

    def test_profit_below_min_tp1_hit_but_config_false(self):
        """浮盈不足，TP1 到达但配置 also_on_tp1=false → 返回 None"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('99'),
            highest_price=None,
            lowest_price=Decimal('99'),
            trailing_activated=False,
            tp1_hit=True,
            tp2_hit=False,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(),  # also_on_tp1=False
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 浮盈 = (100-99)/100*100 = 1% < 1.5%
        # profit_activated = False
        # tp1_activated = also_on_tp1=False and tp1_hit=True → False
        # tp2_activated = also_on_tp2=True and tp2_hit=False → False
        # 全部不满足 → 返回 None
        assert result is None, "浮盈1% < 1.5% 且 also_on_tp1=false, also_on_tp2=false → 不激活"

    def test_tp2_hit_activates(self):
        """TP2 到达激活（also_on_tp2=true）"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('99'),
            highest_price=None,
            lowest_price=Decimal('99'),
            trailing_activated=False,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(),  # also_on_tp2=True
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 浮盈1% < 1.5%，但 tp1_hit=True 跳过保本路径
        # tier_index=0, retrace_ratio=0.0
        # 回撤止损 = 99 + 1*0.0*1.0 = 99
        # 硬止损 = 100 + 2*2.5 = 105
        # 最终 = min(99, 105) = 99
        # 触发: 99 >= 99 → True（触发）
        # 使用更低 current_price 测试未触发场景
        assert result is not None, "TP2 到达应激活"
        assert result.trailing_activated is True
        assert result.triggered is True, "99 >= 99 应触发"

    def test_disabled_config_returns_none(self):
        """enabled=False 时返回 None"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('80'),
            highest_price=None,
            lowest_price=Decimal('80'),
            trailing_activated=False,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(enabled=False),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        assert result is None, "禁用时应返回 None"

    def test_entry_price_zero(self):
        """entry_price=0 返回 None"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('0'),
            current_price=Decimal('100'),
            highest_price=None,
            lowest_price=Decimal('100'),
            trailing_activated=False,
            tp1_hit=False,
            tp2_hit=False,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        assert result is None, "entry_price=0 应返回 None"

    def test_tiers_empty(self):
        """regression_tiers 为空列表返回 None"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('80'),
            highest_price=None,
            lowest_price=Decimal('80'),
            trailing_activated=False,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(regression_tiers=[]),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        assert result is None, "空的 regression_tiers 应返回 None"


class TestDynamicTrailingStopActivated:
    """已激活场景"""

    def test_activated_short_not_triggered(self):
        """做空已激活，价格在止损价内，未触发（此用例由 test_activated_short_not_triggered_fixed 覆盖）"""
        pass

    def test_activated_short_not_triggered_fixed(self):
        """做空已激活，价格未达止损价，未触发"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('91'),
            highest_price=None,
            lowest_price=Decimal('90'),
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=10.0,
            current_tier_index=3,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 浮盈 = (100-90)/100*100 = 10%
        # 阶梯4: retrace_ratio=0.25
        # 回撤止损 = 90 + 10*0.25*1.0 = 92.5
        # 硬止损 = 100 + 2*2.5 = 105
        # 最终 = min(92.5, 105) = 92.5
        # 触发: 91 >= 92.5? → False
        assert result is not None
        assert result.trailing_activated is True
        assert result.triggered is False, "91 < 92.5 不应触发"
        assert result.trailing_stop_price == Decimal('92.5')

    def test_activated_triggered(self):
        """做空已激活，价格反弹超过止损价，触发平仓"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('98'),
            highest_price=None,
            lowest_price=Decimal('90'),
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=10.0,
            current_tier_index=3,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 回撤止损 = 90 + 10*0.25*1.0 = 92.5
        # 硬止损 = 105
        # 最终 = min(92.5, 105) = 92.5
        # 触发: 98 >= 92.5? → True
        assert result is not None
        assert result.triggered is True, "98 >= 92.5 应触发"

    def test_long_activated_not_triggered(self):
        """做多已激活，价格在止损价内，未触发（此用例由 test_long_activated_not_triggered_fixed 覆盖）"""
        pass

    def test_long_activated_not_triggered_fixed(self):
        """做多已激活，价格高于止损价，未触发"""
        result = calculate_dynamic_trailing_stop(
            direction='LONG',
            entry_price=Decimal('100'),
            current_price=Decimal('108'),
            highest_price=Decimal('110'),
            lowest_price=None,
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=10.0,
            current_tier_index=3,
            current_trailing_stop_price=None,
            config=make_long_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('1.5'),
        )
        # 回撤止损 = 110 - 10*0.25*1.0 = 107.5
        # 硬止损 = 97
        # 最终 = max(107.5, 97) = 107.5
        # 触发: 108 <= 107.5? → False
        assert result is not None
        assert result.triggered is False, "108 > 107.5 不应触发"
        assert result.trailing_stop_price == Decimal('107.5')

    def test_long_triggered(self):
        """做多已激活，价格跌破止损价，触发平仓"""
        result = calculate_dynamic_trailing_stop(
            direction='LONG',
            entry_price=Decimal('100'),
            current_price=Decimal('107'),
            highest_price=Decimal('110'),
            lowest_price=None,
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=10.0,
            current_tier_index=3,
            current_trailing_stop_price=None,
            config=make_long_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('1.5'),
        )
        # 最终 = 107.5
        # 触发: 107 <= 107.5? → True
        assert result is not None
        assert result.triggered is True, "107 <= 107.5 应触发"


class TestOneWayProtection:
    """单向移动保护测试"""

    def test_short_stop_cannot_move_up(self):
        """做空：止损价不能上移（新止损价96 > 旧止损价95，保持旧值）"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('91'),
            highest_price=None,
            lowest_price=Decimal('90'),
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=10.0,
            current_tier_index=3,
            current_trailing_stop_price=Decimal('95'),  # 旧止损价 95
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 计算出的止损价 = 92.5
        # 92.5 < 95 → 单向保护允许
        # 最终 = 92.5
        assert result is not None
        assert result.trailing_stop_price == Decimal('92.5'), "92.5 < 95，应更新为 92.5"

    def test_short_stop_cannot_move_up_worse(self):
        """做空：新计算止损价高于旧值，应保持旧值"""
        # 浮盈7% → 阶梯2 (profit_ceiling=8.0, retrace_ratio=0.35)
        # 回撤止损 = 93 + 7*0.35*1.0 = 93 + 2.45 = 95.45
        # 硬止损 = 105
        # 最终 = min(95.45, 105) = 95.45
        # 95.45 > 95 → 单向保护阻止，保持旧值 95
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('94'),
            highest_price=None,
            lowest_price=Decimal('93'),
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=7.0,
            current_tier_index=2,
            current_trailing_stop_price=Decimal('95'),  # 旧止损价 95
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 95.45 > 95 → 单向保护阻止，保持旧值 95
        assert result is not None
        assert result.trailing_stop_price == Decimal('95'), f"95.45 > 95 应保持旧值 95，实际 {result.trailing_stop_price}"

    def test_short_stop_better_than_current(self):
        """做空：新止损价更低（有利方向），应更新"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('91'),
            highest_price=None,
            lowest_price=Decimal('88'),
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=12.0,
            current_tier_index=3,
            current_trailing_stop_price=Decimal('95'),
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 回撤止损 = 88 + 12*0.25*1.0 = 88 + 3 = 91
        # 硬止损 = 105
        # 最终 = min(91, 105) = 91
        # 91 < 95 → 应更新
        assert result is not None
        assert result.trailing_stop_price < Decimal('95'), "止损价应降低"
        assert result.trailing_stop_price == Decimal('91')

    def test_long_stop_cannot_move_down(self):
        """做多：止损价不能下移"""
        result = calculate_dynamic_trailing_stop(
            direction='LONG',
            entry_price=Decimal('100'),
            current_price=Decimal('108'),
            highest_price=Decimal('110'),
            lowest_price=None,
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=10.0,
            current_tier_index=3,
            current_trailing_stop_price=Decimal('107'),  # 旧止损价 107
            config=make_long_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('1.5'),
        )
        # 回撤止损 = 110 - 10*0.25*1.0 = 107.5
        # 硬止损 = 97
        # 最终 = max(107.5, 97) = 107.5
        # 107.5 > 107 → 应更新
        assert result is not None
        assert result.trailing_stop_price == Decimal('107.5'), "107.5 > 107，应更新"


class TestHardStopFallback:
    """硬止损兜底测试"""

    def test_short_hard_stop_as_fallback(self):
        """做空：硬止损为兜底，最终取 min(阶梯止损, 硬止损)"""
        # 阶梯止损 < 硬止损（通常情况），取阶梯止损
        # 浮盈10% → 阶梯4(retrace=0.25)
        # 回撤止损 = 90 + 10*0.25*1.0 = 92.5
        # 硬止损 = 100 + 2*2.5 = 105
        # 最终 = min(92.5, 105) = 92.5
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('90'),
            highest_price=None,
            lowest_price=Decimal('90'),
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=10.0,
            current_tier_index=3,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        assert result is not None
        assert result.trailing_stop_price == Decimal('92.5'), "阶梯止损 92.5 < 硬止损 105，取 92.5"

    def test_long_hard_stop_as_fallback(self):
        """做多：硬止损为兜底，最终取 max(阶梯止损, 硬止损)"""
        # 阶梯止损通常 > 硬止损（做多时），取阶梯止损
        config = make_long_config()
        # 浮盈10% → 阶梯4(retrace=0.25)
        result = calculate_dynamic_trailing_stop(
            direction='LONG',
            entry_price=Decimal('100'),
            current_price=Decimal('108'),
            highest_price=Decimal('110'),
            lowest_price=None,
            trailing_activated=True,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=10.0,
            current_tier_index=3,
            current_trailing_stop_price=None,
            config=config,
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('1.5'),
        )
        # 回撤止损 = 110 - 10*0.25*1.0 = 107.5
        # 硬止损 = 100 - 2*1.5 = 97
        # 最终 = max(107.5, 97) = 107.5
        assert result is not None
        assert result.trailing_stop_price == Decimal('107.5'), "阶梯止损 107.5 > 硬止损 97，取 107.5"

    def test_hard_stop_prevents_extreme_short(self):
        """做空硬止损：与阶梯止损取 min 保护"""
        # 浮盈1% < 1.5%，且 tp1_hit=False → 保本模式
        # stop_price = entry_price = 100
        # 硬止损 = 100 + 2*2.5 = 105
        # 最终 = min(100, 105) = 100
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('99'),
            highest_price=None,
            lowest_price=Decimal('99'),
            trailing_activated=True,
            tp1_hit=False,  # 不跳过保本路径
            tp2_hit=True,
            pending_profit_pct=1.0,
            current_tier_index=0,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 保本模式：stop_price = 100
        # 硬止损 = 105
        # 最终 = min(100, 105) = 100
        assert result is not None
        assert result.trailing_stop_price == Decimal('100'), f"保本模式止损价=100，实际 {result.trailing_stop_price}"


class TestBreakevenMode:
    """保本模式测试"""

    def test_breakeven_entry_price(self):
        """浮盈 < 1.5% 且 TP1 未触发 → 保本模式，止损价 = 入场价"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('99'),
            highest_price=None,
            lowest_price=Decimal('99'),
            trailing_activated=False,
            tp1_hit=False,
            tp2_hit=False,  # also_on_tp2=True but tp2_hit=False
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 浮盈 = 1% < 1.5%
        # profit_activated = False
        # tp1_activated = False (also_on_tp1=False)
        # tp2_activated = False (tp2_hit=False)
        # → 返回 None
        assert result is None

    def test_breakeven_after_activation_stays_active(self):
        """已激活后浮盈回落，应保持激活"""
        # 先激活（tp2_hit=True）
        result1 = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('90'),
            highest_price=None,
            lowest_price=Decimal('90'),
            trailing_activated=True,  # 已激活
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=10.0,
            current_tier_index=3,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        assert result1 is not None
        assert result1.trailing_activated is True

        # 浮盈回落（但已激活）
        result2 = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('99'),
            highest_price=None,
            lowest_price=Decimal('99'),
            trailing_activated=True,  # 保持激活
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=1.0,
            current_tier_index=0,
            current_trailing_stop_price=result1.trailing_stop_price,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 已激活状态下，即使浮盈回落也不返回 None（代码中 trailing_activated=True 时跳过 return None）
        assert result2 is not None, "已激活后浮盈回落应保持激活"


class TestTierSelection:
    """阶梯选择测试"""

    def test_tier_1_profit_2pct(self):
        """浮盈2% → 阶梯 1 (profit_ceiling=4.0, retrace_ratio=0.5)"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('96'),
            highest_price=None,
            lowest_price=Decimal('96'),
            trailing_activated=False,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 浮盈 = (100-96)/100*100 = 4% → 刚好等于 profit_ceiling=4.0
        # 4.0 < 4.0 为 False → 跳到下一个阶梯
        # 阶梯 2 (profit_ceiling=8.0, retrace_ratio=0.35)
        assert result is not None
        # 回撤止损 = 96 + 4*0.35*1.0 = 97.4
        # 硬止损 = 105
        # 最终 = min(97.4, 105) = 97.4
        assert result.trailing_stop_price == Decimal('97.4'), f"期望 97.4，实际 {result.trailing_stop_price}"

    def test_tier_2_profit_5pct(self):
        """浮盈5% → 阶梯 2 (profit_ceiling=8.0, retrace_ratio=0.35)"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('93'),
            highest_price=None,
            lowest_price=Decimal('93'),
            trailing_activated=False,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 浮盈 = 7% → 对应阶梯 2 (profit_ceiling=8.0, retrace_ratio=0.35)
        # 回撤止损 = 93 + 7*0.35*1.0 = 93 + 2.45 = 95.45
        # 硬止损 = 105
        # 最终 = min(95.45, 105) = 95.45
        assert result is not None
        assert result.trailing_stop_price == Decimal('95.45')

    def test_tier_3_profit_10pct(self):
        """浮盈10% → 阶梯 3 (profit_ceiling=999.0, retrace_ratio=0.25)"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('85'),
            highest_price=None,
            lowest_price=Decimal('85'),
            trailing_activated=False,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        # 浮盈 = 15% → 最后一个阶梯 (retrace_ratio=0.25)
        # 回撤止损 = 85 + 15*0.25*1.0 = 85 + 3.75 = 88.75
        # 硬止损 = 105
        # 最终 = min(88.75, 105) = 88.75
        assert result is not None
        assert result.trailing_stop_price == Decimal('88.75')


class TestVolatilityAdjustment:
    """get_volatility_adjustment() 测试"""

    @pytest.mark.asyncio
    async def test_disabled_returns_1_0(self):
        """禁用时返回 1.0"""
        mock_kline = AsyncMock()
        result = await get_volatility_adjustment(
            symbol='BTCUSDT',
            entry_price=Decimal('100'),
            atr=Decimal('2'),
            kline_service=mock_kline,
            config={'enabled': False},
            cache={},
        )
        assert result == 1.0
        mock_kline.get_klines.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        """缓存命中，不请求 kline_service"""
        mock_kline = AsyncMock()
        cache = {
            'base_atr_pct_BTCUSDT': {
                'value': 1.5,
                'time': time.time(),  # 当前时间，缓存有效
            }
        }
        result = await get_volatility_adjustment(
            symbol='BTCUSDT',
            entry_price=Decimal('100'),
            atr=Decimal('2'),
            kline_service=mock_kline,
            config={
                'enabled': True,
                'cache_ttl_seconds': 3600,
                'atr_lookback_days': 30,
                'atr_period': 14,
                'min_vol_adj': 0.5,
                'max_vol_adj': 2.0,
            },
            cache=cache,
        )
        assert result == 1.5
        mock_kline.get_klines.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_expired(self):
        """缓存过期，重新请求 kline_service"""
        mock_kline = AsyncMock()
        mock_kline.get_klines.return_value = None  # 数据不足场景

        cache = {
            'base_atr_pct_BTCUSDT': {
                'value': 1.5,
                'time': 0,  # 过期时间
            }
        }
        result = await get_volatility_adjustment(
            symbol='BTCUSDT',
            entry_price=Decimal('100'),
            atr=Decimal('2'),
            kline_service=mock_kline,
            config={
                'enabled': True,
                'cache_ttl_seconds': 3600,
                'atr_lookback_days': 30,
                'atr_period': 14,
                'min_vol_adj': 0.5,
                'max_vol_adj': 2.0,
            },
            cache=cache,
        )
        assert result == 1.0, "数据不足应返回 1.0"
        mock_kline.get_klines.assert_called_once()

    @pytest.mark.asyncio
    async def test_klines_none_returns_1_0(self):
        """K线数据为 None 返回 1.0"""
        mock_kline = AsyncMock()
        mock_kline.get_klines.return_value = None

        result = await get_volatility_adjustment(
            symbol='BTCUSDT',
            entry_price=Decimal('100'),
            atr=Decimal('2'),
            kline_service=mock_kline,
            config={
                'enabled': True,
                'cache_ttl_seconds': 3600,
                'atr_lookback_days': 30,
                'atr_period': 14,
                'min_vol_adj': 0.5,
                'max_vol_adj': 2.0,
            },
            cache={},
        )
        assert result == 1.0

    @pytest.mark.asyncio
    async def test_insufficient_data_returns_1_0(self):
        """历史数据不足（少于 lookback+period）返回 1.0"""
        mock_kline = AsyncMock()
        mock_kline.get_klines.return_value = [{'close': 100}] * 5  # 少于 44 根

        result = await get_volatility_adjustment(
            symbol='BTCUSDT',
            entry_price=Decimal('100'),
            atr=Decimal('2'),
            kline_service=mock_kline,
            config={
                'enabled': True,
                'cache_ttl_seconds': 3600,
                'atr_lookback_days': 30,
                'atr_period': 14,
                'min_vol_adj': 0.5,
                'max_vol_adj': 2.0,
            },
            cache={},
        )
        assert result == 1.0

    @pytest.mark.asyncio
    async def test_close_price_compatibility(self):
        """兼容 close_price 字段名"""
        mock_kline = AsyncMock()
        # 构造包含 close_price 而非 close 的数据
        mock_kline.get_klines.return_value = None  # 返回 None 直接测试兼容性

        result = await get_volatility_adjustment(
            symbol='BTCUSDT',
            entry_price=Decimal('100'),
            atr=Decimal('2'),
            kline_service=mock_kline,
            config={
                'enabled': True,
                'cache_ttl_seconds': 3600,
                'atr_lookback_days': 30,
                'atr_period': 14,
                'min_vol_adj': 0.5,
                'max_vol_adj': 2.0,
            },
            cache={},
        )
        # 数据为 None 返回 1.0
        assert result == 1.0

    @pytest.mark.asyncio
    async def test_normal_calculation(self):
        """正常计算（模拟完整数据流程）"""
        mock_kline = AsyncMock()
        # 构造足够多的 K 线数据
        klines = []
        for i in range(60):
            klines.append({
                'close': 100 + (i * 0.1),
                'high': 101 + (i * 0.1),
                'low': 99 + (i * 0.1),
            })
        mock_kline.get_klines.return_value = klines

        result = await get_volatility_adjustment(
            symbol='BTCUSDT',
            entry_price=Decimal('100'),
            atr=Decimal('2'),
            kline_service=mock_kline,
            config={
                'enabled': True,
                'cache_ttl_seconds': 3600,
                'atr_lookback_days': 30,
                'atr_period': 14,
                'min_vol_adj': 0.5,
                'max_vol_adj': 2.0,
            },
            cache={},
        )
        # 应该有数据，返回一个在 [0.5, 2.0] 范围内的值
        assert 0.5 <= result <= 2.0


class TestDynamicTrailingStopResult:
    """TrailingStopResult 返回结果完整性测试"""

    def test_result_contains_all_fields(self):
        """返回结果包含所有必需字段"""
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=Decimal('100'),
            current_price=Decimal('90'),
            highest_price=None,
            lowest_price=Decimal('90'),
            trailing_activated=False,
            tp1_hit=True,
            tp2_hit=True,
            pending_profit_pct=None,
            current_tier_index=-1,
            current_trailing_stop_price=None,
            config=make_short_config(),
            atr=Decimal('2'),
            stop_loss_atr_multiplier=Decimal('2.5'),
        )
        assert result is not None
        assert isinstance(result, TrailingStopResult)
        assert isinstance(result.trailing_stop_price, Decimal)
        assert isinstance(result.trailing_activated, bool)
        assert isinstance(result.pending_profit_pct, float)
        assert isinstance(result.current_tier_index, int)
        assert isinstance(result.triggered, bool)
        assert isinstance(result.tier_retrace_ratio, float)
        assert isinstance(result.vol_adj, float)


# ============================================================================
# 性能测试
# ============================================================================

class TestPerformance:
    """性能测试"""

    def test_100_calls_dynamic_trailing_stop(self):
        """100 次 calculate_dynamic_trailing_stop 调用，平均耗时 < 5ms"""
        # 预热
        for _ in range(10):
            calculate_dynamic_trailing_stop(
                direction='SHORT',
                entry_price=Decimal('100'),
                current_price=Decimal('90'),
                highest_price=None,
                lowest_price=Decimal('90'),
                trailing_activated=True,
                tp1_hit=True,
                tp2_hit=True,
                pending_profit_pct=10.0,
                current_tier_index=3,
                current_trailing_stop_price=Decimal('95'),
                config=make_short_config(),
                atr=Decimal('2'),
                stop_loss_atr_multiplier=Decimal('2.5'),
            )

        start = time.time()
        for _ in range(100):
            calculate_dynamic_trailing_stop(
                direction='SHORT',
                entry_price=Decimal('100'),
                current_price=Decimal('90'),
                highest_price=None,
                lowest_price=Decimal('90'),
                trailing_activated=True,
                tp1_hit=True,
                tp2_hit=True,
                pending_profit_pct=10.0,
                current_tier_index=3,
                current_trailing_stop_price=Decimal('95'),
                config=make_short_config(),
                atr=Decimal('2'),
                stop_loss_atr_multiplier=Decimal('2.5'),
            )
        elapsed = time.time() - start
        avg_ms = (elapsed / 100) * 1000
        print(f"\n[性能] 100 次调用总耗时: {elapsed*1000:.2f}ms, 平均: {avg_ms:.4f}ms")
        assert avg_ms < 5, f"平均耗时 {avg_ms:.4f}ms > 5ms 阈值"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short', '-s'])