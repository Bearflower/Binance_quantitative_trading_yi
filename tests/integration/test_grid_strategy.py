"""
网格交易策略集成测试
测试策略初始化、网格设置、订单执行等功能
"""
import pytest
from decimal import Decimal
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime

from strategies.grid.strategy import GridStrategy
from strategies.grid.grid_calculator import GridCalculator, GridLevel
from strategies.grid.order_manager import OrderManager
from strategies.grid.position_manager import PositionManager
from strategies.grid.risk_manager import RiskManager
from shared.binance_api import BinanceClient
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.database import DatabaseManager


# 测试配置
TEST_CONFIG = {
    'strategy': {
        'name': 'grid_trading',
        'version': '1.0.0'
    },
    'symbols': ['BTCUSDT'],
    'grid': {
        'type': 'arithmetic',
        'count': 10,
        'spacing': 100,
        'spacing_ratio': 1.01,
        'base_quantity': 0.001,
        'price_range': {
            'min': 50000,
            'max': 70000
        }
    },
    'trading': {
        'leverage': 5,
        'max_positions': 2,
        'single_position_margin': 100,
        'time_in_force': 'GTC'
    },
    'risk': {
        'max_drawdown': 0.1,
        'max_position_ratio': 0.3,
        'daily_loss_limit': 0.05,
        'stop_loss_percent': 0.1,
        'grid_reset_threshold': 0.15
    },
    'kline': {
        'interval': '1h',
        'limit': 100
    },
    'monitor': {
        'check_interval': 10,
        'save_interval': 60,
        'grid_check_interval': 300
    },
    'notification': {
        'enabled': True,
        'project': 'grid',
        'levels': ['info', 'warning', 'error']
    }
}


class TestGridCalculator:
    """网格计算器测试"""

    def test_calculator_initialization(self):
        """测试网格计算器初始化"""
        calculator = GridCalculator(TEST_CONFIG)

        assert calculator.grid_type == 'arithmetic'
        assert calculator.grid_count == 10
        assert calculator.grid_spacing == Decimal('100')
        assert calculator.base_quantity == Decimal('0.001')

    def test_calculate_arithmetic_grid(self):
        """测试等差网格计算"""
        calculator = GridCalculator(TEST_CONFIG)
        current_price = Decimal('60000')

        levels = calculator.calculate_grid_levels(current_price)

        # 验证网格层级数量
        assert len(levels) > 0

        # 验证价格范围
        prices = [l.price for l in levels]
        assert min(prices) >= Decimal('50000')
        assert max(prices) <= Decimal('70000')

        # 验证买卖方向
        buy_levels = [l for l in levels if l.side == 'BUY']
        sell_levels = [l for l in levels if l.side == 'SELL']

        assert len(buy_levels) > 0
        assert len(sell_levels) > 0

    def test_calculate_geometric_grid(self):
        """测试等比网格计算"""
        config = TEST_CONFIG.copy()
        config['grid']['type'] = 'geometric'

        calculator = GridCalculator(config)
        current_price = Decimal('60000')

        levels = calculator.calculate_grid_levels(current_price)

        assert len(levels) > 0

        # 验证等比关系
        for i in range(1, len(levels)):
            ratio = levels[i].price / levels[i-1].price
            assert abs(ratio - Decimal('1.01')) < Decimal('0.001')

    def test_calculate_reverse_price(self):
        """测试反向价格计算"""
        calculator = GridCalculator(TEST_CONFIG)

        # 测试买单反向价格
        buy_level = GridLevel(
            price=Decimal('59000'),
            side='BUY',
            quantity=Decimal('0.001')
        )
        reverse_price = calculator.calculate_reverse_price(buy_level)
        assert reverse_price == Decimal('59100')  # 59000 + 100

        # 测试卖单反向价格
        sell_level = GridLevel(
            price=Decimal('61000'),
            side='SELL',
            quantity=Decimal('0.001')
        )
        reverse_price = calculator.calculate_reverse_price(sell_level)
        assert reverse_price == Decimal('60900')  # 61000 - 100


class TestPositionManager:
    """持仓管理器测试"""

    @pytest.fixture
    def position_manager(self):
        """创建持仓管理器实例"""
        mock_binance = Mock(spec=BinanceClient)
        mock_db = Mock(spec=DatabaseManager)
        return PositionManager(mock_binance, mock_db, TEST_CONFIG)

    def test_update_position_buy(self, position_manager):
        """测试买入更新持仓"""
        position_manager.update_position(
            symbol='BTCUSDT',
            side='BUY',
            quantity=Decimal('0.001'),
            price=Decimal('60000')
        )

        position = position_manager.get_position('BTCUSDT')
        assert position is not None
        assert position['quantity'] == Decimal('0.001')
        assert position['avg_price'] == Decimal('60000')

    def test_update_position_sell(self, position_manager):
        """测试卖出更新持仓"""
        # 先买入
        position_manager.update_position(
            symbol='BTCUSDT',
            side='BUY',
            quantity=Decimal('0.002'),
            price=Decimal('60000')
        )

        # 再卖出
        position_manager.update_position(
            symbol='BTCUSDT',
            side='SELL',
            quantity=Decimal('0.001'),
            price=Decimal('61000')
        )

        position = position_manager.get_position('BTCUSDT')
        assert position['quantity'] == Decimal('0.001')
        # 已实现盈亏 = (61000 - 60000) * 0.001 = 1 USDT
        assert position['realized_pnl'] == Decimal('1')

    def test_get_position_pnl(self, position_manager):
        """测试持仓盈亏计算"""
        position_manager.update_position(
            symbol='BTCUSDT',
            side='BUY',
            quantity=Decimal('0.001'),
            price=Decimal('60000')
        )

        pnl = position_manager.get_position_pnl(
            symbol='BTCUSDT',
            current_price=Decimal('61000')
        )

        # 未实现盈亏 = (61000 - 60000) * 0.001 = 1 USDT
        assert pnl == Decimal('1')


class TestRiskManager:
    """风控管理器测试"""

    @pytest.fixture
    def risk_manager(self):
        """创建风控管理器实例"""
        mock_binance = Mock(spec=BinanceClient)
        mock_db = Mock(spec=DatabaseManager)
        mock_notification = Mock(spec=NotificationClient)
        return RiskManager(mock_binance, mock_db, mock_notification, TEST_CONFIG)

    @pytest.mark.asyncio
    async def test_check_risk_normal(self, risk_manager):
        """测试正常情况下的风险检查"""
        result = await risk_manager.check_risk(
            symbol='BTCUSDT',
            position={'quantity': Decimal('0.001'), 'avg_price': Decimal('60000')},
            current_price=Decimal('60000'),
            account_balance=Decimal('10000')
        )

        assert not result.has_risk
        assert not result.should_stop

    @pytest.mark.asyncio
    async def test_check_risk_position_ratio(self, risk_manager):
        """测试仓位比例过高"""
        # 设置大仓位 (0.06 BTC * 60000 = 3600 USDT, 占账户余额 36% > 30%)
        result = await risk_manager.check_risk(
            symbol='BTCUSDT',
            position={'quantity': Decimal('0.06'), 'avg_price': Decimal('60000')},
            current_price=Decimal('60000'),
            account_balance=Decimal('10000')
        )

        assert result.has_risk
        assert any(r.type == 'POSITION_RATIO' for r in result.risks)


class TestGridStrategy:
    """网格策略测试"""

    @pytest.fixture
    def strategy(self):
        """创建策略实例"""
        return GridStrategy(TEST_CONFIG)

    @pytest.fixture
    def mock_clients(self):
        """创建模拟客户端"""
        mock_binance = Mock(spec=BinanceClient)
        mock_binance.get_ticker_price = AsyncMock(return_value={'price': '60000'})
        mock_binance.get_account_balance = AsyncMock(return_value={'USDT': 10000})
        mock_binance.place_order = AsyncMock(return_value={'orderId': 12345})
        mock_binance.cancel_order = AsyncMock(return_value={})
        mock_binance.get_open_orders = AsyncMock(return_value=[])

        mock_kline = Mock(spec=KLineService)
        mock_kline.get_klines = AsyncMock(return_value=[
            {'close': '60000'} for _ in range(100)
        ])

        mock_notification = Mock(spec=NotificationClient)
        mock_notification.send = AsyncMock(return_value=True)
        mock_notification.send_alert = AsyncMock(return_value=True)
        mock_notification.send_trade_notification = AsyncMock(return_value=True)

        mock_db = Mock(spec=DatabaseManager)

        return {
            'binance': mock_binance,
            'kline': mock_kline,
            'notification': mock_notification,
            'db': mock_db
        }

    @pytest.mark.asyncio
    async def test_strategy_initialization(self, strategy, mock_clients):
        """测试策略初始化"""
        await strategy.set_binance_client(mock_clients['binance'])
        await strategy.set_kline_service(mock_clients['kline'])
        await strategy.set_notification_client(mock_clients['notification'])
        await strategy.set_database(mock_clients['db'])

        await strategy.initialize()

        assert strategy.grid_calculator is not None
        assert strategy.order_manager is not None
        assert strategy.position_manager is not None
        assert strategy.risk_manager is not None

    @pytest.mark.asyncio
    async def test_strategy_analyze(self, strategy, mock_clients):
        """测试策略分析"""
        await strategy.set_binance_client(mock_clients['binance'])
        await strategy.set_kline_service(mock_clients['kline'])
        await strategy.set_notification_client(mock_clients['notification'])
        await strategy.set_database(mock_clients['db'])

        await strategy.initialize()

        result = await strategy.analyze('BTCUSDT')

        assert result is not None
        assert 'symbol' in result
        assert 'current_price' in result
        assert 'grid_levels' in result
        assert result['symbol'] == 'BTCUSDT'

    @pytest.mark.asyncio
    async def test_strategy_execute_signal(self, strategy, mock_clients):
        """测试策略执行信号"""
        await strategy.set_binance_client(mock_clients['binance'])
        await strategy.set_kline_service(mock_clients['kline'])
        await strategy.set_notification_client(mock_clients['notification'])
        await strategy.set_database(mock_clients['db'])

        await strategy.initialize()

        # 创建测试网格层级
        grid_levels = [
            GridLevel(price=Decimal('59000'), side='BUY', quantity=Decimal('0.001')),
            GridLevel(price=Decimal('61000'), side='SELL', quantity=Decimal('0.001'))
        ]

        signal = {
            'type': 'INITIALIZE_GRID',
            'symbol': 'BTCUSDT',
            'grid_levels': grid_levels
        }

        success = await strategy.execute_signal(signal)

        assert success
        assert 'BTCUSDT' in strategy.grid_states


class TestOrderManager:
    """订单管理器测试"""

    @pytest.fixture
    def order_manager(self):
        """创建订单管理器实例"""
        mock_binance = Mock(spec=BinanceClient)
        mock_binance.place_order = AsyncMock(return_value={'orderId': 12345})
        mock_binance.cancel_order = AsyncMock(return_value={})
        mock_binance.get_open_orders = AsyncMock(return_value=[])

        mock_db = Mock(spec=DatabaseManager)
        mock_notification = Mock(spec=NotificationClient)
        mock_notification.send_trade_notification = AsyncMock(return_value=True)

        return OrderManager(mock_binance, mock_db, mock_notification, TEST_CONFIG)

    @pytest.mark.asyncio
    async def test_place_grid_order(self, order_manager):
        """测试挂网格单"""
        level = GridLevel(
            price=Decimal('60000'),
            side='BUY',
            quantity=Decimal('0.001')
        )

        order = await order_manager.place_grid_order('BTCUSDT', level)

        assert order is not None
        assert order['orderId'] == 12345
        assert 12345 in order_manager.pending_orders

    @pytest.mark.asyncio
    async def test_cancel_all_orders(self, order_manager):
        """测试撤销所有订单"""
        # 先挂单
        level = GridLevel(
            price=Decimal('60000'),
            side='BUY',
            quantity=Decimal('0.001')
        )
        await order_manager.place_grid_order('BTCUSDT', level)

        # 撤销订单
        cancelled_count = await order_manager.cancel_all_orders('BTCUSDT')

        assert cancelled_count == 1
        assert len(order_manager.pending_orders) == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
