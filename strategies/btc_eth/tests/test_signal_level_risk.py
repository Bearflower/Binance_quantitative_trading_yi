"""
测试信号等级止盈止损功能

测试覆盖：
1. _get_grade_risk 向后兼容（signal_levels 不存在时返回全局 risk_config）
2. _get_grade_risk 各等级（S/A/B/C）取值正确
3. _get_grade_risk 未知等级回退到 A 级
4. PositionState grade 字段
5. 各信号等级参数差异化
"""
import copy
from typing import Dict, Optional
from decimal import Decimal
from datetime import datetime

import pytest

from strategies.btc_eth.strategy import BTCEthStrategy, PositionState


# ============================================================
# 测试夹具：模拟策略对象
# ============================================================

# 从 config.yaml 提取的 signal_levels 实际数据
SIGNAL_LEVELS = {
    'S': {
        'stop_loss_atr_multiplier': 2.5,
        'partial_take_profit': {
            'tp1_atr_multiplier': 6.0,
            'tp1_close_ratio': 0.15,
            'tp2_atr_multiplier': 9.0,
            'tp2_close_ratio': 0.35,
            'remaining_ratio': 0.50,
        },
        'dynamic_trailing': {
            'enabled': True,
            'activation': {'min_profit_pct': 3.0, 'also_on_tp1': True},
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
            },
        },
        'time_stop': {
            'max_holding_hours': 96,
            'close_ratio': 0.50,
        },
    },
    'A': {
        'stop_loss_atr_multiplier': 1.5,
        'partial_take_profit': {
            'tp1_atr_multiplier': 4.0,
            'tp1_close_ratio': 0.25,
            'tp2_atr_multiplier': 6.0,
            'tp2_close_ratio': 0.25,
            'remaining_ratio': 0.50,
        },
        'dynamic_trailing': {
            'enabled': True,
            'activation': {'min_profit_pct': 1.5, 'also_on_tp1': True},
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
            },
        },
        'time_stop': {
            'max_holding_hours': 72,
            'close_ratio': 0.50,
        },
    },
    'B': {
        'stop_loss_atr_multiplier': 1.2,
        'partial_take_profit': {
            'tp1_atr_multiplier': 3.0,
            'tp1_close_ratio': 0.30,
            'tp2_atr_multiplier': 5.0,
            'tp2_close_ratio': 0.30,
            'remaining_ratio': 0.40,
        },
        'dynamic_trailing': {
            'enabled': True,
            'activation': {'min_profit_pct': 1.0, 'also_on_tp1': True},
            'regression_tiers': [
                {'profit_ceiling': 1.5, 'retrace_ratio': 0.0},
                {'profit_ceiling': 3.0, 'retrace_ratio': 0.5},
                {'profit_ceiling': 6.0, 'retrace_ratio': 0.35},
                {'profit_ceiling': 999.0, 'retrace_ratio': 0.25},
            ],
            'volatility_adjustment': {
                'enabled': True,
                'atr_lookback_days': 30,
                'atr_period': 14,
                'cache_ttl_seconds': 3600,
            },
        },
        'time_stop': {
            'max_holding_hours': 48,
            'close_ratio': 0.60,
        },
    },
    'C': {
        'stop_loss_atr_multiplier': 1.0,
        'partial_take_profit': {
            'tp1_atr_multiplier': 2.0,
            'tp1_close_ratio': 0.40,
            'tp2_atr_multiplier': 3.5,
            'tp2_close_ratio': 0.40,
            'remaining_ratio': 0.20,
        },
        'dynamic_trailing': {
            'enabled': True,
            'activation': {'min_profit_pct': 0.5, 'also_on_tp1': True},
            'regression_tiers': [
                {'profit_ceiling': 1.0, 'retrace_ratio': 0.0},
                {'profit_ceiling': 2.0, 'retrace_ratio': 0.5},
                {'profit_ceiling': 4.0, 'retrace_ratio': 0.35},
                {'profit_ceiling': 999.0, 'retrace_ratio': 0.25},
            ],
            'volatility_adjustment': {
                'enabled': True,
                'atr_lookback_days': 30,
                'atr_period': 14,
                'cache_ttl_seconds': 3600,
            },
        },
        'time_stop': {
            'max_holding_hours': 24,
            'close_ratio': 0.80,
        },
    },
}

# 带有 signal_levels 的完整 risk_config
RISK_CONFIG_WITH_LEVELS = {
    'signal_levels': copy.deepcopy(SIGNAL_LEVELS),
    'max_position_size': 0.1,
    'close_limit_order': {'enabled': True, 'timeout_seconds': 10},
}

# 不含 signal_levels 的旧版 risk_config（向后兼容）
RISK_CONFIG_WITHOUT_LEVELS = {
    'max_position_size': 0.1,
    'stop_loss_atr_multiplier': 1.5,
    'close_limit_order': {'enabled': True, 'timeout_seconds': 10},
}


class MockStrategy:
    """模拟策略对象，用于测试 _get_grade_risk 方法"""
    
    def __init__(self, risk_config: Dict):
        self.risk_config = risk_config
    
    _get_grade_risk = BTCEthStrategy._get_grade_risk


# ============================================================
# 测试 1: _get_grade_risk 向后兼容
# ============================================================

class TestGetGradeRiskBackwardCompatibility:
    """测试 _get_grade_risk 在 signal_levels 不存在时的向后兼容性"""

    def test_returns_global_risk_config_when_signal_levels_missing(self):
        """当 signal_levels 不存在时，返回全局 risk_config"""
        strategy = MockStrategy(RISK_CONFIG_WITHOUT_LEVELS)
        result = strategy._get_grade_risk('A')
        assert result is RISK_CONFIG_WITHOUT_LEVELS, "应返回全局 risk_config 的引用"
        assert result['stop_loss_atr_multiplier'] == 1.5

    def test_returns_global_risk_config_for_any_grade(self):
        """当 signal_levels 不存在时，任何等级都返回全局 risk_config"""
        strategy = MockStrategy(RISK_CONFIG_WITHOUT_LEVELS)
        for grade in ('S', 'A', 'B', 'C', 'D', ''):
            result = strategy._get_grade_risk(grade)
            assert result is RISK_CONFIG_WITHOUT_LEVELS

    def test_returns_global_config_when_signal_levels_is_empty(self):
        """当 signal_levels 为空字典时，应返回全局 risk_config"""
        config = {**RISK_CONFIG_WITHOUT_LEVELS, 'signal_levels': {}}
        strategy = MockStrategy(config)
        result = strategy._get_grade_risk('S')
        assert result is config, "signal_levels 为空字典时，应返回全局 risk_config"


# ============================================================
# 测试 2: _get_grade_risk 各等级取值正确
# ============================================================

class TestGetGradeRiskEachGrade:
    """测试 _get_grade_risk 各等级(S/A/B/C)的取值正确性"""

    @pytest.fixture
    def strategy(self):
        return MockStrategy(RISK_CONFIG_WITH_LEVELS)

    def test_grade_s_returns_s_config(self, strategy):
        """S 级返回 S 级配置"""
        result = strategy._get_grade_risk('S')
        assert result['stop_loss_atr_multiplier'] == 2.5
        assert result['partial_take_profit']['tp1_atr_multiplier'] == 6.0
        assert result['partial_take_profit']['tp2_atr_multiplier'] == 9.0
        assert result['time_stop']['max_holding_hours'] == 96
        assert result['dynamic_trailing']['activation']['min_profit_pct'] == 3.0

    def test_grade_a_returns_a_config(self, strategy):
        """A 级返回 A 级配置"""
        result = strategy._get_grade_risk('A')
        assert result['stop_loss_atr_multiplier'] == 1.5
        assert result['partial_take_profit']['tp1_atr_multiplier'] == 4.0
        assert result['partial_take_profit']['tp2_atr_multiplier'] == 6.0
        assert result['time_stop']['max_holding_hours'] == 72
        assert result['dynamic_trailing']['activation']['min_profit_pct'] == 1.5

    def test_grade_b_returns_b_config(self, strategy):
        """B 级返回 B 级配置"""
        result = strategy._get_grade_risk('B')
        assert result['stop_loss_atr_multiplier'] == 1.2
        assert result['partial_take_profit']['tp1_atr_multiplier'] == 3.0
        assert result['partial_take_profit']['tp2_atr_multiplier'] == 5.0
        assert result['time_stop']['max_holding_hours'] == 48
        assert result['dynamic_trailing']['activation']['min_profit_pct'] == 1.0

    def test_grade_c_returns_c_config(self, strategy):
        """C 级返回 C 级配置"""
        result = strategy._get_grade_risk('C')
        assert result['stop_loss_atr_multiplier'] == 1.0
        assert result['partial_take_profit']['tp1_atr_multiplier'] == 2.0
        assert result['partial_take_profit']['tp2_atr_multiplier'] == 3.5
        assert result['time_stop']['max_holding_hours'] == 24
        assert result['dynamic_trailing']['activation']['min_profit_pct'] == 0.5

    def test_each_grade_returns_unique_config(self, strategy):
        """各等级返回不同的配置对象（相互独立）"""
        result_s = strategy._get_grade_risk('S')
        result_a = strategy._get_grade_risk('A')
        result_b = strategy._get_grade_risk('B')
        result_c = strategy._get_grade_risk('C')
        
        assert result_s is not result_a
        assert result_a is not result_b
        assert result_b is not result_c


# ============================================================
# 测试 3: _get_grade_risk 未知等级回退到 A 级
# ============================================================

class TestGetGradeRiskUnknownGrade:
    """测试 _get_grade_risk 在传入未知等级时的回退行为"""

    @pytest.fixture
    def strategy(self):
        return MockStrategy(RISK_CONFIG_WITH_LEVELS)

    def test_unknown_grade_d_falls_back_to_a(self, strategy):
        """传入 'D' 应返回 A 级配置"""
        result = strategy._get_grade_risk('D')
        assert result['stop_loss_atr_multiplier'] == 1.5
        assert result['partial_take_profit']['tp1_atr_multiplier'] == 4.0

    def test_unknown_grade_empty_string_falls_back_to_a(self, strategy):
        """传入空字符串应返回 A 级配置"""
        result = strategy._get_grade_risk('')
        assert result['stop_loss_atr_multiplier'] == 1.5

    def test_unknown_grade_lowercase_s_falls_back_to_a(self, strategy):
        """传入小写 's' 应返回 A 级配置（区分大小写）"""
        result = strategy._get_grade_risk('s')
        assert result['stop_loss_atr_multiplier'] == 1.5

    def test_unknown_grade_random_falls_back_to_a(self, strategy):
        """传入随机字符串应返回 A 级配置"""
        result = strategy._get_grade_risk('UNKNOWN_GRADE_123')
        assert result['stop_loss_atr_multiplier'] == 1.5


# ============================================================
# 测试 4: PositionState grade 字段
# ============================================================

class TestPositionStateGrade:
    """测试 PositionState 的 grade 字段"""

    def test_grade_defaults_to_empty_string(self):
        """创建 PositionState 后 grade 默认为空字符串"""
        pos = PositionState()
        assert pos.grade == ""

    def test_grade_can_be_set(self):
        """设置 grade 后能正确读取"""
        pos = PositionState()
        pos.grade = "S"
        assert pos.grade == "S"

    def test_grade_all_values(self):
        """所有有效等级都能正确设置和读取"""
        for grade in ('S', 'A', 'B', 'C'):
            pos = PositionState()
            pos.grade = grade
            assert pos.grade == grade

    def test_grade_is_independent_per_instance(self):
        """每个 PositionState 实例的 grade 相互独立"""
        pos1 = PositionState()
        pos2 = PositionState()
        pos1.grade = "S"
        pos2.grade = "A"
        assert pos1.grade == "S"
        assert pos2.grade == "A"
        assert pos1.grade != pos2.grade

    def test_grade_does_not_affect_other_fields(self):
        """设置 grade 不影响其他字段"""
        pos = PositionState()
        pos.entry_price = Decimal('50000')
        pos.direction = 'LONG'
        pos.grade = 'S'
        
        assert pos.entry_price == Decimal('50000')
        assert pos.direction == 'LONG'
        assert pos.current_quantity == Decimal('0')


# ============================================================
# 测试 5: 各信号等级参数差异化
# ============================================================

class TestSignalLevelParameterDifferentiation:
    """测试各信号等级的参数差异化配置"""

    @pytest.fixture
    def strategy(self):
        return MockStrategy(RISK_CONFIG_WITH_LEVELS)

    def test_stop_loss_atr_multiplier_descending(self, strategy):
        """止损ATR倍数：S > A > B > C，等级越高止损越宽松"""
        s = strategy._get_grade_risk('S')['stop_loss_atr_multiplier']
        a = strategy._get_grade_risk('A')['stop_loss_atr_multiplier']
        b = strategy._get_grade_risk('B')['stop_loss_atr_multiplier']
        c = strategy._get_grade_risk('C')['stop_loss_atr_multiplier']
        
        assert s > a > b > c, f"预期 S({s}) > A({a}) > B({b}) > C({c})"
        assert s == 2.5
        assert a == 1.5
        assert b == 1.2
        assert c == 1.0

    def test_tp1_atr_multiplier_descending(self, strategy):
        """TP1 ATR倍数：S > A > B > C"""
        s = strategy._get_grade_risk('S')['partial_take_profit']['tp1_atr_multiplier']
        a = strategy._get_grade_risk('A')['partial_take_profit']['tp1_atr_multiplier']
        b = strategy._get_grade_risk('B')['partial_take_profit']['tp1_atr_multiplier']
        c = strategy._get_grade_risk('C')['partial_take_profit']['tp1_atr_multiplier']
        
        assert s > a > b > c
        assert s == 6.0
        assert a == 4.0
        assert b == 3.0
        assert c == 2.0

    def test_tp2_atr_multiplier_descending(self, strategy):
        """TP2 ATR倍数：S > A > B > C"""
        s = strategy._get_grade_risk('S')['partial_take_profit']['tp2_atr_multiplier']
        a = strategy._get_grade_risk('A')['partial_take_profit']['tp2_atr_multiplier']
        b = strategy._get_grade_risk('B')['partial_take_profit']['tp2_atr_multiplier']
        c = strategy._get_grade_risk('C')['partial_take_profit']['tp2_atr_multiplier']
        
        assert s > a > b > c
        assert s == 9.0
        assert a == 6.0
        assert b == 5.0
        assert c == 3.5

    def test_tp1_close_ratio_ascending(self, strategy):
        """TP1平仓比例：S < A < B < C，等级越低TP1平仓越多"""
        s = strategy._get_grade_risk('S')['partial_take_profit']['tp1_close_ratio']
        a = strategy._get_grade_risk('A')['partial_take_profit']['tp1_close_ratio']
        b = strategy._get_grade_risk('B')['partial_take_profit']['tp1_close_ratio']
        c = strategy._get_grade_risk('C')['partial_take_profit']['tp1_close_ratio']
        
        assert s < a < b < c, f"预期 S({s}) < A({a}) < B({b}) < C({c})"
        assert s == 0.15
        assert a == 0.25
        assert b == 0.30
        assert c == 0.40

    def test_remaining_ratio_descending(self, strategy):
        """剩余持仓比例：S == A > B > C，等级越高保留越多"""
        s = strategy._get_grade_risk('S')['partial_take_profit']['remaining_ratio']
        a = strategy._get_grade_risk('A')['partial_take_profit']['remaining_ratio']
        b = strategy._get_grade_risk('B')['partial_take_profit']['remaining_ratio']
        c = strategy._get_grade_risk('C')['partial_take_profit']['remaining_ratio']
        
        assert s >= a >= b > c
        assert s == 0.50
        assert a == 0.50
        assert b == 0.40
        assert c == 0.20

    def test_trailing_activation_min_profit_descending(self, strategy):
        """动态追踪激活最小利润：S > A > B > C"""
        s = strategy._get_grade_risk('S')['dynamic_trailing']['activation']['min_profit_pct']
        a = strategy._get_grade_risk('A')['dynamic_trailing']['activation']['min_profit_pct']
        b = strategy._get_grade_risk('B')['dynamic_trailing']['activation']['min_profit_pct']
        c = strategy._get_grade_risk('C')['dynamic_trailing']['activation']['min_profit_pct']
        
        assert s > a > b > c
        assert s == 3.0
        assert a == 1.5
        assert b == 1.0
        assert c == 0.5

    def test_time_stop_max_holding_hours_descending(self, strategy):
        """时间止损最大持仓小时数：S > A > B > C"""
        s = strategy._get_grade_risk('S')['time_stop']['max_holding_hours']
        a = strategy._get_grade_risk('A')['time_stop']['max_holding_hours']
        b = strategy._get_grade_risk('B')['time_stop']['max_holding_hours']
        c = strategy._get_grade_risk('C')['time_stop']['max_holding_hours']
        
        assert s > a > b > c
        assert s == 96
        assert a == 72
        assert b == 48
        assert c == 24

    def test_time_stop_close_ratio_ascending(self, strategy):
        """时间止损平仓比例：S == A < B < C，等级越低时间止损平仓越多"""
        s = strategy._get_grade_risk('S')['time_stop']['close_ratio']
        a = strategy._get_grade_risk('A')['time_stop']['close_ratio']
        b = strategy._get_grade_risk('B')['time_stop']['close_ratio']
        c = strategy._get_grade_risk('C')['time_stop']['close_ratio']
        
        assert s <= a <= b < c
        assert s == 0.50
        assert a == 0.50
        assert b == 0.60
        assert c == 0.80

    def test_volatility_adjustment_config_identical(self, strategy):
        """波动率调整配置在所有等级中应一致"""
        for grade in ('S', 'A', 'B', 'C'):
            va = strategy._get_grade_risk(grade)['dynamic_trailing']['volatility_adjustment']
            assert va['enabled'] is True
            assert va['atr_lookback_days'] == 30
            assert va['atr_period'] == 14
            assert va['cache_ttl_seconds'] == 3600