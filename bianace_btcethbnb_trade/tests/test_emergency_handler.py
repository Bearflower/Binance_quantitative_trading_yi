#!/usr/bin/env python3
"""
应急处理模块测试

测试 core/emergency_handler.py 的核心功能：
1. 极端行情检测
2. 单日亏损检查
3. 连续亏损检查
4. 总资金回撤检查
5. 交易停止/恢复机制
6. 应急报告生成
"""

import pytest
from decimal import Decimal
from unittest.mock import Mock, patch
from datetime import datetime, timedelta

from core.emergency_handler import EmergencyHandler, get_emergency_handler


class TestEmergencyHandler:
    """应急处理器测试类"""

    @pytest.fixture
    def emergency_handler(self):
        """创建应急处理器实例"""
        # Mock 策略参数
        mock_params = Mock()
        mock_params.get = Mock(side_effect=lambda key, default=None: {
            'emergency.extreme_market_threshold': Decimal('5.0'),
            'emergency.max_daily_loss': Decimal('30'),
            'emergency.max_consecutive_losses': 3,
            'emergency.max_total_drawdown': Decimal('0.1')
        }.get(key, default))
        
        return EmergencyHandler(params=mock_params)

    def test_init(self, emergency_handler):
        """测试初始化"""
        assert emergency_handler is not None
        assert emergency_handler.params is not None
        assert emergency_handler.recent_trades == []
        assert emergency_handler.trading_halt_until is None

    def test_check_extreme_market_normal(self, emergency_handler):
        """测试正常行情（非极端行情）"""
        # 准备测试数据
        symbol = 'BTCUSDT'
        price_change_percent = Decimal('3.5')  # 3.5% 涨幅
        
        # 执行测试
        is_extreme = emergency_handler.check_extreme_market(symbol, price_change_percent)
        
        # 验证结果（未超过 5% 阈值）
        assert is_extreme is False

    def test_check_extreme_market_extreme_up(self, emergency_handler):
        """测试极端上涨行情"""
        # 准备测试数据
        symbol = 'BTCUSDT'
        price_change_percent = Decimal('6.5')  # 6.5% 涨幅
        
        # 执行测试
        is_extreme = emergency_handler.check_extreme_market(symbol, price_change_percent)
        
        # 验证结果（超过 5% 阈值）
        assert is_extreme is True

    def test_check_extreme_market_extreme_down(self, emergency_handler):
        """测试极端下跌行情"""
        # 准备测试数据
        symbol = 'BTCUSDT'
        price_change_percent = Decimal('-6.0')  # -6.0% 跌幅
        
        # 执行测试
        is_extreme = emergency_handler.check_extreme_market(symbol, price_change_percent)
        
        # 验证结果（超过 5% 阈值）
        assert is_extreme is True

    def test_check_extreme_market_threshold(self, emergency_handler):
        """测试极端行情阈值边界"""
        # 准备测试数据
        symbol = 'BTCUSDT'
        
        # 刚好等于阈值（5.0%）
        is_extreme = emergency_handler.check_extreme_market(symbol, Decimal('5.0'))
        assert is_extreme is False
        
        # 稍微超过阈值（5.1%）
        is_extreme = emergency_handler.check_extreme_market(symbol, Decimal('5.1'))
        assert is_extreme is True

    def test_check_daily_loss_normal(self, emergency_handler):
        """测试正常单日亏损"""
        # 准备测试数据
        daily_pnl = Decimal('-20')  # 亏损 20U
        
        # 执行测试
        should_halt = emergency_handler.check_daily_loss(daily_pnl)
        
        # 验证结果（未超过 30U 阈值）
        assert should_halt is False
        assert emergency_handler.trading_halt_until is None

    def test_check_daily_loss_exceeded(self, emergency_handler):
        """测试单日亏损超限"""
        # 准备测试数据
        daily_pnl = Decimal('-35')  # 亏损 35U
        
        # 执行测试
        should_halt = emergency_handler.check_daily_loss(daily_pnl)
        
        # 验证结果（超过 30U 阈值）
        assert should_halt is True
        assert emergency_handler.trading_halt_until is not None
        assert emergency_handler.trading_halt_reason == "单日亏损超限 (-35.00U)"

    def test_check_daily_loss_threshold(self, emergency_handler):
        """测试单日亏损阈值边界"""
        # 刚好等于阈值（-30U）
        should_halt = emergency_handler.check_daily_loss(Decimal('-30'))
        assert should_halt is False
        
        # 稍微超过阈值（-30.01U）
        should_halt = emergency_handler.check_daily_loss(Decimal('-30.01'))
        assert should_halt is True

    def test_check_consecutive_losses_normal(self, emergency_handler):
        """测试正常连续亏损"""
        # 准备测试数据
        trades = [
            {'pnl': Decimal('-10')},
            {'pnl': Decimal('5')},  # 盈利，重置计数
            {'pnl': Decimal('-8')}
        ]
        
        # 执行测试
        should_halt = emergency_handler.check_consecutive_losses(trades)
        
        # 验证结果（只有 1 笔连续亏损）
        assert should_halt is False

    def test_check_consecutive_losses_exceeded(self, emergency_handler):
        """测试连续亏损超限"""
        # 准备测试数据
        trades = [
            {'pnl': Decimal('-10')},
            {'pnl': Decimal('-8')},
            {'pnl': Decimal('-12')}  # 第 3 笔连续亏损
        ]
        
        # 执行测试
        should_halt = emergency_handler.check_consecutive_losses(trades)
        
        # 验证结果（3 笔连续亏损）
        assert should_halt is True
        assert emergency_handler.trading_halt_until is not None
        assert "连续亏损" in emergency_handler.trading_halt_reason

    def test_check_consecutive_losses_with_profit(self, emergency_handler):
        """测试连续亏损中有盈利"""
        # 准备测试数据
        trades = [
            {'pnl': Decimal('-10')},
            {'pnl': Decimal('-8')},
            {'pnl': Decimal('5')},  # 盈利，重置计数
            {'pnl': Decimal('-12')},
            {'pnl': Decimal('-10')}
        ]
        
        # 执行测试
        should_halt = emergency_handler.check_consecutive_losses(trades)
        
        # 验证结果（只有 2 笔连续亏损）
        assert should_halt is False

    def test_check_total_drawdown_normal(self, emergency_handler):
        """测试正常资金回撤"""
        # 准备测试数据
        total_capital = Decimal('500')
        current_equity = Decimal('480')  # 回撤 4%
        
        # 执行测试
        should_halt = emergency_handler.check_total_drawdown(total_capital, current_equity)
        
        # 验证结果（未超过 10% 阈值）
        assert should_halt is False

    def test_check_total_drawdown_exceeded(self, emergency_handler):
        """测试资金回撤超限"""
        # 准备测试数据
        total_capital = Decimal('500')
        current_equity = Decimal('420')  # 回撤 16%
        
        # 执行测试
        should_halt = emergency_handler.check_total_drawdown(total_capital, current_equity)
        
        # 验证结果（超过 10% 阈值）
        assert should_halt is True
        assert emergency_handler.trading_halt_until is None  # 无限期停止

    def test_check_total_drawdown_threshold(self, emergency_handler):
        """测试资金回撤阈值边界"""
        total_capital = Decimal('500')
        
        # 刚好等于阈值（10%）
        should_halt = emergency_handler.check_total_drawdown(
            total_capital, Decimal('450')
        )
        assert should_halt is False
        
        # 稍微超过阈值（10.1%）
        should_halt = emergency_handler.check_total_drawdown(
            total_capital, Decimal('449')
        )
        assert should_halt is True

    def test_is_trading_allowed_normal(self, emergency_handler):
        """测试正常交易状态"""
        # 执行测试
        allowed, reason = emergency_handler.is_trading_allowed()
        
        # 验证结果
        assert allowed is True
        assert reason == "允许交易"

    def test_is_trading_allowed_halted(self, emergency_handler):
        """测试停止交易状态"""
        # 设置停止交易
        emergency_handler.trading_halt_until = datetime.now() + timedelta(hours=24)
        emergency_handler.trading_halt_reason = "单日亏损超限"
        
        # 执行测试
        allowed, reason = emergency_handler.is_trading_allowed()
        
        # 验证结果
        assert allowed is False
        assert "停止交易中" in reason

    def test_is_trading_allowed_expired(self, emergency_handler):
        """测试停止交易时间已过"""
        # 设置停止交易（已过期）
        emergency_handler.trading_halt_until = datetime.now() - timedelta(hours=1)
        emergency_handler.trading_halt_reason = "单日亏损超限"
        
        # 执行测试
        allowed, reason = emergency_handler.is_trading_allowed()
        
        # 验证结果（应该恢复交易）
        assert allowed is True
        assert reason == "允许交易"
        assert emergency_handler.trading_halt_until is None

    def test_reset_trading_halt(self, emergency_handler):
        """测试手动恢复交易"""
        # 设置停止交易
        emergency_handler.trading_halt_until = datetime.now() + timedelta(hours=24)
        emergency_handler.trading_halt_reason = "单日亏损超限"
        
        # 手动恢复
        emergency_handler.reset_trading_halt()
        
        # 验证结果
        assert emergency_handler.trading_halt_until is None
        assert emergency_handler.trading_halt_reason == ""

    def test_get_emergency_status_normal(self, emergency_handler):
        """测试获取应急状态（正常）"""
        # 执行测试
        status = emergency_handler.get_emergency_status()
        
        # 验证结果
        assert status['trading_allowed'] is True
        assert status['halt_reason'] == '允许交易'
        assert status['halt_until'] is None
        assert status['recent_trades_count'] == 0
        assert status['consecutive_losses'] == 0
        assert status['daily_pnl'] == Decimal('0')

    def test_get_emergency_status_with_trades(self, emergency_handler):
        """测试获取应急状态（有交易记录）"""
        # 添加交易记录（满足生成 alerts 的条件）
        emergency_handler.recent_trades = [
            {'symbol': 'BTCUSDT', 'pnl': Decimal('-10'), 'close_time': datetime.now().isoformat()},
            {'symbol': 'ETHUSDT', 'pnl': Decimal('-8'), 'close_time': datetime.now().isoformat()},
            {'symbol': 'BNBUSDT', 'pnl': Decimal('-5'), 'close_time': datetime.now().isoformat()}
        ]
        
        # 执行测试
        status = emergency_handler.get_emergency_status()
        
        # 验证结果
        assert status['recent_trades_count'] == 3
        assert status['consecutive_losses'] == 3  # 全部亏损
        assert len(status['alerts']) > 0  # 应该有警报（连续亏损>=2）

    def test_add_trade_record(self, emergency_handler):
        """测试添加交易记录"""
        # 准备测试数据
        trade = {
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'pnl': Decimal('10'),
            'close_time': datetime.now().isoformat()
        }
        
        # 执行测试
        emergency_handler.add_trade_record(trade)
        
        # 验证结果
        assert len(emergency_handler.recent_trades) == 1
        assert emergency_handler.recent_trades[0] == trade

    def test_add_trade_record_limit(self, emergency_handler):
        """测试交易记录数量限制"""
        # 添加 150 条记录
        for i in range(150):
            trade = {
                'symbol': 'BTCUSDT',
                'side': 'BUY',
                'pnl': Decimal('1'),
                'close_time': datetime.now().isoformat()
            }
            emergency_handler.add_trade_record(trade)
        
        # 验证结果（应该只保留最近 100 条）
        assert len(emergency_handler.recent_trades) == 100

    def test_handle_emergency_close(self, emergency_handler):
        """测试紧急平仓"""
        # 准备测试数据
        symbol = 'BTCUSDT'
        reason = "极端行情"
        current_price = Decimal('50000')
        
        # 执行测试
        close_order = emergency_handler.handle_emergency_close(symbol, reason, current_price)
        
        # 验证结果
        assert close_order['symbol'] == symbol
        assert close_order['action'] == 'EMERGENCY_CLOSE'
        assert close_order['reason'] == reason
        assert close_order['current_price'] == current_price
        assert close_order['priority'] == 'HIGH'

    def test_generate_emergency_report(self, emergency_handler):
        """测试生成应急报告"""
        # 添加一些交易记录
        emergency_handler.recent_trades = [
            {'symbol': 'BTCUSDT', 'pnl': Decimal('-10'), 'close_time': datetime.now().isoformat()},
            {'symbol': 'ETHUSDT', 'pnl': Decimal('5'), 'close_time': datetime.now().isoformat()}
        ]
        
        # 执行测试
        report = emergency_handler.generate_emergency_report()
        
        # 验证结果
        assert 'timestamp' in report
        assert report['trading_status'] == 'ALLOWED'
        assert 'recent_performance' in report
        assert 'alerts' in report
        assert 'recommendations' in report

    def test_generate_recommendations(self, emergency_handler):
        """测试生成建议措施"""
        # 测试正常状态
        status = {
            'trading_allowed': True,
            'consecutive_losses': 0,
            'daily_pnl': Decimal('0')
        }
        recommendations = emergency_handler._generate_recommendations(status)
        assert "系统运行正常" in recommendations[0]
        
        # 测试连续亏损
        status = {
            'trading_allowed': True,
            'consecutive_losses': 2,
            'daily_pnl': Decimal('0')
        }
        recommendations = emergency_handler._generate_recommendations(status)
        assert any("连续亏损" in r for r in recommendations)
        
        # 测试当日亏损较大
        status = {
            'trading_allowed': True,
            'consecutive_losses': 0,
            'daily_pnl': Decimal('-25')
        }
        recommendations = emergency_handler._generate_recommendations(status)
        assert any("当日亏损" in r for r in recommendations)


class TestEmergencyHandlerSingleton:
    """测试应急处理器单例模式"""

    def test_get_emergency_handler_singleton(self):
        """测试获取全局实例"""
        # 清除全局实例
        import core.emergency_handler as eh_module
        eh_module._global_emergency_handler = None
        
        # 获取实例
        handler1 = get_emergency_handler()
        handler2 = get_emergency_handler()
        
        # 应该是同一个实例
        assert handler1 is handler2

    def test_get_emergency_handler_with_params(self):
        """测试使用自定义参数获取实例"""
        # 清除全局实例
        import core.emergency_handler as eh_module
        eh_module._global_emergency_handler = None
        
        # Mock 参数
        mock_params = Mock()
        
        # 获取实例
        handler = get_emergency_handler(params=mock_params)
        
        # 验证
        assert handler.params == mock_params


class TestEmergencyHandlerIntegration:
    """测试应急处理器集成场景"""

    @pytest.fixture
    def emergency_handler(self):
        """创建应急处理器实例"""
        mock_params = Mock()
        mock_params.get = Mock(side_effect=lambda key, default=None: {
            'emergency.extreme_market_threshold': Decimal('5.0'),
            'emergency.max_daily_loss': Decimal('30'),
            'emergency.max_consecutive_losses': 3,
            'emergency.max_total_drawdown': Decimal('0.1')
        }.get(key, default))
        
        return EmergencyHandler(params=mock_params)

    def test_multiple_emergency_checks(self, emergency_handler):
        """测试多次应急检查"""
        # 1. 检查极端行情
        is_extreme = emergency_handler.check_extreme_market('BTCUSDT', Decimal('3.0'))
        assert is_extreme is False
        
        # 2. 检查单日亏损
        should_halt = emergency_handler.check_daily_loss(Decimal('-15'))
        assert should_halt is False
        
        # 3. 检查连续亏损
        trades = [
            {'pnl': Decimal('-10')},
            {'pnl': Decimal('-8')}
        ]
        should_halt = emergency_handler.check_consecutive_losses(trades)
        assert should_halt is False
        
        # 4. 检查总资金回撤
        should_halt = emergency_handler.check_total_drawdown(Decimal('500'), Decimal('470'))
        assert should_halt is False
        
        # 5. 确认交易仍然允许
        allowed, _ = emergency_handler.is_trading_allowed()
        assert allowed is True

    def test_emergency_scenario_daily_loss(self, emergency_handler):
        """测试单日亏损超限场景"""
        # 模拟当日亏损超限
        should_halt = emergency_handler.check_daily_loss(Decimal('-35'))
        assert should_halt is True
        
        # 确认交易已停止
        allowed, reason = emergency_handler.is_trading_allowed()
        assert allowed is False
        assert "停止交易中" in reason
        
        # 尝试检查其他项（应该仍然返回停止状态）
        is_extreme = emergency_handler.check_extreme_market('BTCUSDT', Decimal('3.0'))
        assert is_extreme is False  # 这个检查本身不受影响

    def test_emergency_scenario_consecutive_losses(self, emergency_handler):
        """测试连续亏损超限场景"""
        # 模拟连续亏损
        trades = [
            {'pnl': Decimal('-10')},
            {'pnl': Decimal('-12')},
            {'pnl': Decimal('-8')}
        ]
        
        should_halt = emergency_handler.check_consecutive_losses(trades)
        assert should_halt is True
        
        # 确认交易已停止
        allowed, reason = emergency_handler.is_trading_allowed()
        assert allowed is False
        assert "连续亏损" in reason

    def test_emergency_scenario_total_drawdown(self, emergency_handler):
        """测试总资金回撤超限场景"""
        # 模拟总资金回撤超限
        should_halt = emergency_handler.check_total_drawdown(Decimal('500'), Decimal('420'))
        assert should_halt is True
        
        # 确认交易已停止（无限期）
        allowed, reason = emergency_handler.is_trading_allowed()
        assert allowed is False
        
        # 手动恢复
        emergency_handler.reset_trading_halt()
        allowed, _ = emergency_handler.is_trading_allowed()
        assert allowed is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
