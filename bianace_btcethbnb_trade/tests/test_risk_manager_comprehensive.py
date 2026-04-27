#!/usr/bin/env python3
"""
风险管理模块综合测试

测试范围：
1. 止损止盈计算边界情况
2. 移动止损触发条件
3. 风控机制完整流程
4. 异常情况处理
"""

import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from core.risk_manager import RiskManager, get_risk_manager
from config.strategy_params import StrategyParams


class TestRiskManagerStopLoss(unittest.TestCase):
    """测试止损计算"""

    def setUp(self):
        """测试前准备"""
        self.risk_manager = get_risk_manager()

    def test_calculate_stop_loss_long(self):
        """测试多头止损计算"""
        entry_price = Decimal('50000')
        direction = 1  # 多头
        stop_loss_pct = Decimal('0.02')  # 2%

        stop_loss = self.risk_manager.calculate_stop_loss(
            entry_price, direction, stop_loss_pct
        )

        # 多头止损价 = 开仓价 × (1 - 止损幅度)
        expected = Decimal('49000')
        self.assertEqual(stop_loss, expected)

    def test_calculate_stop_loss_short(self):
        """测试空头止损计算"""
        entry_price = Decimal('50000')
        direction = -1  # 空头
        stop_loss_pct = Decimal('0.02')  # 2%

        stop_loss = self.risk_manager.calculate_stop_loss(
            entry_price, direction, stop_loss_pct
        )

        # 空头止损价 = 开仓价 × (1 + 止损幅度)
        expected = Decimal('51000')
        self.assertEqual(stop_loss, expected)

    def test_calculate_stop_loss_zero_stop_pct(self):
        """测试止损幅度为0的边界情况"""
        entry_price = Decimal('50000')
        direction = 1
        stop_loss_pct = Decimal('0')

        stop_loss = self.risk_manager.calculate_stop_loss(
            entry_price, direction, stop_loss_pct
        )

        # 止损幅度为0，止损价应该等于开仓价
        self.assertEqual(stop_loss, entry_price)

    def test_calculate_stop_loss_large_stop_pct(self):
        """测试大止损幅度的边界情况"""
        entry_price = Decimal('50000')
        direction = 1
        stop_loss_pct = Decimal('0.5')  # 50%

        stop_loss = self.risk_manager.calculate_stop_loss(
            entry_price, direction, stop_loss_pct
        )

        # 止损价 = 50000 × (1 - 0.5) = 25000
        expected = Decimal('25000')
        self.assertEqual(stop_loss, expected)

    def test_calculate_atr_based_stop_loss_long(self):
        """测试基于ATR的多头止损计算"""
        entry_price = Decimal('50000')
        direction = 1
        atr14 = Decimal('500')

        stop_loss, stop_distance = self.risk_manager.calculate_atr_based_stop_loss(
            entry_price, direction, atr14
        )

        # 止损距离 = 2.0 × ATR14 = 1000
        expected_distance = Decimal('1000')
        self.assertEqual(stop_distance, expected_distance)

        # 多头止损价 = 开仓价 - 止损距离
        expected_stop = Decimal('49000')
        self.assertEqual(stop_loss, expected_stop)

    def test_calculate_atr_based_stop_loss_short(self):
        """测试基于ATR的空头止损计算"""
        entry_price = Decimal('50000')
        direction = -1
        atr14 = Decimal('500')

        stop_loss, stop_distance = self.risk_manager.calculate_atr_based_stop_loss(
            entry_price, direction, atr14
        )

        # 止损距离 = 2.0 × ATR14 = 1000
        expected_distance = Decimal('1000')
        self.assertEqual(stop_distance, expected_distance)

        # 空头止损价 = 开仓价 + 止损距离
        expected_stop = Decimal('51000')
        self.assertEqual(stop_loss, expected_stop)

    def test_calculate_atr_based_stop_loss_with_key_level(self):
        """测试带关键位的ATR止损计算"""
        entry_price = Decimal('50000')
        direction = 1
        atr14 = Decimal('500')
        key_level_distance = Decimal('1500')  # 关键位距离大于ATR距离

        stop_loss, stop_distance = self.risk_manager.calculate_atr_based_stop_loss(
            entry_price, direction, atr14, key_level_distance
        )

        # 止损距离应该取关键位距离（1500 > 1000）
        self.assertEqual(stop_distance, key_level_distance)

        # 止损价 = 50000 - 1500 = 48500
        expected_stop = Decimal('48500')
        self.assertEqual(stop_loss, expected_stop)

    def test_calculate_atr_based_stop_loss_max_stop(self):
        """测试最大止损幅度限制"""
        entry_price = Decimal('50000')
        direction = 1
        atr14 = Decimal('2000')  # ATR很大
        max_stop_pct = Decimal('0.07')  # 最大7%

        stop_loss, stop_distance = self.risk_manager.calculate_atr_based_stop_loss(
            entry_price, direction, atr14, max_stop_pct=max_stop_pct
        )

        # 止损距离应该被限制在开仓价的7%以内
        max_distance = entry_price * max_stop_pct
        self.assertLessEqual(stop_distance, max_distance)


class TestRiskManagerTakeProfit(unittest.TestCase):
    """测试止盈计算"""

    def setUp(self):
        """测试前准备"""
        self.risk_manager = get_risk_manager()

    def test_calculate_take_profit_levels_long(self):
        """测试多头止盈价位计算"""
        entry_price = Decimal('50000')
        direction = 1
        atr14 = Decimal('500')
        signal_grade = 'A'

        tp_levels = self.risk_manager.calculate_take_profit_levels(
            entry_price, direction, atr14, signal_grade
        )

        # 验证返回的是列表
        self.assertIsInstance(tp_levels, list)
        self.assertEqual(len(tp_levels), 3)

        # 验证每个止盈位包含必要字段
        for tp_info in tp_levels:
            self.assertIn('level', tp_info)
            self.assertIn('price', tp_info)
            self.assertIn('ratio', tp_info)

        # 验证TP1和TP2的价格高于开仓价（多头）
        tp1 = next((tp for tp in tp_levels if tp['level'] == 'TP1'), None)
        tp2 = next((tp for tp in tp_levels if tp['level'] == 'TP2'), None)
        
        self.assertIsNotNone(tp1)
        self.assertIsNotNone(tp2)
        self.assertGreater(tp1['price'], entry_price)
        self.assertGreater(tp2['price'], entry_price)

    def test_calculate_take_profit_levels_short(self):
        """测试空头止盈价位计算"""
        entry_price = Decimal('50000')
        direction = -1
        atr14 = Decimal('500')
        signal_grade = 'A'

        tp_levels = self.risk_manager.calculate_take_profit_levels(
            entry_price, direction, atr14, signal_grade
        )

        # 验证空头止盈价位应该低于开仓价
        tp1 = next((tp for tp in tp_levels if tp['level'] == 'TP1'), None)
        self.assertIsNotNone(tp1)
        self.assertLess(tp1['price'], entry_price)

    def test_calculate_take_profit_levels_ratio(self):
        """测试止盈仓位比例"""
        entry_price = Decimal('50000')
        direction = 1
        atr14 = Decimal('500')
        signal_grade = 'A'

        tp_levels = self.risk_manager.calculate_take_profit_levels(
            entry_price, direction, atr14, signal_grade
        )

        # V6.13.1版本：TP1平仓25%，TP2平仓25%，剩余50%
        tp1 = next((tp for tp in tp_levels if tp['level'] == 'TP1'), None)
        tp2 = next((tp for tp in tp_levels if tp['level'] == 'TP2'), None)
        tp3 = next((tp for tp in tp_levels if tp['level'] == 'TP3'), None)

        self.assertEqual(tp1['ratio'], Decimal('0.25'))
        self.assertEqual(tp2['ratio'], Decimal('0.25'))
        self.assertEqual(tp3['ratio'], Decimal('0.50'))


class TestRiskManagerMarginCheck(unittest.TestCase):
    """测试保证金检查"""

    def setUp(self):
        """测试前准备"""
        self.risk_manager = get_risk_manager()

    def test_check_margin_ratio_safe(self):
        """测试安全保证金率"""
        account_equity = Decimal('10000')
        used_margin = Decimal('5000')

        margin_ratio, risk_level, need_intervention = self.risk_manager.check_margin_ratio(
            account_equity, used_margin
        )

        # 保证金率 = 10000 / 5000 = 2.0，应该安全
        self.assertEqual(margin_ratio, Decimal('2'))
        self.assertEqual(risk_level, 'SAFE')
        self.assertFalse(need_intervention)

    def test_check_margin_ratio_warning(self):
        """测试警告保证金率"""
        account_equity = Decimal('7000')
        used_margin = Decimal('5000')

        margin_ratio, risk_level, need_intervention = self.risk_manager.check_margin_ratio(
            account_equity, used_margin
        )

        # 保证金率 = 7000 / 5000 = 1.4，应该警告
        self.assertEqual(margin_ratio, Decimal('1.4'))
        self.assertEqual(risk_level, 'WARNING')
        self.assertTrue(need_intervention)

    def test_check_margin_ratio_emergency(self):
        """测试紧急保证金率"""
        account_equity = Decimal('5500')
        used_margin = Decimal('5000')

        margin_ratio, risk_level, need_intervention = self.risk_manager.check_margin_ratio(
            account_equity, used_margin
        )

        # 保证金率 = 5500 / 5000 = 1.1，应该紧急
        self.assertEqual(margin_ratio, Decimal('1.1'))
        self.assertEqual(risk_level, 'EMERGENCY')
        self.assertTrue(need_intervention)

    def test_check_margin_ratio_zero_used(self):
        """测试占用保证金为0的边界情况"""
        account_equity = Decimal('10000')
        used_margin = Decimal('0')

        margin_ratio, risk_level, need_intervention = self.risk_manager.check_margin_ratio(
            account_equity, used_margin
        )

        # 占用保证金为0时，应该返回安全状态
        self.assertEqual(risk_level, 'NONE')
        self.assertFalse(need_intervention)

    def test_check_margin_usage(self):
        """测试保证金使用率计算"""
        total_capital = Decimal('10000')
        used_margin = Decimal('2000')

        usage, is_exceeded = self.risk_manager.check_margin_usage(
            total_capital, used_margin
        )

        # 使用率 = 2000 / 10000 = 20%
        expected = Decimal('0.2')
        self.assertEqual(usage, expected)
        self.assertFalse(is_exceeded)

    def test_check_margin_usage_exceeded(self):
        """测试保证金使用率超限"""
        total_capital = Decimal('10000')
        used_margin = Decimal('7000')  # 70%使用率

        usage, is_exceeded = self.risk_manager.check_margin_usage(
            total_capital, used_margin
        )

        # 使用率 = 7000 / 10000 = 70%，超过60%限制
        self.assertTrue(is_exceeded)


class TestRiskManagerTrailingStop(unittest.TestCase):
    """测试移动止损"""

    def setUp(self):
        """测试前准备"""
        self.risk_manager = get_risk_manager()

    def test_calculate_trailing_stop_after_tp1(self):
        """测试TP1后的移动止损"""
        current_price = Decimal('51000')
        original_stop_loss = Decimal('48000')
        direction = 1
        entry_price = Decimal('50000')
        tp1_price = Decimal('51250')

        # TP1后，止损应该移至开仓价（保本）
        new_stop = self.risk_manager.calculate_trailing_stop_adjustment(
            current_price, original_stop_loss, direction,
            tp_reached='TP1', entry_price=entry_price, tp1_price=tp1_price
        )

        # 保本止损价应该等于开仓价
        self.assertEqual(new_stop, entry_price)

    def test_calculate_trailing_stop_after_tp2(self):
        """测试TP2后的移动止损"""
        current_price = Decimal('52000')
        original_stop_loss = Decimal('48000')
        direction = 1
        entry_price = Decimal('50000')
        tp1_price = Decimal('51250')

        # TP2后，止损应该移至TP1价
        new_stop = self.risk_manager.calculate_trailing_stop_adjustment(
            current_price, original_stop_loss, direction,
            tp_reached='TP2', entry_price=entry_price, tp1_price=tp1_price
        )

        # 止损应该移至TP1价
        self.assertEqual(new_stop, tp1_price)


class TestRiskManagerRValue(unittest.TestCase):
    """测试R值计算"""

    def setUp(self):
        """测试前准备"""
        self.risk_manager = get_risk_manager()

    def test_calculate_r_value_long(self):
        """测试多头R值计算"""
        entry_price = Decimal('50000')
        stop_loss_price = Decimal('49000')
        direction = 1

        r_value = self.risk_manager.calculate_r_value(
            entry_price, stop_loss_price, direction
        )

        # R值 = |50000 - 49000| = 1000
        expected = Decimal('1000')
        self.assertEqual(r_value, expected)

    def test_calculate_r_value_short(self):
        """测试空头R值计算"""
        entry_price = Decimal('50000')
        stop_loss_price = Decimal('51000')
        direction = -1

        r_value = self.risk_manager.calculate_r_value(
            entry_price, stop_loss_price, direction
        )

        # R值 = |50000 - 51000| = 1000
        expected = Decimal('1000')
        self.assertEqual(r_value, expected)


class TestRiskManagerFloatLoss(unittest.TestCase):
    """测试浮动亏损检查"""

    def setUp(self):
        """测试前准备"""
        self.risk_manager = get_risk_manager()

    def test_check_float_loss_safe(self):
        """测试安全的浮动亏损"""
        float_loss = Decimal('-10')  # 亏损10U
        risk_amount = Decimal('10')  # 风险金额10U

        need_stop = self.risk_manager.check_float_loss(float_loss, risk_amount)

        # 亏损10U，未超过20U限制
        self.assertFalse(need_stop)

    def test_check_float_loss_exceeded(self):
        """测试超限的浮动亏损"""
        float_loss = Decimal('-25')  # 亏损25U
        risk_amount = Decimal('10')  # 风险金额10U

        need_stop = self.risk_manager.check_float_loss(float_loss, risk_amount)

        # 亏损25U，超过20U限制
        self.assertTrue(need_stop)


class TestRiskManagerIntegration(unittest.TestCase):
    """风险管理集成测试"""

    def setUp(self):
        """测试前准备"""
        self.risk_manager = get_risk_manager()

    def test_full_risk_management_workflow(self):
        """测试完整的风险管理流程"""
        # 模拟交易参数
        entry_price = Decimal('50000')
        direction = 1  # 多头
        atr14 = Decimal('500')
        signal_grade = 'A'
        account_equity = Decimal('10000')
        used_margin = Decimal('2000')

        # 1. 计算止损
        stop_loss, stop_distance = self.risk_manager.calculate_atr_based_stop_loss(
            entry_price, direction, atr14
        )

        # 2. 计算止盈
        tp_levels = self.risk_manager.calculate_take_profit_levels(
            entry_price, direction, atr14, signal_grade
        )

        # 3. 检查保证金
        margin_ratio, risk_level, need_intervention = self.risk_manager.check_margin_ratio(
            account_equity, used_margin
        )

        # 验证完整流程
        self.assertLess(stop_loss, entry_price)  # 多头止损应该低于开仓价
        self.assertEqual(len(tp_levels), 3)  # 应该有3个止盈位
        self.assertEqual(risk_level, 'SAFE')  # 保证金率安全

    def test_risk_manager_singleton(self):
        """测试风险管理器单例模式"""
        manager1 = get_risk_manager()
        manager2 = get_risk_manager()

        self.assertIs(manager1, manager2)


if __name__ == '__main__':
    unittest.main()
