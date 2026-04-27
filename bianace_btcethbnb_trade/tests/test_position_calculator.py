#!/usr/bin/env python3
"""
仓位计算模块测试

测试 core/position_calculator.py 的核心功能：
1. 仓位参数计算
2. 止损百分比计算
3. 仓位系数获取
4. 保证金使用率检查
5. 名义价值检查
6. 仓位调整计算
"""

import pytest
from decimal import Decimal
from unittest.mock import Mock

from core.position_calculator import PositionCalculator, get_position_calculator


class TestPositionCalculator:
    """仓位计算器测试类"""

    @pytest.fixture
    def position_calculator(self):
        """创建仓位计算器实例"""
        # Mock 策略参数
        mock_params = Mock()
        mock_params.get = Mock(side_effect=lambda key, default=None: {
            'position_sizing.min_stop_loss_pct': Decimal('0.03'),
            'position_sizing.max_stop_loss_pct': Decimal('0.07'),
            'position_sizing.risk_amount': Decimal('10'),
            'position_sizing.max_position_notional': Decimal('1500'),
            'position_sizing.max_total_notional': Decimal('4000'),
            'position_sizing.position_coefficient.S': Decimal('0.5'),
            'position_sizing.position_coefficient.A': Decimal('0.3'),
            'position_sizing.position_coefficient.B': Decimal('0.2'),
            'account.total_capital': Decimal('500'),
            'account.single_position_margin': Decimal('30'),
            'account.max_total_margin_ratio': Decimal('0.3'),
            'risk_management.max_margin_usage': Decimal('0.6'),
            'signal_grades.S': {'max_leverage': 5},
            'signal_grades.A': {'max_leverage': 4},
            'signal_grades.B': {'max_leverage': 3}
        }.get(key, default))
        
        return PositionCalculator(params=mock_params)

    def test_init(self, position_calculator):
        """测试初始化"""
        assert position_calculator is not None
        assert position_calculator.params is not None

    def test_calculate_position_long(self, position_calculator):
        """测试计算多头仓位"""
        # 准备测试数据
        symbol = 'BTCUSDT'
        entry_price = Decimal('50000')
        stop_loss_price = Decimal('48000')  # 止损 4%
        direction = 1  # 多头
        signal_grade = 'A'
        
        # 执行测试
        position = position_calculator.calculate_position(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            direction=direction,
            signal_grade=signal_grade
        )
        
        # 验证结果
        assert position['symbol'] == symbol
        assert position['entry_price'] == entry_price
        assert position['stop_loss_price'] == stop_loss_price
        assert position['direction'] == direction
        assert position['signal_grade'] == signal_grade
        
        # 验证止损百分比
        expected_stop_pct = (entry_price - stop_loss_price) / entry_price
        assert abs(position['stop_loss_pct'] - expected_stop_pct) < Decimal('0.0001')
        
        # 验证风险金额
        assert position['risk_amount'] == Decimal('10')
        
        # 验证仓位系数（A 级 = 30%）
        assert position['position_coefficient'] == Decimal('0.3')
        
        # 验证杠杆（A 级 = 4x）
        assert position['leverage'] == 4

    def test_calculate_position_short(self, position_calculator):
        """测试计算空头仓位"""
        # 准备测试数据
        symbol = 'ETHUSDT'
        entry_price = Decimal('3000')
        stop_loss_price = Decimal('3150')  # 止损 5%
        direction = -1  # 空头
        signal_grade = 'S'
        
        # 执行测试
        position = position_calculator.calculate_position(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            direction=direction,
            signal_grade=signal_grade
        )
        
        # 验证结果
        assert position['direction'] == direction
        assert position['signal_grade'] == 'S'
        
        # 验证止损百分比（空头）
        expected_stop_pct = (stop_loss_price - entry_price) / entry_price
        assert abs(position['stop_loss_pct'] - expected_stop_pct) < Decimal('0.0001')
        
        # 验证仓位系数（S 级 = 50%）
        assert position['position_coefficient'] == Decimal('0.5')
        
        # 验证杠杆（S 级 = 5x）
        assert position['leverage'] == 5

    def test_calculate_position_grade_b(self, position_calculator):
        """测试 B 级信号仓位计算"""
        # 准备测试数据
        symbol = 'BNBUSDT'
        entry_price = Decimal('300')
        stop_loss_price = Decimal('285')  # 止损 5%
        direction = 1
        signal_grade = 'B'
        
        # 执行测试
        position = position_calculator.calculate_position(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            direction=direction,
            signal_grade=signal_grade
        )
        
        # 验证仓位系数（B 级 = 20%）
        assert position['position_coefficient'] == Decimal('0.2')
        
        # 验证杠杆（B 级 = 3x）
        assert position['leverage'] == 3

    def test_calculate_stop_loss_percentage_long(self, position_calculator):
        """测试计算多头止损百分比"""
        # 准备测试数据
        entry_price = Decimal('50000')
        stop_loss_price = Decimal('48000')
        direction = 1  # 多头
        
        # 执行测试
        stop_pct = position_calculator._calculate_stop_loss_percentage(
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            direction=direction
        )
        
        # 验证结果
        expected = (entry_price - stop_loss_price) / entry_price
        assert abs(stop_pct - expected) < Decimal('0.0001')

    def test_calculate_stop_loss_percentage_short(self, position_calculator):
        """测试计算空头止损百分比"""
        # 准备测试数据
        entry_price = Decimal('3000')
        stop_loss_price = Decimal('3150')
        direction = -1  # 空头
        
        # 执行测试
        stop_pct = position_calculator._calculate_stop_loss_percentage(
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            direction=direction
        )
        
        # 验证结果
        expected = (stop_loss_price - entry_price) / entry_price
        assert abs(stop_pct - expected) < Decimal('0.0001')

    def test_validate_stop_loss_range_valid(self, position_calculator):
        """测试验证止损幅度（有效范围）"""
        # 测试 3% 止损
        assert position_calculator._validate_stop_loss_range(Decimal('0.03')) is True
        
        # 测试 5% 止损
        assert position_calculator._validate_stop_loss_range(Decimal('0.05')) is True
        
        # 测试 7% 止损
        assert position_calculator._validate_stop_loss_range(Decimal('0.07')) is True

    def test_validate_stop_loss_range_invalid(self, position_calculator):
        """测试验证止损幅度（无效范围）"""
        # 测试 2% 止损（太小）
        assert position_calculator._validate_stop_loss_range(Decimal('0.02')) is False
        
        # 测试 8% 止损（太大）
        assert position_calculator._validate_stop_loss_range(Decimal('0.08')) is False

    def test_get_position_coefficient(self, position_calculator):
        """测试获取仓位系数"""
        # 测试 S 级
        assert position_calculator._get_position_coefficient('S') == Decimal('0.5')
        
        # 测试 A 级
        assert position_calculator._get_position_coefficient('A') == Decimal('0.3')
        
        # 测试 B 级
        assert position_calculator._get_position_coefficient('B') == Decimal('0.2')

    def test_check_total_margin_usage_allowed(self, position_calculator):
        """测试检查总保证金使用率（允许）"""
        # 准备测试数据
        current_positions = [
            {'margin': Decimal('10')},
            {'margin': Decimal('15')}
        ]
        new_position = {'margin': Decimal('20')}
        
        # 执行测试
        allowed, reason = position_calculator.check_total_margin_usage(
            current_positions, new_position
        )
        
        # 验证结果（总保证金 45U，总资金 500U，占比 9%，允许）
        assert allowed is True
        assert "安全范围内" in reason

    def test_check_total_margin_usage_exceeded(self, position_calculator):
        """测试检查总保证金使用率（超限）"""
        # 准备测试数据
        current_positions = [
            {'margin': Decimal('80')},
            {'margin': Decimal('70')}
        ]
        new_position = {'margin': Decimal('50')}
        
        # 执行测试
        allowed, reason = position_calculator.check_total_margin_usage(
            current_positions, new_position
        )
        
        # 验证结果（总保证金 200U，总资金 500U，占比 40%，超过 30% 上限）
        assert allowed is False
        assert "超过上限" in reason

    def test_check_total_margin_usage_warning(self, position_calculator):
        """测试检查总保证金使用率（预警）"""
        # 准备测试数据
        current_positions = [
            {'margin': Decimal('180')}
        ]
        new_position = {'margin': Decimal('130')}
        
        # 执行测试
        allowed, reason = position_calculator.check_total_margin_usage(
            current_positions, new_position
        )
        
        # 验证结果（总保证金 310U，总资金 500U，占比 62%，超过 60% 预警线，但不超过 30% 上限）
        assert allowed is False
        assert "预警线" in reason

    def test_check_total_notional_value_allowed(self, position_calculator):
        """测试检查总名义价值（允许）"""
        # 准备测试数据
        current_positions = [
            {'notional_value': Decimal('1000')},
            {'notional_value': Decimal('800')}
        ]
        new_position = {'notional_value': Decimal('500')}
        
        # 执行测试
        allowed, reason = position_calculator.check_total_notional_value(
            current_positions, new_position
        )
        
        # 验证结果（总名义价值 2300U < 4000U 上限）
        assert allowed is True
        assert "安全范围内" in reason

    def test_check_total_notional_value_exceeded(self, position_calculator):
        """测试检查总名义价值（超限）"""
        # 准备测试数据
        current_positions = [
            {'notional_value': Decimal('2000')},
            {'notional_value': Decimal('1500')}
        ]
        new_position = {'notional_value': Decimal('1000')}
        
        # 执行测试
        allowed, reason = position_calculator.check_total_notional_value(
            current_positions, new_position
        )
        
        # 验证结果（总名义价值 4500U > 4000U 上限）
        assert allowed is False
        assert "超过上限" in reason

    def test_calculate_position_adjustment_increase(self, position_calculator):
        """测试仓位调整（加仓）"""
        # 准备测试数据
        original_position = {
            'quantity': Decimal('0.01'),
            'notional_value': Decimal('500'),
            'margin': Decimal('10'),
            'leverage': 5
        }
        adjustment_ratio = Decimal('0.5')  # 加仓 50%
        
        # 执行测试
        adjusted = position_calculator.calculate_position_adjustment(
            original_position, adjustment_ratio
        )
        
        # 验证结果
        assert adjusted['quantity'] == Decimal('0.015')
        assert adjusted['notional_value'] == Decimal('750')
        assert adjusted['margin'] == Decimal('15')

    def test_calculate_position_adjustment_decrease(self, position_calculator):
        """测试仓位调整（减仓）"""
        # 准备测试数据
        original_position = {
            'quantity': Decimal('0.01'),
            'notional_value': Decimal('500'),
            'margin': Decimal('10'),
            'leverage': 5
        }
        adjustment_ratio = Decimal('-0.3')  # 减仓 30%
        
        # 执行测试
        adjusted = position_calculator.calculate_position_adjustment(
            original_position, adjustment_ratio
        )
        
        # 验证结果
        assert adjusted['quantity'] == Decimal('0.007')
        assert adjusted['notional_value'] == Decimal('350')
        assert adjusted['margin'] == Decimal('7')

    def test_get_position_summary(self, position_calculator):
        """测试生成仓位摘要"""
        # 准备测试数据
        position = {
            'symbol': 'BTCUSDT',
            'direction': 1,
            'leverage': 5,
            'position_coefficient': Decimal('0.5'),
            'margin': Decimal('10'),
            'actual_notional_value': Decimal('500'),
            'quantity': Decimal('0.01'),
            'risk_amount': Decimal('10'),
            'risk_ratio': Decimal('0.02')
        }
        
        # 执行测试
        summary = position_calculator.get_position_summary(position)
        
        # 验证结果
        assert 'BTCUSDT' in summary
        assert '5' in summary  # 杠杆
        assert '50%' in summary  # 仓位系数
        assert '10.00U' in summary  # 保证金

    def test_calculate_position_with_small_stop_loss(self, position_calculator):
        """测试止损幅度过小的情况"""
        # 准备测试数据（止损 2%，小于最小值 3%）
        symbol = 'BTCUSDT'
        entry_price = Decimal('50000')
        stop_loss_price = Decimal('49000')  # 止损 2%
        direction = 1
        signal_grade = 'A'
        
        # 执行测试
        position = position_calculator.calculate_position(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            direction=direction,
            signal_grade=signal_grade
        )
        
        # 验证结果（应该调整到最小止损幅度 3%）
        assert position['stop_loss_pct'] >= Decimal('0.03')

    def test_calculate_position_with_large_stop_loss(self, position_calculator):
        """测试止损幅度过大的情况"""
        # 准备测试数据（止损 10%，大于最大值 7%）
        symbol = 'BTCUSDT'
        entry_price = Decimal('50000')
        stop_loss_price = Decimal('45000')  # 止损 10%
        direction = 1
        signal_grade = 'A'
        
        # 执行测试
        position = position_calculator.calculate_position(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            direction=direction,
            signal_grade=signal_grade
        )
        
        # 验证结果（应该调整到最大止损幅度 7%）
        assert position['stop_loss_pct'] <= Decimal('0.07')

    def test_calculate_position_margin_limit(self, position_calculator):
        """测试保证金限制"""
        # 准备测试数据（会导致保证金超过单仓上限 30U）
        symbol = 'BTCUSDT'
        entry_price = Decimal('50000')
        stop_loss_price = Decimal('48000')  # 止损 4%
        direction = 1
        signal_grade = 'S'  # S 级，仓位系数 50%
        
        # 执行测试
        position = position_calculator.calculate_position(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            direction=direction,
            signal_grade=signal_grade
        )
        
        # 验证结果（保证金应该不超过单仓上限）
        assert position['margin'] <= Decimal('30')


class TestPositionCalculatorSingleton:
    """测试仓位计算器单例模式"""

    def test_get_position_calculator_singleton(self):
        """测试获取全局实例"""
        # 清除全局实例
        import core.position_calculator as pc_module
        pc_module._global_calculator = None
        
        # 获取实例
        calculator1 = get_position_calculator()
        calculator2 = get_position_calculator()
        
        # 应该是同一个实例
        assert calculator1 is calculator2

    def test_get_position_calculator_with_params(self):
        """测试使用自定义参数获取实例"""
        # 清除全局实例
        import core.position_calculator as pc_module
        pc_module._global_calculator = None
        
        # Mock 参数
        mock_params = Mock()
        
        # 获取实例
        calculator = get_position_calculator(params=mock_params)
        
        # 验证
        assert calculator.params == mock_params


class TestPositionCalculatorFormulas:
    """测试仓位计算公式"""

    @pytest.fixture
    def position_calculator(self):
        """创建仓位计算器实例"""
        mock_params = Mock()
        mock_params.get = Mock(side_effect=lambda key, default=None: {
            'position_sizing.min_stop_loss_pct': Decimal('0.03'),
            'position_sizing.max_stop_loss_pct': Decimal('0.07'),
            'position_sizing.risk_amount': Decimal('10'),
            'position_sizing.max_position_notional': Decimal('1500'),
            'position_sizing.position_coefficient.A': Decimal('0.3'),
            'account.total_capital': Decimal('500'),
            'account.single_position_margin': Decimal('30'),
            'signal_grades.A': {'max_leverage': 4}
        }.get(key, default))
        
        return PositionCalculator(params=mock_params)

    def test_formula_base_notional_value(self, position_calculator):
        """测试基础名义价值计算公式"""
        # 基础名义价值 = 风险金额 / 止损百分比
        risk_amount = Decimal('10')
        stop_loss_pct = Decimal('0.04')  # 4%
        
        expected = risk_amount / stop_loss_pct  # 10 / 0.04 = 250
        
        # 通过实际计算验证
        position = position_calculator.calculate_position(
            symbol='BTCUSDT',
            entry_price=Decimal('50000'),
            stop_loss_price=Decimal('48000'),  # 4%
            direction=1,
            signal_grade='A'
        )
        
        # 基础名义价值应该接近 250U
        assert abs(position['base_notional_value'] - expected) < Decimal('1')

    def test_formula_actual_notional_value(self, position_calculator):
        """测试实际名义价值计算公式"""
        # 实际名义价值 = 基础名义价值 × 仓位系数
        base_notional = Decimal('250')
        position_coefficient = Decimal('0.3')  # A 级
        
        expected = base_notional * position_coefficient  # 250 * 0.3 = 75
        
        # 通过实际计算验证
        position = position_calculator.calculate_position(
            symbol='BTCUSDT',
            entry_price=Decimal('50000'),
            stop_loss_price=Decimal('48000'),
            direction=1,
            signal_grade='A'
        )
        
        # 实际名义价值应该接近 75U
        assert abs(position['actual_notional_value'] - expected) < Decimal('1')

    def test_formula_margin(self, position_calculator):
        """测试保证金计算公式"""
        # 保证金 = 实际名义价值 / 杠杆
        actual_notional = Decimal('75')
        leverage = 4  # A 级
        
        expected = actual_notional / leverage  # 75 / 4 = 18.75
        
        # 通过实际计算验证
        position = position_calculator.calculate_position(
            symbol='BTCUSDT',
            entry_price=Decimal('50000'),
            stop_loss_price=Decimal('48000'),
            direction=1,
            signal_grade='A'
        )
        
        # 保证金应该接近 18.75U
        assert abs(position['margin'] - expected) < Decimal('0.1')

    def test_formula_quantity(self, position_calculator):
        """测试合约数量计算公式"""
        # 合约数量 = 实际名义价值 / 开仓价格
        actual_notional = Decimal('75')
        entry_price = Decimal('50000')
        
        expected = actual_notional / entry_price  # 75 / 50000 = 0.0015
        
        # 通过实际计算验证
        position = position_calculator.calculate_position(
            symbol='BTCUSDT',
            entry_price=entry_price,
            stop_loss_price=Decimal('48000'),
            direction=1,
            signal_grade='A'
        )
        
        # 合约数量应该接近 0.0015
        assert abs(position['quantity'] - expected) < Decimal('0.0001')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
