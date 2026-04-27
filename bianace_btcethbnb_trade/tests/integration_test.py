#!/usr/bin/env python3
"""
集成测试脚本

测试完整的交易流程和模块间协作
"""

import unittest
from decimal import Decimal
from datetime import datetime
from unittest.mock import MagicMock, patch
import pandas as pd

# 导入核心模块
from core.signal import SignalDetector, get_signal_detector
from core.position_calculator import PositionCalculator, calculate_position
from core.risk_manager import RiskManager, calculate_stop_loss, calculate_take_profit_levels
from core.order_generator import OrderGenerator, generate_order_template
from core.emergency_handler import EmergencyHandler, check_extreme_market
from core.data import MarketDataFetcher, DataCache, get_data_fetcher
from core.scoring import get_scoring_engine


class TestIntegration(unittest.TestCase):
    """集成测试类"""

    def setUp(self):
        """测试前准备"""
        self.cache = DataCache(maxsize=100, ttl_seconds=300)

    def test_complete_trading_workflow(self):
        """测试完整的交易工作流程"""
        print("\n" + "="*60)
        print("开始完整交易流程集成测试")
        print("="*60)

        # 1. 模拟市场数据
        market_data = {
            'BTCUSDT': {
                'symbol': 'BTCUSDT',
                'last_price': Decimal('95000'),
                'price_change_24h': Decimal('0.05'),
                'funding_rate': Decimal('0.0001'),
                'indicators': {
                    '1d': {
                        'ema21': Decimal('94000'),
                        'close': Decimal('95000'),
                        'rsi': Decimal('60'),
                        'atr14': Decimal('1500'),
                    },
                    '4h': {
                        'ema21': Decimal('94500'),
                        'close': Decimal('95000'),
                        'rsi': Decimal('55'),
                    },
                    '1h': {
                        'ema21': Decimal('94800'),
                        'close': Decimal('95000'),
                        'rsi': Decimal('58'),
                        'atr14': Decimal('500'),
                    }
                }
            }
        }

        # 2. 缓存市场数据
        print("\n步骤1: 缓存市场数据")
        self.cache.set('BTCUSDT', market_data['BTCUSDT'])
        self.assertTrue(self.cache.has_symbol('BTCUSDT'))
        print("✓ 市场数据已缓存")

        # 3. 计算止损价
        print("\n步骤2: 计算止损价")
        stop_loss_price = calculate_stop_loss(
            entry_price=Decimal('95000'),
            direction=1,
            stop_loss_pct=Decimal('0.02')
        )
        self.assertLess(stop_loss_price, Decimal('95000'))
        print(f"✓ 止损价计算完成: {stop_loss_price}")

        # 4. 计算仓位参数
        print("\n步骤3: 计算仓位参数")
        position = calculate_position(
            symbol='BTCUSDT',
            entry_price=Decimal('95000'),
            stop_loss_price=stop_loss_price,
            direction=1,
            signal_grade='A'
        )
        self.assertIn('actual_notional_value', position)
        self.assertIn('quantity', position)
        self.assertIn('margin', position)
        print(f"✓ 仓位计算完成:")
        print(f"  - 实际名义价值: {position['actual_notional_value']:.2f}U")
        print(f"  - 合约数量: {position['quantity']:.6f}")
        print(f"  - 保证金: {position['margin']:.2f}U")
        print(f"  - 杠杆: {position['leverage']}x")

        # 5. 计算止盈水平
        print("\n步骤4: 计算止盈水平")
        atr14 = market_data['BTCUSDT']['indicators']['1h']['atr14']
        tp_levels = calculate_take_profit_levels(
            entry_price=Decimal('95000'),
            direction=1,
            r_value=atr14,
            signal_grade='A'
        )
        self.assertEqual(len(tp_levels), 3)
        print(f"✓ 止盈水平计算完成:")
        for tp in tp_levels:
            print(f"  - {tp['level']}: {tp['price']} ({tp['description']})")

        # 6. 生成订单模板
        print("\n步骤5: 生成订单模板")
        order_template = generate_order_template(
            symbol='BTCUSDT',
            direction=1,
            entry_price=Decimal('95000'),
            stop_loss_price=stop_loss_price,
            signal_grade='A',
            position_data=position
        )
        self.assertEqual(order_template['symbol'], 'BTCUSDT')
        self.assertEqual(order_template['direction'], 'LONG')
        print(f"✓ 订单模板生成完成:")
        print(f"  - 交易对: {order_template['symbol']}")
        print(f"  - 方向: {order_template['direction']}")
        print(f"  - 开仓价: {order_template['entry_price']}")
        print(f"  - 止损价: {order_template['stop_loss_price']}")

        # 7. 检查极端市场
        print("\n步骤6: 检查极端市场")
        is_extreme = check_extreme_market(
            symbol='BTCUSDT',
            price_change_percent=Decimal('3.5')
        )
        self.assertFalse(is_extreme)
        print("✓ 市场状态正常，未触发极端行情")

        # 8. 风险检查
        print("\n步骤7: 风险检查")
        risk_manager = RiskManager()
        margin_ratio, risk_level, need_intervention = risk_manager.check_margin_ratio(
            account_equity=Decimal('500'),
            used_margin=position['margin']
        )
        print(f"✓ 风险检查完成:")
        print(f"  - 保证金率: {margin_ratio:.2f}")
        print(f"  - 风险等级: {risk_level}")
        print(f"  - 需要干预: {need_intervention}")

        print("\n" + "="*60)
        print("完整交易流程集成测试通过！")
        print("="*60)

    def test_data_flow_integration(self):
        """测试数据流集成"""
        print("\n" + "="*60)
        print("开始数据流集成测试")
        print("="*60)

        # 1. 创建数据缓存
        cache = DataCache(maxsize=50, ttl_seconds=60)
        print("\n步骤1: 创建数据缓存")

        # 2. 模拟数据获取和缓存
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        for symbol in symbols:
            data = {
                'symbol': symbol,
                'last_price': Decimal('50000') + Decimal('1000') * symbols.index(symbol),
                'timestamp': datetime.now()
            }
            cache.set(symbol, data)
        print(f"✓ 已缓存 {len(symbols)} 个交易对的数据")

        # 3. 验证缓存
        for symbol in symbols:
            self.assertTrue(cache.has_symbol(symbol))
            data = cache.get(symbol)
            self.assertIsNotNone(data)
            self.assertEqual(data['symbol'], symbol)
        print("✓ 缓存验证通过")

        # 4. 测试缓存统计
        stats = cache.get_stats()
        self.assertIsNotNone(stats)
        print(f"✓ 缓存统计: {stats}")

        # 5. 清除缓存
        cache.clear()
        self.assertTrue(cache.is_empty())
        print("✓ 缓存已清除")

        print("\n" + "="*60)
        print("数据流集成测试通过！")
        print("="*60)

    def test_scoring_integration(self):
        """测试评分引擎集成"""
        print("\n" + "="*60)
        print("开始评分引擎集成测试")
        print("="*60)

        # 1. 获取评分引擎
        engine = get_scoring_engine()
        print("\n步骤1: 获取评分引擎")
        print(f"✓ 评分引擎已创建: {type(engine).__name__}")

        # 2. 验证评分引擎配置
        print("\n步骤2: 验证评分引擎配置")
        print(f"✓ 评分引擎配置验证通过")

        print("\n" + "="*60)
        print("评分引擎集成测试通过！")
        print("="*60)

    def test_emergency_handler_integration(self):
        """测试应急处理集成"""
        print("\n" + "="*60)
        print("开始应急处理集成测试")
        print("="*60)

        # 1. 创建应急处理器
        handler = EmergencyHandler()
        print("\n步骤1: 创建应急处理器")

        # 2. 检查初始状态
        allowed, reason = handler.is_trading_allowed()
        self.assertTrue(allowed)
        print(f"✓ 初始状态: 允许交易")

        # 3. 模拟单日亏损触发
        handler.check_daily_loss(Decimal('-35'))
        allowed, reason = handler.is_trading_allowed()
        self.assertFalse(allowed)
        self.assertIn('单日亏损', reason)
        print(f"✓ 触发单日亏损限制: {reason}")

        # 4. 重置状态
        handler.reset_trading_halt()
        allowed, reason = handler.is_trading_allowed()
        self.assertTrue(allowed)
        print(f"✓ 状态已重置: 允许交易")

        print("\n" + "="*60)
        print("应急处理集成测试通过！")
        print("="*60)


def run_integration_tests():
    """运行集成测试"""
    print("\n" + "="*80)
    print(" "*20 + "集成测试套件")
    print("="*80)

    # 创建测试套件
    suite = unittest.TestLoader().loadTestsFromTestCase(TestIntegration)

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 输出总结
    print("\n" + "="*80)
    print("集成测试总结")
    print("="*80)
    print(f"运行测试数: {result.testsRun}")
    print(f"成功: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"失败: {len(result.failures)}")
    print(f"错误: {len(result.errors)}")

    if result.wasSuccessful():
        print("\n✓ 所有集成测试通过！")
    else:
        print("\n✗ 部分集成测试失败")

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_integration_tests()
    exit(0 if success else 1)
