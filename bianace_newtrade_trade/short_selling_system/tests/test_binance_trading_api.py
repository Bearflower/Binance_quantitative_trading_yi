"""
币安交易 API 测试模块

测试：
- 下单（市价/限价）
- 查询订单
- 撤销订单
- 止盈止损
- 持仓查询
- 账户余额查询
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from core.binance_trading_api import BinanceTradingAPI
from core.trading_executor import TradingExecutor


class TestBinanceTradingAPI:
    """币安交易 API 测试类"""
    
    @pytest.fixture
    def api(self):
        """创建 API 实例"""
        with patch('core.binance_trading_api.settings'):
            api = BinanceTradingAPI()
            api.api_key = "test_api_key"
            api.secret_key = "test_secret_key"
            return api
    
    def test_generate_signature(self, api):
        """测试签名生成"""
        params = {'symbol': 'BTCUSDT', 'timestamp': 1234567890}
        signature = api._generate_signature(params)
        
        assert signature is not None
        assert len(signature) == 64  # SHA256 签名长度
    
    def test_adjust_quantity(self, api):
        """测试数量精度调整（增强版）"""
        with patch.object(api, 'get_symbol_precision', return_value={
            'quantity_precision': 3,
            'step_size': 0.001,
            'min_qty': 0.001,
            'max_qty': 1000
        }):
            # 测试调整数量
            adjusted = api.adjust_quantity('BTCUSDT', 0.123456)
            assert adjusted == 0.123
            
            # 测试 step_size 对齐
            adjusted = api.adjust_quantity('BTCUSDT', 0.1237)
            assert adjusted == 0.123  # 向下取整
            
            # 测试小于最小值
            adjusted = api.adjust_quantity('BTCUSDT', 0.0001)
            assert adjusted == 0.001  # 调整为最小值
            
            # 测试大于最大值
            adjusted = api.adjust_quantity('BTCUSDT', 1001.0)
            assert adjusted == 1000  # 调整为最大值
    
    def test_adjust_price(self, api):
        """测试价格精度调整（向下取整）"""
        with patch.object(api, 'get_symbol_precision', return_value={
            'price_precision': 2,
            'tick_size': 0.01
        }):
            # 测试调整价格（向下取整到 tick_size 的整数倍）
            adjusted = api.adjust_price('BTCUSDT', 50123.456)
            assert adjusted == 50123.45  # 向下取整
            
            # 测试 tick_size 对齐
            adjusted = api.adjust_price('BTCUSDT', 50123.457)
            assert adjusted == 50123.45  # 向下取整
            
            # 测试正好是 tick_size 的整数倍
            adjusted = api.adjust_price('BTCUSDT', 50123.46)
            assert adjusted == 50123.46
    
    @patch('core.binance_trading_api.requests.post')
    def test_place_market_order(self, mock_post, api):
        """测试市价单"""
        # Mock 响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'orderId': '12345678',
            'symbol': 'BTCUSDT',
            'side': 'SELL',
            'status': 'FILLED',
            'avgPrice': '50000.0',
            'executedQty': '0.01'
        }
        mock_post.return_value = mock_response
        
        # 测试下单
        with patch.object(api, 'adjust_quantity', return_value=0.01):
            result = api.place_market_order(
                symbol='BTCUSDT',
                side='SELL',
                quantity=0.01,
                position_side='SHORT'
            )
        
        assert result is not None
        assert result['orderId'] == '12345678'
        assert result['status'] == 'FILLED'
    
    @patch('core.binance_trading_api.requests.post')
    def test_place_stop_loss_order(self, mock_post, api):
        """测试止损单"""
        # Mock 响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'orderId': '87654321',
            'symbol': 'BTCUSDT',
            'status': 'NEW'
        }
        mock_post.return_value = mock_response
        
        # 测试止损单
        result = api.place_stop_loss_order(
            symbol='BTCUSDT',
            side='BUY',
            quantity=0.01,
            stop_price=52000.0,
            position_side='SHORT'
        )
        
        assert result is not None
        assert result['orderId'] == '87654321'
    
    @patch('core.binance_trading_api.requests.delete')
    def test_cancel_order(self, mock_delete, api):
        """测试撤销订单"""
        # Mock 响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'orderId': '12345678',
            'status': 'CANCELED'
        }
        mock_delete.return_value = mock_response
        
        # 测试撤销
        result = api.cancel_order(
            symbol='BTCUSDT',
            order_id=12345678
        )
        
        assert result is not None
        assert result['status'] == 'CANCELED'
    
    @patch('core.binance_trading_api.requests.get')
    def test_query_order(self, mock_get, api):
        """测试查询订单"""
        # Mock 响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'orderId': '12345678',
            'symbol': 'BTCUSDT',
            'status': 'FILLED',
            'avgPrice': '50000.0',
            'executedQty': '0.01'
        }
        mock_get.return_value = mock_response
        
        # 测试查询
        result = api.query_order(
            symbol='BTCUSDT',
            order_id=12345678
        )
        
        assert result is not None
        assert result['status'] == 'FILLED'
        assert float(result['avgPrice']) == 50000.0
    
    @patch('core.binance_trading_api.requests.get')
    def test_get_position(self, mock_get, api):
        """测试查询持仓"""
        # Mock 响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                'symbol': 'BTCUSDT',
                'positionAmt': '0.01',
                'entryPrice': '50000.0',
                'markPrice': '51000.0',
                'unrealizedProfit': '10.0',
                'positionSide': 'SHORT',
                'leverage': '5'
            }
        ]
        mock_get.return_value = mock_response
        
        # 测试查询持仓
        positions = api.get_position()
        
        assert len(positions) == 1
        assert positions[0]['symbol'] == 'BTCUSDT'
        assert float(positions[0]['unrealizedProfit']) == 10.0
    
    @patch('core.binance_trading_api.requests.get')
    def test_get_account_balance(self, mock_get, api):
        """测试查询余额"""
        # Mock 响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                'asset': 'USDT',
                'walletBalance': '1000.0',
                'unrealizedProfit': '10.0',
                'availableBalance': '990.0'
            }
        ]
        mock_get.return_value = mock_response
        
        # 测试查询余额
        balances = api.get_account_balance()
        
        assert len(balances) == 1
        assert balances[0]['asset'] == 'USDT'
        assert float(balances[0]['walletBalance']) == 1000.0


class TestTradingExecutor:
    """交易执行器测试类"""
    
    @pytest.fixture
    def executor(self):
        """创建交易执行器实例"""
        with patch('core.trading_executor.settings'):
            return TradingExecutor()
    
    @patch('core.trading_executor.binance_trading_api')
    def test_execute_short_trade(self, mock_api, executor):
        """测试执行做空交易"""
        # Mock API 响应
        mock_api.set_leverage.return_value = {'leverage': 5}
        mock_api.place_market_order.return_value = {
            'orderId': '12345678',
            'status': 'FILLED',
            'executedQty': '0.01',
            'avgPrice': '50000.0'
        }
        mock_api.place_stop_loss_order.return_value = {
            'orderId': '87654321',
            'status': 'NEW'
        }
        mock_api.place_take_profit_order.return_value = {
            'orderId': '11223344',
            'status': 'NEW'
        }
        
        # 测试交易执行
        order_id = executor.execute_short_trade(
            symbol='BTCUSDT',
            entry_price=50000.0,
            stop_loss=52000.0,
            take_profit_1=48000.0,
            take_profit_2=47000.0,
            quantity=0.01,
            leverage=5,
            reason='测试订单'
        )
        
        assert order_id == '12345678'
        assert 'BTCUSDT' in executor.positions
        assert executor.positions['BTCUSDT']['status'] == 'open'
        assert executor.positions['BTCUSDT']['entry_price'] == 50000.0
    
    @patch('core.trading_executor.binance_trading_api')
    def test_close_position(self, mock_api, executor):
        """测试平仓"""
        # 先创建持仓
        executor.positions['BTCUSDT'] = {
            'order_id': '12345678',
            'symbol': 'BTCUSDT',
            'entry_price': 50000.0,
            'quantity': 0.01,
            'leverage': 5,
            'status': 'open'
        }
        
        # Mock API 响应
        mock_api.get_mark_price.return_value = 48000.0
        mock_api.place_market_order.return_value = {
            'orderId': '99887766',
            'status': 'FILLED',
            'executedQty': '0.01',
            'avgPrice': '48000.0'
        }
        
        # 测试平仓
        pnl = executor.close_position(
            symbol='BTCUSDT',
            reason='manual'
        )
        
        # 做空盈亏 = (50000 - 48000) * 0.01 * 5 = 100 USDT
        assert pnl == 100.0
        assert executor.positions['BTCUSDT']['status'] == 'closed'
    
    def test_query_order(self, executor):
        """测试查询订单"""
        with patch('core.trading_executor.binance_trading_api') as mock_api:
            mock_api.query_order.return_value = {
                'orderId': '12345678',
                'status': 'FILLED'
            }
            
            result = executor.query_order('BTCUSDT', 12345678)
            
            assert result is not None
            assert result['status'] == 'FILLED'
    
    def test_cancel_order(self, executor):
        """测试撤销订单"""
        with patch('core.trading_executor.binance_trading_api') as mock_api:
            mock_api.cancel_order.return_value = {
                'orderId': '12345678',
                'status': 'CANCELED'
            }
            
            result = executor.cancel_order('BTCUSDT', 12345678)
            
            assert result is not None
            assert result['status'] == 'CANCELED'
    
    def test_get_order_history(self, executor):
        """测试获取订单历史"""
        # 添加订单记录
        executor.orders['12345678'] = {
            'symbol': 'BTCUSDT',
            'side': 'SELL',
            'type': 'MARKET',
            'quantity': 0.01,
            'avg_price': 50000.0
        }
        
        # 测试获取历史
        history = executor.get_order_history('BTCUSDT')
        
        assert len(history) == 1
        assert history[0]['symbol'] == 'BTCUSDT'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
