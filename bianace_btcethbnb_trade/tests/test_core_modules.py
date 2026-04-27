#!/usr/bin/env python3
"""
核心模块单元测试

测试范围：
1. 信号检测模块
2. 仓位计算模块
3. 风险管理模块
4. 订单生成模块
5. 应急处理模块
"""

import unittest
from decimal import Decimal
from datetime import datetime
from core.signal import SignalDetector, get_signal_detector
from core.position_calculator import PositionCalculator, calculate_position
from core.risk_manager import RiskManager, calculate_stop_loss, calculate_take_profit_levels
from core.order_generator import OrderGenerator, generate_order_template
from core.emergency_handler import EmergencyHandler, check_extreme_market
from config.strategy_params import StrategyParams


class TestSignalDetector(unittest.TestCase):
    """测试信号检测模块"""
    
    def setUp(self):
        self.detector = get_signal_detector()
    
    def test_detect_signals(self):
        """测试信号检测"""
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        signals = self.detector.detect_signals(symbols)
        
        # 验证返回类型
        self.assertIsInstance(signals, list)
        
        # 验证信号格式（如果有信号）
        if signals:
            signal = signals[0]
            self.assertIn('币种', signal)
            self.assertIn('开仓方向', signal)
            self.assertIn('信号等级', signal)
            self.assertIn('开仓价', signal)
            self.assertIn('止损价', signal)


class TestPositionCalculator(unittest.TestCase):
    """测试仓位计算模块"""
    
    def test_calculate_position_long(self):
        """测试多头仓位计算"""
        position = calculate_position(
            symbol='BTCUSDT',
            entry_price=Decimal('95000'),
            stop_loss_price=Decimal('93000'),
            direction=1,
            signal_grade='A'
        )
        
        # 验证计算结果
        self.assertIn('actual_notional_value', position)
        self.assertIn('quantity', position)
        self.assertIn('margin', position)
        self.assertIn('leverage', position)
        
        # 验证保证金不超过上限
        self.assertLessEqual(position['margin'], Decimal('30'))
    
    def test_calculate_position_short(self):
        """测试空头仓位计算"""
        position = calculate_position(
            symbol='ETHUSDT',
            entry_price=Decimal('2200'),
            stop_loss_price=Decimal('2250'),
            direction=-1,
            signal_grade='B'
        )
        
        self.assertIn('actual_notional_value', position)
        self.assertGreater(position['actual_notional_value'], Decimal('0'))


class TestRiskManager(unittest.TestCase):
    """测试风险管理模块"""
    
    def test_calculate_stop_loss_long(self):
        """测试多头止损价计算"""
        stop_loss = calculate_stop_loss(
            entry_price=Decimal('95000'),
            direction=1,
            stop_loss_pct=Decimal('0.02')
        )
        
        # 止损价应该低于开仓价
        self.assertLess(stop_loss, Decimal('95000'))
        # 验证计算准确性
        expected = Decimal('95000') * (1 - Decimal('0.02'))
        self.assertEqual(stop_loss, expected)
    
    def test_calculate_stop_loss_short(self):
        """测试空头止损价计算"""
        stop_loss = calculate_stop_loss(
            entry_price=Decimal('2200'),
            direction=-1,
            stop_loss_pct=Decimal('0.02')
        )
        
        # 止损价应该高于开仓价
        self.assertGreater(stop_loss, Decimal('2200'))
    
    def test_calculate_take_profit_levels(self):
        """测试止盈水平计算"""
        r_value = Decimal('100')  # R值等同于ATR14
        tp_levels = calculate_take_profit_levels(
            entry_price=Decimal('1000'),
            direction=1,
            r_value=r_value,
            signal_grade='A'
        )
        
        # 验证返回 3 个止盈水平
        self.assertEqual(len(tp_levels), 3)
        
        # 验证 TP1 = 开仓价 + 2.5×ATR14（V6.13.1规范）
        tp1_expected = Decimal('1000') + r_value * Decimal('2.5')
        self.assertEqual(tp_levels[0]['price'], tp1_expected)
        
        # 验证 TP2 = 开仓价 + 4.0×ATR14（V6.13.1规范）
        tp2_expected = Decimal('1000') + r_value * Decimal('4.0')
        self.assertEqual(tp_levels[1]['price'], tp2_expected)
        
        # 验证 TP3 无固定价格
        self.assertIsNone(tp_levels[2]['price'])
    
    def test_check_margin_ratio_safe(self):
        """测试保证金率检查 - 安全状态"""
        from core.risk_manager import check_margin_ratio
        
        margin_ratio, risk_level, need_intervention = check_margin_ratio(
            account_equity=Decimal('500'),
            used_margin=Decimal('100')
        )
        
        # 保证金率 = 500/100 = 5.0，应该安全
        self.assertGreater(margin_ratio, Decimal('1.5'))
        self.assertEqual(risk_level, 'SAFE')
        self.assertFalse(need_intervention)
    
    def test_check_margin_ratio_warning(self):
        """测试保证金率检查 - 预警状态"""
        from core.risk_manager import RiskManager
        
        manager = RiskManager()
        margin_ratio, risk_level, need_intervention = manager.check_margin_ratio(
            account_equity=Decimal('140'),
            used_margin=Decimal('100')
        )
        
        # 保证金率 = 140/100 = 1.4，应该预警
        self.assertLessEqual(margin_ratio, Decimal('1.5'))
        self.assertEqual(risk_level, 'WARNING')
        self.assertTrue(need_intervention)
    
    def test_check_margin_usage(self):
        """测试保证金使用率检查"""
        from core.risk_manager import RiskManager
        
        manager = RiskManager()
        margin_usage, exceeded = manager.check_margin_usage(
            total_capital=Decimal('500'),
            used_margin=Decimal('350')
        )
        
        # 使用率 = 350/500 = 70%，超过60%预警线
        self.assertGreater(margin_usage, Decimal('0.6'))
        self.assertTrue(exceeded)
    
    def test_calculate_r_value(self):
        """测试R值计算"""
        from core.risk_manager import RiskManager
        
        manager = RiskManager()
        r_value = manager.calculate_r_value(
            entry_price=Decimal('95000'),
            stop_loss_price=Decimal('93000'),
            direction=1
        )
        
        # R值 = |95000 - 93000| = 2000
        self.assertEqual(r_value, Decimal('2000'))


class TestOrderGenerator(unittest.TestCase):
    """测试订单生成模块"""
    
    def test_generate_order_template(self):
        """测试订单模板生成"""
        position_data = {
            'notional_value': Decimal('120'),
            'risk_amount': Decimal('10'),
            'risk_ratio': Decimal('0.02')
        }
        
        template = generate_order_template(
            symbol='BTCUSDT',
            direction=1,
            entry_price=Decimal('95000'),
            stop_loss_price=Decimal('93000'),
            signal_grade='A',
            position_data=position_data
        )
        
        # 验证模板字段
        self.assertEqual(template['symbol'], 'BTCUSDT')
        self.assertEqual(template['direction'], 'LONG')
        self.assertIn('entry_price', template)
        self.assertIn('stop_loss_price', template)
        self.assertIn('take_profit_levels', template)
        self.assertIn('leverage', template)
    
    def test_format_price(self):
        """测试价格格式化"""
        generator = OrderGenerator()
        
        # 测试价格格式化（tick_size=0.1）
        price = Decimal('95123.456')
        formatted = generator._format_price(price, Decimal('0.1'))
        
        # 应该向下取整到 0.1
        self.assertEqual(formatted, Decimal('95123.4'))


class TestEmergencyHandler(unittest.TestCase):
    """测试应急处理模块"""
    
    def test_check_extreme_market_normal(self):
        """测试正常市场检测"""
        result = check_extreme_market(
            symbol='BTCUSDT',
            price_change_percent=Decimal('3.5')
        )
        
        # 涨跌幅 < 5%，不是极端行情
        self.assertFalse(result)
    
    def test_check_extreme_market_extreme(self):
        """测试极端市场检测"""
        result = check_extreme_market(
            symbol='BTCUSDT',
            price_change_percent=Decimal('5.5')
        )
        
        # 涨跌幅 > 5%，是极端行情
        self.assertTrue(result)
    
    def test_trading_halt(self):
        """测试停止交易逻辑"""
        handler = EmergencyHandler()
        
        # 初始状态应该允许交易
        allowed, reason = handler.is_trading_allowed()
        self.assertTrue(allowed)
        
        # 模拟触发单日亏损
        handler.check_daily_loss(Decimal('-35'))
        
        # 应该停止交易
        allowed, reason = handler.is_trading_allowed()
        self.assertFalse(allowed)
        self.assertIn('单日亏损', reason)


class TestIntegration(unittest.TestCase):
    """集成测试"""
    
    def test_full_workflow(self):
        """测试完整工作流程"""
        # 1. 信号检测
        detector = get_signal_detector()
        signals = detector.detect_signals(['BTCUSDT'])
        
        if signals:
            signal = signals[0]
            
            # 2. 仓位计算
            position = calculate_position(
                symbol=signal['币种'],
                entry_price=Decimal(str(signal['开仓价'])),
                stop_loss_price=Decimal(str(signal['止损价'])),
                direction=1 if signal['开仓方向'] == '多' else -1,
                signal_grade=signal['信号等级']
            )
            
            # 3. 订单生成
            template = generate_order_template(
                symbol=signal['币种'],
                direction=1 if signal['开仓方向'] == '多' else -1,
                entry_price=Decimal(str(signal['开仓价'])),
                stop_loss_price=Decimal(str(signal['止损价'])),
                signal_grade=signal['信号等级'],
                position_data=position
            )
            
            # 4. 验证订单参数完整性
            self.assertIn('entry_price', template)
            self.assertIn('stop_loss_price', template)
            self.assertIn('take_profit_levels', template)
            self.assertIn('quantity', template)
            self.assertIn('margin', template)


if __name__ == '__main__':
    unittest.main()
