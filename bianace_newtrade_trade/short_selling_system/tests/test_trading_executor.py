"""
交易执行器单元测试
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trading_executor import TradingExecutor


class TestTradingExecutor:
    """交易执行器测试类"""
    
    @pytest.fixture
    def executor(self):
        """创建测试用的执行器实例"""
        return TradingExecutor()
    
    def test_calculate_stop_loss_fixed_percent(self, executor):
        """测试固定百分比止损计算"""
        entry_price = 100.0
        recent_high = 110.0
        stop_loss_percent = 3.0
        
        # 固定 3% 止损应该比技术位止损更严格
        stop_loss = executor.calculate_stop_loss(entry_price, recent_high, stop_loss_percent)
        
        # 固定 3% 止损 = 100 * 1.03 = 103
        expected = 103.0
        assert abs(stop_loss - expected) < 0.01, f"Expected {expected}, got {stop_loss}"
    
    def test_calculate_stop_loss_technical(self, executor):
        """测试技术位止损计算"""
        entry_price = 100.0
        recent_high = 102.0
        stop_loss_percent = 5.0
        
        # 技术位止损 = 102 * 1.02 = 104.04
        # 固定 5% 止损 = 100 * 1.05 = 105
        # 应该返回更宽松的技术位
        stop_loss = executor.calculate_stop_loss(entry_price, recent_high, stop_loss_percent)
        
        expected = 104.04  # 技术位更严格
        assert abs(stop_loss - expected) < 0.01, f"Expected {expected}, got {stop_loss}"
    
    def test_calculate_take_profit(self, executor):
        """测试止盈计算"""
        entry_price = 100.0
        tp1_pct = 20.0
        tp2_pct = 30.0
        
        tp1, tp2 = executor.calculate_take_profit(entry_price, tp1_pct, tp2_pct)
        
        assert abs(tp1 - 80.0) < 0.01, f"Expected TP1=80, got {tp1}"
        assert abs(tp2 - 70.0) < 0.01, f"Expected TP2=70, got {tp2}"
    
    @patch('core.trading_executor.binance_client')
    def test_execute_short_trade_success(self, mock_binance_client, executor):
        """测试成功执行做空交易"""
        # Mock Binance API 响应
        mock_binance_client.futures_create_order.return_value = {
            'orderId': '12345678',
            'symbol': 'BTCUSDT',
            'side': 'SELL',
            'status': 'FILLED',
            'avgPrice': '100.0',
            'executedQty': '0.01'
        }
        
        order_id = executor.execute_short_trade(
            symbol='BTCUSDT',
            entry_price=100.0,
            stop_loss=103.0,
            take_profit_1=80.0,
            take_profit_2=70.0,
            quantity=0.01,
            leverage=5,
            reason='测试订单'
        )
        
        assert order_id == '12345678'
        mock_binance_client.futures_create_order.assert_called_once()
    
    @patch('core.trading_executor.binance_client')
    def test_execute_short_trade_failure(self, mock_binance_client, executor):
        """测试交易执行失败"""
        mock_binance_client.futures_create_order.side_effect = Exception("API Error")
        
        with pytest.raises(Exception) as exc_info:
            executor.execute_short_trade(
                symbol='BTCUSDT',
                entry_price=100.0,
                stop_loss=103.0,
                take_profit_1=80.0,
                take_profit_2=70.0,
                quantity=0.01,
                leverage=5,
                reason='测试订单'
            )
        
        assert "API Error" in str(exc_info.value)
    
    @patch('core.trading_executor.binance_client')
    def test_close_position_profit(self, mock_binance_client, executor):
        """测试平仓 - 盈利场景"""
        # Mock 开仓信息
        executor.positions['TEST'] = {
            'entry_price': 100.0,
            'quantity': 0.01,
            'side': 'SHORT'
        }
        
        # Mock API 响应
        mock_binance_client.futures_create_order.return_value = {
            'orderId': '87654321',
            'symbol': 'TESTUSDT',
            'side': 'BUY',
            'status': 'FILLED',
            'avgPrice': '80.0',
            'executedQty': '0.01'
        }
        
        pnl = executor.close_position(
            symbol='TEST',
            exit_price=80.0,
            reason='止盈'
        )
        
        # 盈利 = (100 - 80) * 0.01 = 0.2 USDT
        assert pnl > 0
        assert 'TEST' not in executor.positions  # 仓位已清除
    
    @patch('core.trading_executor.binance_client')
    def test_close_position_loss(self, mock_binance_client, executor):
        """测试平仓 - 亏损场景"""
        executor.positions['TEST'] = {
            'entry_price': 100.0,
            'quantity': 0.01,
            'side': 'SHORT'
        }
        
        mock_binance_client.futures_create_order.return_value = {
            'orderId': '87654321',
            'avgPrice': '105.0',
            'executedQty': '0.01'
        }
        
        pnl = executor.close_position(
            symbol='TEST',
            exit_price=105.0,
            reason='止损'
        )
        
        # 亏损 = (100 - 105) * 0.01 = -0.05 USDT
        assert pnl < 0
    
    def test_check_stop_loss_triggered(self, executor):
        """测试止损触发"""
        executor.positions['TEST'] = {
            'entry_price': 100.0,
            'stop_loss': 103.0,
            'quantity': 0.01,
            'side': 'SHORT'
        }
        
        # 当前价格 104 > 止损价 103，应该触发止损
        result = executor.check_stop_loss('TEST', 104.0)
        
        assert result == 104.0
    
    def test_check_stop_loss_not_triggered(self, executor):
        """测试止损未触发"""
        executor.positions['TEST'] = {
            'entry_price': 100.0,
            'stop_loss': 103.0,
            'quantity': 0.01,
            'side': 'SHORT'
        }
        
        # 当前价格 102 < 止损价 103，不触发
        result = executor.check_stop_loss('TEST', 102.0)
        
        assert result is None
    
    def test_check_take_profit_triggered(self, executor):
        """测试止盈触发"""
        executor.positions['TEST'] = {
            'entry_price': 100.0,
            'take_profit_1': 80.0,
            'take_profit_2': 70.0,
            'quantity': 0.01,
            'side': 'SHORT'
        }
        
        # 价格跌到 75，应该触发第一目标止盈
        result = executor.check_take_profit('TEST', 75.0)
        
        assert result == 75.0
    
    def test_check_time_stop_triggered(self, executor):
        """测试时间停止触发"""
        # 设置 25 小时前的开仓时间
        old_time = datetime.now() - timedelta(hours=25)
        executor.positions['TEST'] = {
            'entry_price': 100.0,
            'open_time': old_time,
            'quantity': 0.01,
            'side': 'SHORT'
        }
        
        result = executor.check_time_stop('TEST')
        
        assert result == 'TIME_STOP'
    
    def test_check_time_stop_not_triggered(self, executor):
        """测试时间停止未触发"""
        # 设置 10 小时前的开仓时间
        recent_time = datetime.now() - timedelta(hours=10)
        executor.positions['TEST'] = {
            'entry_price': 100.0,
            'open_time': recent_time,
            'quantity': 0.01,
            'side': 'SHORT'
        }
        
        result = executor.check_time_stop('TEST')
        
        assert result is None
    
    def test_get_position_info(self, executor):
        """测试获取仓位信息"""
        executor.positions['TEST'] = {
            'entry_price': 100.0,
            'quantity': 0.01,
            'side': 'SHORT',
            'open_time': datetime.now(),
            'reason': '测试仓位'
        }
        
        info = executor.get_position_info('TEST')
        
        assert info is not None
        assert info['entry_price'] == 100.0
        assert info['quantity'] == 0.01
    
    def test_get_position_info_not_exists(self, executor):
        """测试获取不存在的仓位信息"""
        info = executor.get_position_info('NONEXISTENT')
        
        assert info is None
    
    def test_get_all_positions(self, executor):
        """测试获取所有仓位"""
        executor.positions['TEST1'] = {'entry_price': 100.0, 'quantity': 0.01}
        executor.positions['TEST2'] = {'entry_price': 200.0, 'quantity': 0.02}
        
        positions = executor.get_all_positions()
        
        assert len(positions) == 2
        assert 'TEST1' in positions
        assert 'TEST2' in positions
    
    def test_leverage_validation(self, executor):
        """测试杠杆倍数验证"""
        with pytest.raises(ValueError):
            executor.execute_short_trade(
                symbol='BTCUSDT',
                entry_price=100.0,
                stop_loss=103.0,
                take_profit_1=80.0,
                take_profit_2=70.0,
                quantity=0.01,
                leverage=101,  # 超过最大 100 倍
                reason='测试'
            )
        
        with pytest.raises(ValueError):
            executor.execute_short_trade(
                symbol='BTCUSDT',
                entry_price=100.0,
                stop_loss=103.0,
                take_profit_1=80.0,
                take_profit_2=70.0,
                quantity=0.01,
                leverage=0,  # 无效倍数
                reason='测试'
            )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
