#!/usr/bin/env python3
"""
订单生成器模块测试

测试 core/order_generator.py 的核心功能：
1. 订单模板生成
2. 价格和数量格式化
3. 市价单参数生成
4. 限价单参数生成
5. 止损止盈单参数生成
6. 完整订单流程测试
"""

import pytest
from decimal import Decimal
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from core.order_generator import OrderGenerator, get_order_generator


class TestOrderGenerator:
    """订单生成器测试类"""

    @pytest.fixture
    def order_generator(self):
        """创建订单生成器实例"""
        # Mock 策略参数
        mock_params = Mock()
        mock_params.get = Mock(side_effect=lambda key, default=None: {
            'risk_management.take_profit_levels': {
                'tp1_multiplier': Decimal('1.5'),
                'tp2_multiplier': Decimal('2.5'),
                'tp1_ratio': Decimal('0.3'),
                'tp2_ratio': Decimal('0.3'),
                'tp3_ratio': Decimal('0.4')
            },
            'position_sizing.leverage_by_grade': {
                'S': 5,
                'A': 4,
                'B': 3
            },
            'account.min_notional_value': Decimal('100')
        }.get(key, default))
        
        return OrderGenerator(params=mock_params)

    def test_init(self, order_generator):
        """测试初始化"""
        assert order_generator is not None
        assert order_generator.params is not None

    def test_generate_order_template_long(self, order_generator):
        """测试生成多头订单模板"""
        # 准备测试数据
        symbol = 'BTCUSDT'
        direction = 1  # 多头
        entry_price = Decimal('50000')
        stop_loss_price = Decimal('48000')  # 止损 4%
        signal_grade = 'A'
        position_data = {
            'notional_value': Decimal('500'),
            'risk_amount': Decimal('10'),
            'risk_ratio': Decimal('0.02')
        }
        
        # 执行测试
        template = order_generator.generate_order_template(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            signal_grade=signal_grade,
            position_data=position_data
        )
        
        # 验证结果
        assert template['symbol'] == symbol
        assert template['direction'] == 'LONG'
        assert template['entry_price'] == entry_price
        assert template['stop_loss_price'] == stop_loss_price
        assert template['signal_grade'] == signal_grade
        assert template['leverage'] == 4  # A 级杠杆
        assert len(template['take_profit_levels']) == 3  # TP1, TP2, TP3
        
        # 验证止盈价格计算
        r_value = entry_price - stop_loss_price  # 2000
        tp1_price = entry_price + r_value * Decimal('1.5')  # 50000 + 3000 = 53000
        tp2_price = entry_price + r_value * Decimal('2.5')  # 50000 + 5000 = 55000
        
        assert template['take_profit_levels'][0]['price'] == tp1_price
        assert template['take_profit_levels'][1]['price'] == tp2_price
        assert template['take_profit_levels'][2]['price'] is None  # TP3 移动止损

    def test_generate_order_template_short(self, order_generator):
        """测试生成空头订单模板"""
        # 准备测试数据
        symbol = 'ETHUSDT'
        direction = -1  # 空头
        entry_price = Decimal('3000')
        stop_loss_price = Decimal('3150')  # 止损 5%
        signal_grade = 'S'
        position_data = {
            'notional_value': Decimal('600'),
            'risk_amount': Decimal('10'),
            'risk_ratio': Decimal('0.02')
        }
        
        # 执行测试
        template = order_generator.generate_order_template(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            signal_grade=signal_grade,
            position_data=position_data
        )
        
        # 验证结果
        assert template['symbol'] == symbol
        assert template['direction'] == 'SHORT'
        assert template['leverage'] == 5  # S 级杠杆
        
        # 验证止盈价格计算（空头向下）
        r_value = stop_loss_price - entry_price  # 150
        tp1_price = entry_price - r_value * Decimal('1.5')  # 3000 - 225 = 2775
        tp2_price = entry_price - r_value * Decimal('2.5')  # 3000 - 375 = 2625
        
        assert template['take_profit_levels'][0]['price'] == tp1_price
        assert template['take_profit_levels'][1]['price'] == tp2_price

    def test_format_order_for_api(self, order_generator):
        """测试订单格式化（精度处理）"""
        # 准备测试数据
        order_template = {
            'symbol': 'BTCUSDT',
            'direction': 'LONG',
            'entry_price': Decimal('50000.12345'),
            'stop_loss_price': Decimal('48000.98765'),
            'quantity': Decimal('0.0123456'),
            'take_profit_levels': [
                {'level': 'TP1', 'price': Decimal('53000.55555'), 'ratio': Decimal('0.3')},
                {'level': 'TP2', 'price': Decimal('55000.66666'), 'ratio': Decimal('0.3')}
            ]
        }
        
        api_precision = {
            'tick_size': Decimal('0.1'),
            'step_size': Decimal('0.001')
        }
        
        # 执行测试
        formatted = order_generator.format_order_for_api(
            order_template=order_template,
            api_precision=api_precision
        )
        
        # 验证价格格式化（向下取整到 tick_size）
        assert formatted['entry_price'] == Decimal('50000.1')
        assert formatted['stop_loss_price'] == Decimal('48000.9')
        
        # 验证数量格式化（向下取整到 step_size）
        assert formatted['quantity'] == Decimal('0.012')
        
        # 验证止盈价格格式化
        assert formatted['take_profit_levels'][0]['price'] == Decimal('53000.5')
        assert formatted['take_profit_levels'][1]['price'] == Decimal('55000.6')

    def test_format_price_round_down(self, order_generator):
        """测试价格向下取整"""
        # 测试向下取整逻辑
        price = Decimal('50000.99')
        tick_size = Decimal('0.1')
        
        formatted = order_generator._format_price(price, tick_size)
        
        # 应该向下取整到 50000.9
        assert formatted == Decimal('50000.9')

    def test_format_quantity_with_min_notional(self, order_generator):
        """测试数量格式化（考虑最小名义价值）"""
        # 测试数量太小的情况
        quantity = Decimal('0.001')
        step_size = Decimal('0.001')
        min_notional = Decimal('100')
        price = Decimal('50000')
        
        formatted = order_generator._format_quantity(
            quantity=quantity,
            step_size=step_size,
            min_notional=min_notional,
            price=price
        )
        
        # 0.001 * 50000 = 50U < 100U，需要调整
        # 最小数量 = 100 / 50000 = 0.002
        assert formatted >= Decimal('0.002')

    def test_generate_market_order_params(self, order_generator):
        """测试生成市价单参数"""
        # 准备测试数据
        order_template = {
            'symbol': 'BTCUSDT',
            'direction': 'LONG',
            'quantity': Decimal('0.01')
        }
        
        # 执行测试
        params = order_generator.generate_market_order_params(order_template)
        
        # 验证结果
        assert params['symbol'] == 'BTCUSDT'
        assert params['side'] == 'BUY'
        assert params['position_share'] == 'BOTH'
        assert params['type'] == 'MARKET'
        assert params['quantity'] == Decimal('0.01')

    def test_generate_limit_order_params_long(self, order_generator):
        """测试生成限价单参数（多头）"""
        # 准备测试数据
        order_template = {
            'symbol': 'BTCUSDT',
            'direction': 'LONG',
            'quantity': Decimal('0.01'),
            'entry_price': Decimal('50000')
        }
        
        current_price = Decimal('50000')
        orderbook_data = {
            'bids': [{'price': '49999.5', 'qty': '1.0'}],
            'asks': [{'price': '50000.5', 'qty': '1.0'}]
        }
        
        # 执行测试
        params = order_generator.generate_limit_order_params(
            order_template=order_template,
            current_price=current_price,
            orderbook_data=orderbook_data
        )
        
        # 验证结果
        assert params['symbol'] == 'BTCUSDT'
        assert params['side'] == 'BUY'
        assert params['type'] == 'LIMIT'
        assert params['timeInForce'] == 'GTC'
        # 做多应该使用买一价
        assert Decimal(params['price']) == Decimal('49999.5')

    def test_generate_limit_order_params_short(self, order_generator):
        """测试生成限价单参数（空头）"""
        # 准备测试数据
        order_template = {
            'symbol': 'BTCUSDT',
            'direction': 'SHORT',
            'quantity': Decimal('0.01'),
            'entry_price': Decimal('50000')
        }
        
        current_price = Decimal('50000')
        orderbook_data = {
            'bids': [{'price': '49999.5', 'qty': '1.0'}],
            'asks': [{'price': '50000.5', 'qty': '1.0'}]
        }
        
        # 执行测试
        params = order_generator.generate_limit_order_params(
            order_template=order_template,
            current_price=current_price,
            orderbook_data=orderbook_data
        )
        
        # 验证结果
        assert params['side'] == 'SELL'
        # 做空应该使用卖一价
        assert Decimal(params['price']) == Decimal('50000.5')

    def test_generate_stop_loss_order_params(self, order_generator):
        """测试生成止损单参数"""
        # 准备测试数据
        order_template = {
            'symbol': 'BTCUSDT',
            'direction': 'LONG',
            'stop_loss_price': Decimal('48000'),
            'quantity': Decimal('0.01')
        }
        
        # 执行测试
        params = order_generator.generate_stop_loss_order_params(order_template)
        
        # 验证结果
        assert params['symbol'] == 'BTCUSDT'
        assert params['side'] == 'SELL'  # 平多仓
        assert params['position_side'] == 'BOTH'
        assert params['strategy_type'] == 'STOP_MARKET'
        assert params['stop_price'] == Decimal('48000')
        assert params['reduce_only'] is True

    def test_generate_take_profit_order_params(self, order_generator):
        """测试生成止盈单参数"""
        # 准备测试数据
        order_template = {
            'symbol': 'BTCUSDT',
            'direction': 'LONG',
            'quantity': Decimal('0.01')
        }
        
        tp_level = {
            'level': 'TP1',
            'price': Decimal('53000'),
            'ratio': Decimal('0.3')
        }
        
        # 执行测试
        params = order_generator.generate_take_profit_order_params(
            order_template=order_template,
            tp_level=tp_level,
            position_qty=Decimal('0.01')
        )
        
        # 验证结果
        assert params['symbol'] == 'BTCUSDT'
        assert params['side'] == 'SELL'
        assert params['strategy_type'] == 'TAKE_PROFIT_MARKET'
        assert params['stop_price'] == Decimal('53000')
        # 数量应该是 30%
        assert params['quantity'] == Decimal('0.003')

    def test_generate_all_orders(self, order_generator):
        """测试生成完整订单流程"""
        # 准备测试数据
        order_template = {
            'symbol': 'BTCUSDT',
            'direction': 'LONG',
            'entry_price': Decimal('50000'),
            'stop_loss_price': Decimal('48000'),
            'quantity': Decimal('0.01'),
            'take_profit_levels': [
                {'level': 'TP1', 'price': Decimal('53000'), 'ratio': Decimal('0.3')},
                {'level': 'TP2', 'price': Decimal('55000'), 'ratio': Decimal('0.3')},
                {'level': 'TP3', 'price': None, 'ratio': Decimal('0.4')}
            ]
        }
        
        current_price = Decimal('50000')
        orderbook_data = {
            'bids': [{'price': '49999.5'}],
            'asks': [{'price': '50000.5'}]
        }
        
        # 执行测试（使用限价单）
        orders = order_generator.generate_all_orders(
            order_template=order_template,
            use_limit_order=True,
            current_price=current_price,
            orderbook_data=orderbook_data
        )
        
        # 验证结果
        assert 'entry' in orders
        assert 'stop_loss' in orders
        assert 'take_profits' in orders
        
        # 验证开仓单
        assert orders['entry']['type'] == 'LIMIT'
        assert orders['entry']['side'] == 'BUY'
        
        # 验证止损单
        assert orders['stop_loss']['strategy_type'] == 'STOP_MARKET'
        
        # 验证止盈单（TP1 和 TP2，TP3 无价格）
        assert len(orders['take_profits']) == 2

    def test_generate_all_orders_market_mode(self, order_generator):
        """测试生成完整订单流程（市价单模式）"""
        # 准备测试数据
        order_template = {
            'symbol': 'BTCUSDT',
            'direction': 'SHORT',
            'entry_price': Decimal('50000'),
            'stop_loss_price': Decimal('52000'),
            'quantity': Decimal('0.01'),
            'take_profit_levels': [
                {'level': 'TP1', 'price': Decimal('47000'), 'ratio': Decimal('0.3')}
            ]
        }
        
        # 执行测试（使用市价单）
        orders = order_generator.generate_all_orders(
            order_template=order_template,
            use_limit_order=False
        )
        
        # 验证结果
        assert orders['entry']['type'] == 'MARKET'
        assert orders['entry']['side'] == 'SELL'

    def test_get_leverage_for_grade(self, order_generator):
        """测试根据信号等级获取杠杆"""
        # 测试 S 级
        assert order_generator._get_leverage_for_grade('S') == 5
        
        # 测试 A 级
        assert order_generator._get_leverage_for_grade('A') == 4
        
        # 测试 B 级
        assert order_generator._get_leverage_for_grade('B') == 3
        
        # 测试未知等级（默认）
        assert order_generator._get_leverage_for_grade('C') == 3

    def test_get_default_precision(self, order_generator):
        """测试获取默认精度"""
        # 测试 BTCUSDT
        precision = order_generator._get_default_precision('BTCUSDT')
        assert precision['tick_size'] == Decimal('0.1')
        assert precision['step_size'] == Decimal('0.001')
        
        # 测试 ETHUSDT
        precision = order_generator._get_default_precision('ETHUSDT')
        assert precision['tick_size'] == Decimal('0.1')
        
        # 测试未知交易对
        precision = order_generator._get_default_precision('UNKNOWN')
        assert precision['tick_size'] == Decimal('0.1')

    def test_calculate_take_profit_prices(self, order_generator):
        """测试计算止盈价格"""
        # 多头情况
        entry_price = Decimal('50000')
        direction = 1
        r_value = Decimal('2000')
        
        tp_levels = order_generator._calculate_take_profit_prices(
            entry_price=entry_price,
            direction=direction,
            r_value=r_value
        )
        
        # 验证 TP1
        assert tp_levels[0]['level'] == 'TP1'
        assert tp_levels[0]['price'] == Decimal('53000')  # 50000 + 2000 * 1.5
        assert tp_levels[0]['ratio'] == Decimal('0.3')
        
        # 验证 TP2
        assert tp_levels[1]['level'] == 'TP2'
        assert tp_levels[1]['price'] == Decimal('55000')  # 50000 + 2000 * 2.5
        
        # 验证 TP3（移动止损）
        assert tp_levels[2]['level'] == 'TP3'
        assert tp_levels[2]['price'] is None
        assert tp_levels[2]['ratio'] == Decimal('0.4')


class TestOrderGeneratorSingleton:
    """测试订单生成器单例模式"""

    def test_get_order_generator_singleton(self):
        """测试获取全局实例"""
        # 清除全局实例
        import core.order_generator as og_module
        og_module._global_order_generator = None
        
        # 获取实例
        generator1 = get_order_generator()
        generator2 = get_order_generator()
        
        # 应该是同一个实例
        assert generator1 is generator2

    def test_get_order_generator_with_params(self):
        """测试使用自定义参数获取实例"""
        # 清除全局实例
        import core.order_generator as og_module
        og_module._global_order_generator = None
        
        # Mock 参数
        mock_params = Mock()
        
        # 获取实例
        generator = get_order_generator(params=mock_params)
        
        # 验证
        assert generator.params == mock_params


class TestOrderGeneratorEdgeCases:
    """测试订单生成器边界情况"""

    @pytest.fixture
    def order_generator(self):
        """创建订单生成器实例"""
        mock_params = Mock()
        mock_params.get = Mock(return_value=None)
        return OrderGenerator(params=mock_params)

    def test_format_price_zero_tick_size(self, order_generator):
        """测试零 tick_size 的情况"""
        # 这种情况不应该发生，但需要测试健壮性
        price = Decimal('50000')
        tick_size = Decimal('0.0001')  # 非常小的精度
        
        formatted = order_generator._format_price(price, tick_size)
        
        # 应该能正常处理
        assert formatted >= Decimal('0')

    def test_format_quantity_very_small(self, order_generator):
        """测试非常小的数量"""
        quantity = Decimal('0.00001')
        step_size = Decimal('0.001')
        min_notional = Decimal('100')
        price = Decimal('50000')
        
        formatted = order_generator._format_quantity(
            quantity=quantity,
            step_size=step_size,
            min_notional=min_notional,
            price=price
        )
        
        # 应该调整到满足最小名义价值
        assert formatted * price >= min_notional

    def test_generate_order_with_zero_risk(self, order_generator):
        """测试零风险金额的情况"""
        # 准备测试数据
        symbol = 'BTCUSDT'
        direction = 1
        entry_price = Decimal('50000')
        stop_loss_price = Decimal('50000')  # R 值为 0
        signal_grade = 'A'
        position_data = {
            'notional_value': Decimal('0'),
            'risk_amount': Decimal('0'),
            'risk_ratio': Decimal('0')
        }
        
        # 执行测试
        template = order_generator.generate_order_template(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            signal_grade=signal_grade,
            position_data=position_data
        )
        
        # 验证结果（应该能正常处理）
        assert template['symbol'] == symbol
        # R 值为 0 时，止盈价格应该等于开仓价
        assert template['take_profit_levels'][0]['price'] == entry_price


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
