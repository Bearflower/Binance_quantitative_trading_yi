#!/usr/bin/env python3
"""
服务层模块测试

测试 services 模块的核心功能：
1. FrequencyController - 频率控制器
2. RuleTradeExecutor - 规则引擎交易执行器
3. TradeExecutor - 交易执行器
"""

import pytest
from decimal import Decimal
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta

from services.frequency_controller import FrequencyController, get_frequency_controller
from services.rule_executor import RuleTradeExecutor, get_rule_executor
from services.trade_executor import TradeExecutor, TradeSignal, get_trade_executor


class TestFrequencyController:
    """频率控制器测试类"""

    @pytest.fixture
    def mock_db(self):
        """创建 Mock 数据库"""
        db = Mock()
        db._execute_one = Mock(return_value={'count': 0})
        db._execute_query = Mock(return_value=[])
        return db

    @pytest.fixture
    def frequency_controller(self, mock_db):
        """创建频率控制器实例"""
        controller = FrequencyController(db_manager=mock_db)
        # 手动设置配置参数
        controller.max_trades_per_day = 4
        controller.max_trades_per_symbol_per_day = 2
        controller.cooldown_hours = 12
        controller.max_consecutive_losses = 5
        controller.max_daily_loss_amount = Decimal('25')
        controller.total_capital = Decimal('500')
        return controller

    def test_init(self, frequency_controller):
        """测试初始化"""
        assert frequency_controller is not None
        assert frequency_controller.max_trades_per_day == 4
        assert frequency_controller.max_trades_per_symbol_per_day == 2

    def test_check_trade_allowed_normal(self, frequency_controller, mock_db):
        """测试正常交易检查"""
        # Mock 数据库返回（今日交易数为 0）
        mock_db._execute_one = Mock(return_value={'count': 0})
        
        # 执行测试
        allowed, reason = frequency_controller.check_trade_allowed('BTCUSDT')
        
        # 验证结果
        assert allowed is True
        assert "通过" in reason

    def test_check_trade_allowed_daily_limit(self, frequency_controller, mock_db):
        """测试每日交易次数限制"""
        # Mock 数据库返回（今日交易数为 4）
        mock_db._execute_one = Mock(return_value={'count': 4})
        
        # 执行测试
        allowed, reason = frequency_controller.check_trade_allowed('BTCUSDT')
        
        # 验证结果
        assert allowed is False
        assert "交易上限" in reason

    def test_check_trade_allowed_symbol_limit(self, frequency_controller, mock_db):
        """测试单品种交易次数限制"""
        # Mock 数据库返回
        def mock_execute_one(query, params):
            if 'COUNT' in query:
                return {'count': 2}  # 该品种今日已交易 2 次
            return {'count': 0}
        
        mock_db._execute_one = Mock(side_effect=mock_execute_one)
        
        # 执行测试
        allowed, reason = frequency_controller.check_trade_allowed('BTCUSDT')
        
        # 验证结果
        assert allowed is False
        assert "交易上限" in reason

    def test_check_trade_allowed_cooldown(self, frequency_controller, mock_db):
        """测试冷却期检查"""
        # Mock 数据库返回
        def mock_execute_one(query, params):
            if 'COUNT' in query:
                return {'count': 0}
            if 'open_time' in query:
                # 最后交易时间是 1 小时前
                return {'open_time': datetime.now() - timedelta(hours=1)}
            return {'count': 0}
        
        mock_db._execute_one = Mock(side_effect=mock_execute_one)
        
        # 执行测试
        allowed, reason = frequency_controller.check_trade_allowed('BTCUSDT')
        
        # 验证结果（冷却期 12 小时，1 小时前交易，还在冷却期）
        assert allowed is False
        assert "冷却期" in reason

    def test_check_trade_allowed_consecutive_losses(self, frequency_controller, mock_db):
        """测试连续亏损检查"""
        # Mock 数据库返回
        def mock_execute_one(query, params):
            if 'COUNT' in query:
                return {'count': 0}
            return {'count': 0}
        
        def mock_execute_query(query):
            # 返回 5 笔连续亏损
            return [
                {'pnl': -10},
                {'pnl': -8},
                {'pnl': -12},
                {'pnl': -5},
                {'pnl': -15}
            ]
        
        mock_db._execute_one = Mock(side_effect=mock_execute_one)
        mock_db._execute_query = Mock(side_effect=mock_execute_query)
        
        # 执行测试
        allowed, reason = frequency_controller.check_trade_allowed('BTCUSDT')
        
        # 验证结果
        assert allowed is False
        assert "连续亏损" in reason

    def test_check_trade_allowed_daily_loss(self, frequency_controller, mock_db):
        """测试每日亏损限额检查"""
        # Mock 数据库返回
        def mock_execute_one(query, params):
            if 'COUNT' in query:
                return {'count': 0}
            if 'SUM' in query or 'COALESCE' in query:
                return {'total_pnl': -30}  # 当日亏损 30U
            return {'count': 0}
        
        mock_db._execute_one = Mock(side_effect=mock_execute_one)
        mock_db._execute_query = Mock(return_value=[])
        
        # 执行测试
        allowed, reason = frequency_controller.check_trade_allowed('BTCUSDT')
        
        # 验证结果
        assert allowed is False
        assert "亏损超限" in reason

    def test_record_trade(self, frequency_controller, mock_db):
        """测试记录交易"""
        # 执行测试
        frequency_controller.record_trade(
            symbol='BTCUSDT',
            trade_time=datetime.now(),
            pnl=Decimal('10'),
            direction='多'
        )
        
        # 验证数据库调用
        assert mock_db._execute_query.called

    def test_get_trade_stats(self, frequency_controller, mock_db):
        """测试获取交易统计"""
        # Mock 数据库返回
        def mock_execute_one(query, params):
            if 'COUNT' in query:
                return {'count': 2}
            if 'SUM' in query or 'COALESCE' in query:
                return {'total_pnl': 15}
            return {'count': 0}
        
        mock_db._execute_one = Mock(side_effect=mock_execute_one)
        mock_db._execute_query = Mock(return_value=[])
        
        # 执行测试
        stats = frequency_controller.get_trade_stats()
        
        # 验证结果
        assert stats['daily_total'] == 2
        assert stats['daily_max'] == 4
        assert stats['daily_pnl'] == 15.0


class TestRuleTradeExecutor:
    """规则引擎交易执行器测试类"""

    @pytest.fixture
    def mock_params(self):
        """创建 Mock 策略参数"""
        params = Mock()
        params.get = Mock(side_effect=lambda key, default=None: {
            'account.max_positions': 2,
            'account.total_capital': Decimal('500'),
            'account.max_total_margin_ratio': Decimal('0.3'),
            'position_sizing.max_total_notional': Decimal('4000')
        }.get(key, default))
        return params

    @pytest.fixture
    def rule_executor(self, mock_params):
        """创建规则执行器实例"""
        executor = RuleTradeExecutor(params=mock_params, testnet=True)
        # 手动设置组件
        executor.data_fetcher = Mock()
        executor.signal_detector = Mock()
        executor.position_calculator = Mock()
        executor.risk_manager = Mock()
        executor.trade_api = Mock()
        return executor

    def test_init(self, rule_executor):
        """测试初始化"""
        assert rule_executor is not None
        assert rule_executor.max_positions == 2
        assert rule_executor.total_capital == Decimal('500')

    def test_execute_signals_empty(self, rule_executor):
        """测试执行空信号列表"""
        # 执行测试
        result = rule_executor.execute_signals([])
        
        # 验证结果
        assert result['success'] is True
        assert result['total_signals'] == 0

    def test_execute_signals_full_positions(self, rule_executor):
        """测试满仓情况"""
        # Mock 当前持仓
        rule_executor._get_current_positions = Mock(return_value=[
            {'margin': Decimal('20')},
            {'margin': Decimal('25')}
        ])
        
        # 执行测试
        result = rule_executor.execute_signals([{'币种': 'BTCUSDT'}])
        
        # 验证结果
        assert result['success'] is False
        assert "满仓" in result['errors'][0]

    def test_pre_trade_check_pass(self, rule_executor):
        """测试交易前风险检查（通过）"""
        # 准备测试数据
        position_params = {
            'margin': Decimal('15'),
            'notional_value': Decimal('300')
        }
        
        # Mock 当前持仓
        rule_executor._get_current_positions = Mock(return_value=[])
        
        # 执行测试
        passed = rule_executor._pre_trade_check(position_params)
        
        # 验证结果
        assert passed is True

    def test_pre_trade_check_margin_exceeded(self, rule_executor):
        """测试交易前风险检查（保证金超限）"""
        # 准备测试数据
        position_params = {
            'margin': Decimal('100'),
            'notional_value': Decimal('500')
        }
        
        # Mock 当前持仓
        rule_executor._get_current_positions = Mock(return_value=[
            {'margin': Decimal('80')}
        ])
        
        # 执行测试
        passed = rule_executor._pre_trade_check(position_params)
        
        # 验证结果
        assert passed is False

    def test_get_current_positions(self, rule_executor):
        """测试获取当前持仓"""
        # Mock API 返回
        rule_executor.trade_api.get_position_risk = Mock(return_value=[
            {'positionAmt': '0.01'},
            {'positionAmt': '0'},
            {'positionAmt': '-0.02'}
        ])
        
        # 执行测试
        positions = rule_executor._get_current_positions()
        
        # 验证结果（过滤掉持仓为 0 的）
        assert len(positions) == 2


class TestTradeSignal:
    """交易信号测试类"""

    def test_create_signal(self):
        """测试创建交易信号"""
        signal_data = {
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'position_side': 'LONG',
            'action': 'OPEN',
            'quantity': '0.01',
            'price': '50000',
            'tp_price': '52000',
            'sl_price': '48000',
            'leverage': 20
        }
        
        signal = TradeSignal(signal_data)
        
        assert signal.symbol == 'BTCUSDT'
        assert signal.side == 'BUY'
        assert signal.quantity == Decimal('0.01')
        assert signal.price == Decimal('50000')

    def test_validate_signal_invalid_symbol(self):
        """测试验证信号（无效交易对）"""
        signal_data = {
            'symbol': '',
            'side': 'BUY',
            'action': 'OPEN',
            'quantity': '0.01'
        }
        
        with pytest.raises(ValueError, match="交易对不能为空"):
            TradeSignal(signal_data)

    def test_validate_signal_invalid_side(self):
        """测试验证信号（无效方向）"""
        signal_data = {
            'symbol': 'BTCUSDT',
            'side': 'INVALID',
            'action': 'OPEN',
            'quantity': '0.01'
        }
        
        with pytest.raises(ValueError, match="无效的方向"):
            TradeSignal(signal_data)

    def test_validate_signal_invalid_quantity(self):
        """测试验证信号（无效数量）"""
        signal_data = {
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'action': 'OPEN',
            'quantity': '0'
        }
        
        with pytest.raises(ValueError, match="数量必须大于 0"):
            TradeSignal(signal_data)


class TestTradeExecutor:
    """交易执行器测试类"""

    @pytest.fixture
    def trade_executor(self):
        """创建交易执行器实例"""
        # Mock 数据库连接和 API
        with patch('services.trade_executor.get_db_manager') as mock_db, \
             patch('services.trade_executor.get_trade_api') as mock_api:
            mock_db.return_value = Mock()
            mock_api.return_value = Mock()
            
            executor = TradeExecutor()
            # 手动设置组件
            executor.notifier = Mock()
            executor.default_leverage = 20
            executor.max_positions = 2
            executor.single_position_margin = Decimal('30')
            return executor

    def test_init(self, trade_executor):
        """测试初始化"""
        assert trade_executor is not None
        assert trade_executor.default_leverage == 20

    def test_parse_analysis_result_json(self, trade_executor):
        """测试解析 AI 分析结果（JSON 格式）"""
        analysis_text = '{"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "action": "OPEN", "quantity": "0.01"}'
        
        signals = trade_executor.parse_analysis_result(analysis_text)
        
        assert len(signals) == 1
        assert signals[0].symbol == 'BTCUSDT'

    def test_parse_analysis_result_text(self, trade_executor):
        """测试解析 AI 分析结果（文本格式）"""
        analysis_text = "开仓 BTCUSDT 多单，数量 0.001, 价格 50000, 止盈 52000, 止损 49000"
        
        signals = trade_executor.parse_analysis_result(analysis_text)
        
        assert len(signals) == 1
        assert signals[0].symbol == 'BTCUSDT'
        assert signals[0].side == 'BUY'
        assert signals[0].position_side == 'LONG'

    def test_parse_analysis_result_empty(self, trade_executor):
        """测试解析空分析结果"""
        signals = trade_executor.parse_analysis_result("")
        
        assert len(signals) == 0


class TestServicesIntegration:
    """服务层集成测试"""

    @pytest.fixture
    def mock_db(self):
        """创建 Mock 数据库"""
        db = Mock()
        db._execute_one = Mock(return_value={'count': 0})
        db._execute_query = Mock(return_value=[])
        return db

    def test_frequency_controller_and_executor_integration(self, mock_db):
        """测试频率控制器和执行器集成"""
        # 创建频率控制器
        frequency_controller = FrequencyController(db_manager=mock_db)
        frequency_controller.max_trades_per_day = 4
        frequency_controller.max_trades_per_symbol_per_day = 2
        frequency_controller.cooldown_hours = 12
        frequency_controller.max_consecutive_losses = 5
        frequency_controller.max_daily_loss_amount = Decimal('25')
        frequency_controller.total_capital = Decimal('500')
        
        # 检查交易是否允许
        allowed, reason = frequency_controller.check_trade_allowed('BTCUSDT')
        
        # 验证结果
        assert allowed is True

    def test_signal_to_order_flow(self):
        """测试信号到订单的完整流程"""
        # 创建交易信号
        signal_data = {
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'position_side': 'LONG',
            'action': 'OPEN',
            'quantity': '0.01',
            'price': '50000',
            'tp_price': '52000',
            'sl_price': '48000',
            'leverage': 20
        }
        
        signal = TradeSignal(signal_data)
        
        # 验证信号创建成功
        assert signal.symbol == 'BTCUSDT'
        assert signal.quantity == Decimal('0.01')
        assert signal.price == Decimal('50000')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
