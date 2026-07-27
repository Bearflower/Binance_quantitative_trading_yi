"""
限价单改造测试脚本（V4.1：市价单改为限价单）

测试范围：
1. 配置读取测试 - 验证 limit_order_slippage 配置读取与默认值
2. 限价计算逻辑测试 - 验证止损/止盈/平仓限价计算公式
3. 订单类型测试 - 验证各方法使用正确的订单类型（LIMIT/STOP/TAKE_PROFIT）
4. 平仓容错测试 - 验证限价单失败时回退到市价单
5. 边界条件测试 - 验证滑点为0、较大值、价格异常等情况

使用 mock 模拟 BinanceClient、DatabaseManager、NotificationClient，不实际调用币安API。
"""

import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock
from decimal import Decimal

# 添加项目根目录到 Python 路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, project_root)

from strategies.new_coin.executor import TradingExecutor


# =============================================================================
# 辅助函数
# =============================================================================

def make_config(slippage=0.001, **overrides):
    """构造测试用配置字典"""
    trading = {
        'leverage': 2,
        'max_positions': 3,
        'single_position_margin': 50,
        'stop_loss_percent': 0.05,
        'take_profit_percent': 0.10,
        'limit_order_slippage': slippage,
        'batch_take_profit': {
            'enabled': True,
            'target1_atr_multiplier': 1.5,
            'target1_close_percent': 0.30,
            'target2_atr_multiplier': 3.5,
            'target2_close_percent': 0.40,
            'trailing_stop_atr_multiplier': 1.5,
        },
        'time_stop': {
            'enabled': True,
            'max_holding_hours': 72,
        },
        'emergency_stop': {
            'enabled': True,
            'check_minutes': 15,
            'trigger_percent': 0.015,
        },
        'atr_stop': {
            'multiplier': 2.5,
        },
        'risk_control': {
            'max_loss_percent': 0.02,
        },
    }
    trading.update(overrides)
    return {'trading': trading}


def make_executor(config=None, slippage=0.001):
    """构造测试用 TradingExecutor 实例（带 mock 依赖）

    Returns:
        (executor, binance_api, db, notification) 四元组
    """
    if config is None:
        config = make_config(slippage=slippage)

    binance_api = AsyncMock()
    db = AsyncMock()
    notification = AsyncMock()
    kline_service = AsyncMock()

    executor = TradingExecutor(
        binance_api=binance_api,
        db=db,
        notification=notification,
        config=config,
        kline_service=kline_service
    )
    return executor, binance_api, db, notification


def setup_precision_mock(binance_api, symbol='TESTUSDT', tick_size='0.01', step_size='0.001'):
    """Mock _request 方法以返回交易对精度信息

    _get_symbol_precision 方法会调用 _request("GET", "/fapi/v1/exchangeInfo", ...)
    需要返回包含 tickSize 和 stepSize 的交易对信息
    """
    async def mock_request(method, endpoint, params=None, signed=False):
        if endpoint == '/fapi/v1/exchangeInfo':
            return {
                'symbols': [{
                    'symbol': symbol,
                    'filters': [
                        {'filterType': 'PRICE_FILTER', 'tickSize': tick_size},
                        {'filterType': 'LOT_SIZE', 'stepSize': step_size},
                    ]
                }]
            }
        return {}
    binance_api._request = mock_request


def calc_stop_limit_price(stop_price, slippage, tick_size=Decimal('0.01')):
    """计算预期止损限价 = 止损价 × (1 + 滑点)，并按精度取整"""
    return (stop_price * (Decimal('1') + Decimal(str(slippage)))).quantize(tick_size)


def calc_tp_limit_price(tp_price, slippage, tick_size=Decimal('0.01')):
    """计算预期止盈限价 = 止盈价 × (1 + 滑点)，并按精度取整"""
    return (tp_price * (Decimal('1') + Decimal(str(slippage)))).quantize(tick_size)


def calc_close_limit_price(current_price, slippage, tick_size=Decimal('0.01')):
    """计算预期平仓限价 = 当前价 × (1 + 滑点)，并按精度取整"""
    return (current_price * (Decimal('1') + Decimal(str(slippage)))).quantize(tick_size)


# =============================================================================
# 1. 配置读取测试
# =============================================================================

class TestConfigReading(unittest.IsolatedAsyncioTestCase):
    """配置读取测试：验证 TradingExecutor 能正确读取 limit_order_slippage"""

    def test_slippage_default_value(self):
        """测试默认滑点值 0.001（配置中未设置时）"""
        config = make_config()
        del config['trading']['limit_order_slippage']  # 删除配置，测试默认值
        executor, _, _, _ = make_executor(config=config)
        self.assertEqual(
            executor.limit_order_slippage,
            Decimal('0.001'),
            "默认滑点值应为 0.001"
        )

    def test_slippage_custom_value(self):
        """测试自定义滑点值 0.005"""
        executor, _, _, _ = make_executor(slippage=0.005)
        self.assertEqual(
            executor.limit_order_slippage,
            Decimal('0.005'),
            "自定义滑点值应为 0.005"
        )

    def test_slippage_from_config(self):
        """测试从配置文件读取滑点值 0.002"""
        config = make_config(slippage=0.002)
        executor, _, _, _ = make_executor(config=config)
        self.assertEqual(
            executor.limit_order_slippage,
            Decimal('0.002'),
            "配置读取的滑点值应为 0.002"
        )

    def test_slippage_is_decimal_type(self):
        """测试滑点值类型为 Decimal"""
        executor, _, _, _ = make_executor(slippage=0.001)
        self.assertIsInstance(
            executor.limit_order_slippage,
            Decimal,
            "滑点值类型应为 Decimal"
        )


# =============================================================================
# 2. 限价计算逻辑测试
# =============================================================================

class TestLimitPriceCalculation(unittest.IsolatedAsyncioTestCase):
    """限价计算逻辑测试：验证止损/止盈/平仓的限价计算公式"""

    async def test_stop_loss_limit_calculation(self):
        """止损限价 = 止损价 × (1 + 0.001)

        入场价 100，止损幅度 5%，紧急止损 1.5%
        止损价 = max(100×1.05, 100×1.015) = 105
        止损限价 = 105 × 1.001 = 105.105 → 105.10
        """
        executor, binance_api, _, _ = make_executor(slippage=0.001)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        quantity = Decimal('10')
        entry_price = Decimal('100')
        tick_size = Decimal('0.01')

        await executor._set_stop_loss_take_profit(symbol, quantity, entry_price, tick_size)

        self.assertEqual(binance_api.place_conditional_order.call_count, 2)

        # 第一次调用是止损单
        stop_call = binance_api.place_conditional_order.call_args_list[0]
        expected_stop_price = Decimal('105.00')
        expected_stop_limit = calc_stop_limit_price(expected_stop_price, 0.001)

        self.assertEqual(stop_call.kwargs['order_type'], 'STOP')
        self.assertEqual(stop_call.kwargs['stop_price'], expected_stop_price)
        self.assertEqual(
            stop_call.kwargs['price'],
            expected_stop_limit,
            f"止损限价应为 {expected_stop_limit}，实际 {stop_call.kwargs['price']}"
        )

    async def test_take_profit_limit_calculation(self):
        """止盈限价 = 止盈价 × (1 + 0.001)

        入场价 100，止盈幅度 10%
        止盈价 = 100 × 0.90 = 90
        止盈限价 = 90 × 1.001 = 90.09
        """
        executor, binance_api, _, _ = make_executor(slippage=0.001)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        quantity = Decimal('10')
        entry_price = Decimal('100')
        tick_size = Decimal('0.01')

        await executor._set_stop_loss_take_profit(symbol, quantity, entry_price, tick_size)

        # 第二次调用是止盈单
        tp_call = binance_api.place_conditional_order.call_args_list[1]
        expected_tp_price = Decimal('90.00')
        expected_tp_limit = calc_tp_limit_price(expected_tp_price, 0.001)

        self.assertEqual(tp_call.kwargs['order_type'], 'TAKE_PROFIT')
        self.assertEqual(tp_call.kwargs['stop_price'], expected_tp_price)
        self.assertEqual(
            tp_call.kwargs['price'],
            expected_tp_limit,
            f"止盈限价应为 {expected_tp_limit}，实际 {tp_call.kwargs['price']}"
        )

    async def test_close_position_limit_calculation(self):
        """平仓限价 = 当前价 × (1 + 0.001)

        当前价 100
        平仓限价 = 100 × 1.001 = 100.10
        """
        executor, binance_api, _, _ = make_executor(slippage=0.001)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        current_price = Decimal('100')

        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        binance_api.get_ticker_price = AsyncMock(return_value=current_price)

        await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        binance_api.place_order.assert_called_once()
        call_kwargs = binance_api.place_order.call_args.kwargs

        expected_limit = calc_close_limit_price(current_price, 0.001)
        self.assertEqual(call_kwargs['order_type'], 'LIMIT')
        self.assertEqual(
            call_kwargs['price'],
            expected_limit,
            f"平仓限价应为 {expected_limit}，实际 {call_kwargs['price']}"
        )

    async def test_batch_take_profit_stop_limit_calculation(self):
        """分批止盈止损限价计算：止损限价 = 止损价 × (1 + 滑点)

        入场价 100，ATR 2
        ATR止损价 = 100 + 2×2.5 = 105
        最小止损价 = 100 × 1.05 = 105
        紧急止损价 = 100 × 1.015 = 101.5
        最终止损价 = max(105, 101.5, 105) = 105
        止损限价 = 105 × 1.001 = 105.10
        """
        executor, binance_api, _, _ = make_executor(slippage=0.001)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        total_quantity = Decimal('10')
        entry_price = Decimal('100')
        atr = Decimal('2')
        tick_size = Decimal('0.01')
        step_size = Decimal('0.001')

        executor.position_tracking[symbol] = {}

        await executor._set_batch_take_profit(
            symbol, total_quantity, entry_price, atr, tick_size, step_size
        )

        # 第一次调用是止损单
        stop_call = binance_api.place_conditional_order.call_args_list[0]
        expected_stop_price = Decimal('105.00')
        expected_stop_limit = calc_stop_limit_price(expected_stop_price, 0.001)

        self.assertEqual(stop_call.kwargs['order_type'], 'STOP')
        self.assertEqual(
            stop_call.kwargs['price'],
            expected_stop_limit,
            f"分批止损限价应为 {expected_stop_limit}，实际 {stop_call.kwargs['price']}"
        )

    async def test_batch_take_profit_target_limit_calculation(self):
        """分批止盈目标限价计算：止盈限价 = 止盈价 × (1 + 滑点)

        入场价 100，ATR 2
        目标1价 = 100 - 2×1.5 = 97，限价 = 97 × 1.001 = 97.097 → 97.10
        目标2价 = 100 - 2×3.5 = 93，限价 = 93 × 1.001 = 93.093 → 93.09
        """
        executor, binance_api, _, _ = make_executor(slippage=0.001)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        total_quantity = Decimal('10')
        entry_price = Decimal('100')
        atr = Decimal('2')
        tick_size = Decimal('0.01')
        step_size = Decimal('0.001')

        executor.position_tracking[symbol] = {}

        await executor._set_batch_take_profit(
            symbol, total_quantity, entry_price, atr, tick_size, step_size
        )

        # 第二次调用是目标1止盈
        tp1_call = binance_api.place_conditional_order.call_args_list[1]
        expected_tp1_price = Decimal('97.00')
        expected_tp1_limit = calc_tp_limit_price(expected_tp1_price, 0.001)

        self.assertEqual(tp1_call.kwargs['order_type'], 'TAKE_PROFIT')
        self.assertEqual(
            tp1_call.kwargs['price'],
            expected_tp1_limit,
            f"目标1止盈限价应为 {expected_tp1_limit}，实际 {tp1_call.kwargs['price']}"
        )

        # 第三次调用是目标2止盈
        tp2_call = binance_api.place_conditional_order.call_args_list[2]
        expected_tp2_price = Decimal('93.00')
        expected_tp2_limit = calc_tp_limit_price(expected_tp2_price, 0.001)

        self.assertEqual(tp2_call.kwargs['order_type'], 'TAKE_PROFIT')
        self.assertEqual(
            tp2_call.kwargs['price'],
            expected_tp2_limit,
            f"目标2止盈限价应为 {expected_tp2_limit}，实际 {tp2_call.kwargs['price']}"
        )

    async def test_breakeven_stop_limit_calculation(self):
        """保本止损限价计算：止损限价 = 保本价 × (1 + 滑点)

        保本价 = 入场价 = 100
        止损限价 = 100 × 1.001 = 100.10
        """
        executor, binance_api, _, _ = make_executor(slippage=0.001)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        executor.position_tracking[symbol] = {
            'entry_price': 100,
            'remaining_quantity': 10,
        }

        binance_api.get_open_orders = AsyncMock(return_value=[
            {'type': 'STOP', 'orderId': '999'}
        ])
        binance_api.cancel_order = AsyncMock(return_value={})

        await executor._adjust_stop_to_breakeven(symbol)

        binance_api.place_conditional_order.assert_called_once()
        call_kwargs = binance_api.place_conditional_order.call_args.kwargs

        expected_stop_price = Decimal('100.00')
        expected_stop_limit = calc_stop_limit_price(expected_stop_price, 0.001)

        self.assertEqual(call_kwargs['order_type'], 'STOP')
        self.assertEqual(call_kwargs['stop_price'], expected_stop_price)
        self.assertEqual(
            call_kwargs['price'],
            expected_stop_limit,
            f"保本止损限价应为 {expected_stop_limit}，实际 {call_kwargs['price']}"
        )


# =============================================================================
# 3. 订单类型测试
# =============================================================================

class TestOrderTypes(unittest.IsolatedAsyncioTestCase):
    """订单类型测试：验证各方法使用正确的订单类型"""

    async def test_place_short_order_uses_limit(self):
        """_place_short_order 使用 LIMIT 类型并传入 price 参数"""
        executor, binance_api, _, _ = make_executor()

        symbol = 'TESTUSDT'
        quantity = Decimal('10')
        price = Decimal('100')

        binance_api.place_order = AsyncMock(return_value={'orderId': '123', 'status': 'NEW'})

        result = await executor._place_short_order(symbol, quantity, price)

        self.assertIsNotNone(result, "开空仓应返回订单信息")
        binance_api.place_order.assert_called_once()

        call_kwargs = binance_api.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['order_type'], 'LIMIT', "开仓订单类型应为 LIMIT")
        self.assertEqual(call_kwargs['price'], price, "应传入限价 price 参数")
        self.assertEqual(call_kwargs['side'], 'SELL', "做空方向应为 SELL")
        self.assertEqual(call_kwargs['quantity'], quantity, "应传入数量参数")
        self.assertEqual(call_kwargs['symbol'], symbol, "应传入交易对参数")

    async def test_set_stop_loss_take_profit_uses_stop_and_take_profit(self):
        """_set_stop_loss_take_profit 使用 STOP 和 TAKE_PROFIT 类型"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        quantity = Decimal('10')
        entry_price = Decimal('100')
        tick_size = Decimal('0.01')

        await executor._set_stop_loss_take_profit(symbol, quantity, entry_price, tick_size)

        self.assertEqual(
            binance_api.place_conditional_order.call_count, 2,
            "应调用 place_conditional_order 两次（止损+止盈）"
        )

        # 止损单使用 STOP
        stop_call = binance_api.place_conditional_order.call_args_list[0]
        self.assertEqual(stop_call.kwargs['order_type'], 'STOP', "止损单类型应为 STOP")
        self.assertIn('price', stop_call.kwargs, "止损单应包含限价 price 参数")
        self.assertEqual(stop_call.kwargs['side'], 'BUY', "做空止损方向应为 BUY")

        # 止盈单使用 TAKE_PROFIT
        tp_call = binance_api.place_conditional_order.call_args_list[1]
        self.assertEqual(tp_call.kwargs['order_type'], 'TAKE_PROFIT', "止盈单类型应为 TAKE_PROFIT")
        self.assertIn('price', tp_call.kwargs, "止盈单应包含限价 price 参数")
        self.assertEqual(tp_call.kwargs['side'], 'BUY', "做空止盈方向应为 BUY")

    async def test_set_batch_take_profit_uses_stop_and_take_profit(self):
        """_set_batch_take_profit 使用 STOP 和 TAKE_PROFIT 类型"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        total_quantity = Decimal('10')
        entry_price = Decimal('100')
        atr = Decimal('2')
        tick_size = Decimal('0.01')
        step_size = Decimal('0.001')

        executor.position_tracking[symbol] = {}

        await executor._set_batch_take_profit(
            symbol, total_quantity, entry_price, atr, tick_size, step_size
        )

        # 应调用 3 次：1个止损 + 2个止盈
        self.assertEqual(
            binance_api.place_conditional_order.call_count, 3,
            "应调用 place_conditional_order 三次（1止损+2止盈）"
        )

        # 止损单使用 STOP
        stop_call = binance_api.place_conditional_order.call_args_list[0]
        self.assertEqual(stop_call.kwargs['order_type'], 'STOP', "分批止损单类型应为 STOP")
        self.assertIn('price', stop_call.kwargs, "分批止损单应包含限价 price 参数")

        # 第一目标止盈使用 TAKE_PROFIT
        tp1_call = binance_api.place_conditional_order.call_args_list[1]
        self.assertEqual(tp1_call.kwargs['order_type'], 'TAKE_PROFIT', "第一目标止盈类型应为 TAKE_PROFIT")
        self.assertIn('price', tp1_call.kwargs, "第一目标止盈应包含限价 price 参数")

        # 第二目标止盈使用 TAKE_PROFIT
        tp2_call = binance_api.place_conditional_order.call_args_list[2]
        self.assertEqual(tp2_call.kwargs['order_type'], 'TAKE_PROFIT', "第二目标止盈类型应为 TAKE_PROFIT")
        self.assertIn('price', tp2_call.kwargs, "第二目标止盈应包含限价 price 参数")

    async def test_adjust_stop_to_breakeven_uses_stop(self):
        """_adjust_stop_to_breakeven 使用 STOP 类型"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        executor.position_tracking[symbol] = {
            'entry_price': 100,
            'remaining_quantity': 10,
        }

        binance_api.get_open_orders = AsyncMock(return_value=[
            {'type': 'STOP', 'orderId': '999'}
        ])
        binance_api.cancel_order = AsyncMock(return_value={})

        await executor._adjust_stop_to_breakeven(symbol)

        # 验证取消了旧止损单
        binance_api.cancel_order.assert_called_once()

        # 验证下了新止损单，类型为 STOP
        binance_api.place_conditional_order.assert_called_once()
        call_kwargs = binance_api.place_conditional_order.call_args.kwargs
        self.assertEqual(call_kwargs['order_type'], 'STOP', "保本止损单类型应为 STOP")
        self.assertIn('price', call_kwargs, "保本止损单应包含限价 price 参数")

    async def test_adjust_stop_to_breakeven_cancel_checks_stop_type(self):
        """_adjust_stop_to_breakeven 取消订单时检查 STOP 类型（非 STOP_MARKET）"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        executor.position_tracking[symbol] = {
            'entry_price': 100,
            'remaining_quantity': 10,
        }

        # 返回多种类型订单，只有 STOP 应被取消
        binance_api.get_open_orders = AsyncMock(return_value=[
            {'type': 'STOP', 'orderId': '999'},
            {'type': 'TAKE_PROFIT', 'orderId': '888'},
            {'type': 'LIMIT', 'orderId': '777'},
            {'type': 'STOP_MARKET', 'orderId': '666'},  # 旧类型，不应被取消
        ])
        binance_api.cancel_order = AsyncMock(return_value={})

        await executor._adjust_stop_to_breakeven(symbol)

        # 只应取消 1 次（STOP 类型，orderId=999）
        self.assertEqual(
            binance_api.cancel_order.call_count, 1,
            "只应取消 STOP 类型的订单，不应取消 STOP_MARKET 等其他类型"
        )
        cancel_call = binance_api.cancel_order.call_args
        self.assertEqual(cancel_call.args[1], '999', "应取消 orderId=999 的 STOP 订单")

    async def test_close_position_uses_limit(self):
        """_close_position 使用 LIMIT 类型"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        binance_api.get_ticker_price = AsyncMock(return_value=Decimal('100'))

        await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        binance_api.place_order.assert_called_once()
        call_kwargs = binance_api.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['order_type'], 'LIMIT', "平仓订单类型应为 LIMIT")
        self.assertIn('price', call_kwargs, "平仓订单应包含限价 price 参数")
        self.assertEqual(call_kwargs['side'], 'BUY', "做空平仓方向应为 BUY")


# =============================================================================
# 4. 平仓容错测试
# =============================================================================

class TestClosePositionFallback(unittest.IsolatedAsyncioTestCase):
    """平仓容错测试：验证限价单失败时回退到市价单"""

    async def test_fallback_to_market_on_limit_exception(self):
        """限价单抛出异常时，回退到 MARKET 市价单"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        binance_api.get_ticker_price = AsyncMock(return_value=Decimal('100'))

        # 第一次（限价单）抛出异常，第二次（市价单）成功
        binance_api.place_order = AsyncMock(side_effect=[
            Exception('限价单下单失败'),
            {'orderId': '456', 'status': 'FILLED'}
        ])

        result = await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        self.assertTrue(result, "平仓应成功（市价单回退成功）")
        self.assertEqual(
            binance_api.place_order.call_count, 2,
            "应调用 place_order 两次（限价失败+市价回退）"
        )

        # 第一次是 LIMIT
        first_call = binance_api.place_order.call_args_list[0]
        self.assertEqual(first_call.kwargs['order_type'], 'LIMIT', "第一次应为 LIMIT 限价单")

        # 第二次是 MARKET
        second_call = binance_api.place_order.call_args_list[1]
        self.assertEqual(second_call.kwargs['order_type'], 'MARKET', "第二次应为 MARKET 市价单（回退）")

    async def test_fallback_to_market_on_price_zero(self):
        """获取价格返回 0 时，直接使用 MARKET 市价单"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        # 获取价格返回 0（异常）
        binance_api.get_ticker_price = AsyncMock(return_value=Decimal('0'))
        # 市价单成功
        binance_api.place_order = AsyncMock(return_value={'orderId': '456', 'status': 'FILLED'})

        result = await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        self.assertTrue(result, "平仓应成功（市价单回退成功）")
        # 应该只调用 1 次（市价单），因为价格异常直接进入 except 块
        self.assertEqual(
            binance_api.place_order.call_count, 1,
            "价格异常时应只调用 1 次市价单"
        )

        call_kwargs = binance_api.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['order_type'], 'MARKET', "价格异常时应使用 MARKET 市价单")

    async def test_fallback_to_market_on_price_exception(self):
        """获取价格抛出异常时，直接使用 MARKET 市价单"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        # 获取价格抛出异常
        binance_api.get_ticker_price = AsyncMock(side_effect=Exception('网络错误'))
        # 市价单成功
        binance_api.place_order = AsyncMock(return_value={'orderId': '456', 'status': 'FILLED'})

        result = await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        self.assertTrue(result, "平仓应成功（市价单回退成功）")
        self.assertEqual(
            binance_api.place_order.call_count, 1,
            "价格异常时应只调用 1 次市价单"
        )

        call_kwargs = binance_api.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['order_type'], 'MARKET', "价格异常时应使用 MARKET 市价单")

    async def test_fallback_to_market_on_negative_price(self):
        """获取价格返回负数时，直接使用 MARKET 市价单"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        # 获取价格返回负数（异常）
        binance_api.get_ticker_price = AsyncMock(return_value=Decimal('-1'))
        # 市价单成功
        binance_api.place_order = AsyncMock(return_value={'orderId': '456', 'status': 'FILLED'})

        result = await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        self.assertTrue(result, "平仓应成功（市价单回退成功）")
        self.assertEqual(binance_api.place_order.call_count, 1)

        call_kwargs = binance_api.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['order_type'], 'MARKET', "价格异常时应使用 MARKET 市价单")

    async def test_close_position_returns_false_when_no_position(self):
        """没有做空持仓时返回 False"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        # 没有做空持仓
        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'LONG', 'positionAmt': '10'}
        ])

        result = await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        self.assertFalse(result, "没有做空持仓时应返回 False")
        binance_api.place_order.assert_not_called()


# =============================================================================
# 5. 边界条件测试
# =============================================================================

class TestBoundaryConditions(unittest.IsolatedAsyncioTestCase):
    """边界条件测试：验证滑点为0、较大值、价格异常等情况"""

    async def test_slippage_zero_stop_loss(self):
        """滑点为 0 时的止损限价计算：限价 = 止损价 × (1 + 0) = 止损价"""
        executor, binance_api, _, _ = make_executor(slippage=0)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        quantity = Decimal('10')
        entry_price = Decimal('100')
        tick_size = Decimal('0.01')

        await executor._set_stop_loss_take_profit(symbol, quantity, entry_price, tick_size)

        stop_call = binance_api.place_conditional_order.call_args_list[0]
        expected_stop_price = Decimal('105.00')
        expected_stop_limit = calc_stop_limit_price(expected_stop_price, 0)

        self.assertEqual(
            stop_call.kwargs['price'],
            expected_stop_limit,
            f"滑点为0时止损限价应等于止损价 {expected_stop_limit}"
        )

    async def test_slippage_zero_take_profit(self):
        """滑点为 0 时的止盈限价计算：限价 = 止盈价 × (1 + 0) = 止盈价"""
        executor, binance_api, _, _ = make_executor(slippage=0)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        quantity = Decimal('10')
        entry_price = Decimal('100')
        tick_size = Decimal('0.01')

        await executor._set_stop_loss_take_profit(symbol, quantity, entry_price, tick_size)

        tp_call = binance_api.place_conditional_order.call_args_list[1]
        expected_tp_price = Decimal('90.00')
        expected_tp_limit = calc_tp_limit_price(expected_tp_price, 0)

        self.assertEqual(
            tp_call.kwargs['price'],
            expected_tp_limit,
            f"滑点为0时止盈限价应等于止盈价 {expected_tp_limit}"
        )

    async def test_slippage_zero_close_position(self):
        """滑点为 0 时的平仓限价计算：限价 = 当前价 × (1 + 0) = 当前价"""
        executor, binance_api, _, _ = make_executor(slippage=0)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        current_price = Decimal('100')

        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        binance_api.get_ticker_price = AsyncMock(return_value=current_price)

        await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        call_kwargs = binance_api.place_order.call_args.kwargs
        expected_limit = calc_close_limit_price(current_price, 0)
        self.assertEqual(
            call_kwargs['price'],
            expected_limit,
            f"滑点为0时平仓限价应等于当前价 {expected_limit}"
        )

    async def test_slippage_large_stop_loss(self):
        """滑点为 0.005（0.5%）时的止损限价计算"""
        executor, binance_api, _, _ = make_executor(slippage=0.005)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        quantity = Decimal('10')
        entry_price = Decimal('100')
        tick_size = Decimal('0.01')

        await executor._set_stop_loss_take_profit(symbol, quantity, entry_price, tick_size)

        stop_call = binance_api.place_conditional_order.call_args_list[0]
        expected_stop_price = Decimal('105.00')
        # 止损限价 = 105 × 1.005 = 105.525 → 105.52（ROUND_HALF_EVEN）
        expected_stop_limit = calc_stop_limit_price(expected_stop_price, 0.005)

        self.assertEqual(
            stop_call.kwargs['price'],
            expected_stop_limit,
            f"滑点0.005时止损限价应为 {expected_stop_limit}，实际 {stop_call.kwargs['price']}"
        )

    async def test_slippage_large_take_profit(self):
        """滑点为 0.005（0.5%）时的止盈限价计算"""
        executor, binance_api, _, _ = make_executor(slippage=0.005)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        quantity = Decimal('10')
        entry_price = Decimal('100')
        tick_size = Decimal('0.01')

        await executor._set_stop_loss_take_profit(symbol, quantity, entry_price, tick_size)

        tp_call = binance_api.place_conditional_order.call_args_list[1]
        expected_tp_price = Decimal('90.00')
        # 止盈限价 = 90 × 1.005 = 90.45
        expected_tp_limit = calc_tp_limit_price(expected_tp_price, 0.005)

        self.assertEqual(
            tp_call.kwargs['price'],
            expected_tp_limit,
            f"滑点0.005时止盈限价应为 {expected_tp_limit}，实际 {tp_call.kwargs['price']}"
        )

    async def test_slippage_large_close_position(self):
        """滑点为 0.005（0.5%）时的平仓限价计算"""
        executor, binance_api, _, _ = make_executor(slippage=0.005)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        current_price = Decimal('100')

        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        binance_api.get_ticker_price = AsyncMock(return_value=current_price)

        await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        call_kwargs = binance_api.place_order.call_args.kwargs
        # 平仓限价 = 100 × 1.005 = 100.50
        expected_limit = calc_close_limit_price(current_price, 0.005)
        self.assertEqual(
            call_kwargs['price'],
            expected_limit,
            f"滑点0.005时平仓限价应为 {expected_limit}，实际 {call_kwargs['price']}"
        )

    async def test_close_position_price_zero_triggers_fallback(self):
        """价格为 0 时触发市价单回退"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        binance_api.get_ticker_price = AsyncMock(return_value=Decimal('0'))
        binance_api.place_order = AsyncMock(return_value={'orderId': '456', 'status': 'FILLED'})

        result = await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        self.assertTrue(result, "价格为0时应回退市价单并成功")
        call_kwargs = binance_api.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['order_type'], 'MARKET', "价格为0时应使用 MARKET 市价单")

    async def test_close_position_negative_price_triggers_fallback(self):
        """价格为负数时触发市价单回退"""
        executor, binance_api, _, _ = make_executor()
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        binance_api.get_position = AsyncMock(return_value=[
            {'positionSide': 'SHORT', 'positionAmt': '-10'}
        ])
        binance_api.get_ticker_price = AsyncMock(return_value=Decimal('-100'))
        binance_api.place_order = AsyncMock(return_value={'orderId': '456', 'status': 'FILLED'})

        result = await executor._close_position(symbol, Decimal('1.0'), '测试平仓')

        self.assertTrue(result, "价格为负数时应回退市价单并成功")
        call_kwargs = binance_api.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['order_type'], 'MARKET', "价格为负数时应使用 MARKET 市价单")

    async def test_high_price_stop_loss_calculation(self):
        """高价格场景下的止损限价计算（验证大数计算无溢出）"""
        executor, binance_api, _, _ = make_executor(slippage=0.001)
        setup_precision_mock(binance_api)

        symbol = 'TESTUSDT'
        quantity = Decimal('0.001')
        entry_price = Decimal('50000')  # 高价格
        tick_size = Decimal('0.01')

        await executor._set_stop_loss_take_profit(symbol, quantity, entry_price, tick_size)

        stop_call = binance_api.place_conditional_order.call_args_list[0]
        # 止损价 = 50000 × 1.05 = 52500
        expected_stop_price = Decimal('52500.00')
        # 止损限价 = 52500 × 1.001 = 52552.50
        expected_stop_limit = calc_stop_limit_price(expected_stop_price, 0.001)

        self.assertEqual(stop_call.kwargs['stop_price'], expected_stop_price)
        self.assertEqual(
            stop_call.kwargs['price'],
            expected_stop_limit,
            f"高价格场景止损限价应为 {expected_stop_limit}"
        )

    async def test_small_price_take_profit_calculation(self):
        """小价格场景下的止盈限价计算（验证小数精度）"""
        executor, binance_api, _, _ = make_executor(slippage=0.001)
        setup_precision_mock(binance_api, tick_size='0.0001')  # 更小精度

        symbol = 'TESTUSDT'
        quantity = Decimal('1000')
        entry_price = Decimal('0.5')  # 小价格
        tick_size = Decimal('0.0001')

        await executor._set_stop_loss_take_profit(symbol, quantity, entry_price, tick_size)

        tp_call = binance_api.place_conditional_order.call_args_list[1]
        # 止盈价 = 0.5 × 0.9 = 0.45
        expected_tp_price = Decimal('0.4500')
        # 止盈限价 = 0.45 × 1.001 = 0.45045 → 0.4504（按0.0001精度）
        expected_tp_limit = calc_tp_limit_price(expected_tp_price, 0.001, tick_size)

        self.assertEqual(
            tp_call.kwargs['price'],
            expected_tp_limit,
            f"小价格场景止盈限价应为 {expected_tp_limit}"
        )


# =============================================================================
# 测试运行与报告生成
# =============================================================================

def run_tests():
    """运行所有测试并输出详细报告"""
    print()
    print('=' * 80)
    print('  限价单改造测试报告（V4.1：市价单改为限价单）')
    print('=' * 80)
    print()
    print('测试范围：')
    print('  1. 配置读取测试 - 验证 limit_order_slippage 配置读取与默认值')
    print('  2. 限价计算逻辑测试 - 验证止损/止盈/平仓限价计算公式')
    print('  3. 订单类型测试 - 验证各方法使用正确的订单类型')
    print('  4. 平仓容错测试 - 验证限价单失败时回退到市价单')
    print('  5. 边界条件测试 - 验证滑点为0、较大值、价格异常等情况')
    print()
    print('-' * 80)
    print('测试用例执行详情：')
    print('-' * 80)
    print()

    # 创建测试套件
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 添加所有测试类
    test_classes = [
        TestConfigReading,
        TestLimitPriceCalculation,
        TestOrderTypes,
        TestClosePositionFallback,
        TestBoundaryConditions,
    ]

    for test_class in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(test_class))

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)

    print()
    print('=' * 80)
    print('  测试总结')
    print('=' * 80)
    total = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    failed = len(result.failures)
    errors = len(result.errors)
    print(f'  测试用例总数: {total}')
    print(f'  通过数量:     {passed}')
    print(f'  失败数量:     {failed}')
    print(f'  错误数量:     {errors}')
    print(f'  通过率:       {passed}/{total} = {(passed/total*100):.1f}%' if total > 0 else '  通过率: N/A')
    print()

    if result.failures:
        print('-' * 80)
        print('失败用例详情：')
        print('-' * 80)
        for i, (test, traceback) in enumerate(result.failures, 1):
            print(f'{i}. {test}')
            # 只输出最后几行关键信息
            tb_lines = traceback.strip().split('\n')
            for line in tb_lines[-5:]:
                print(f'   {line}')
            print()

    if result.errors:
        print('-' * 80)
        print('错误用例详情：')
        print('-' * 80)
        for i, (test, traceback) in enumerate(result.errors, 1):
            print(f'{i}. {test}')
            tb_lines = traceback.strip().split('\n')
            for line in tb_lines[-5:]:
                print(f'   {line}')
            print()

    print('=' * 80)
    if result.wasSuccessful():
        print('  测试结论: 全部通过，限价单改造符合预期')
    else:
        print('  测试结论: 存在失败/错误用例，需要修复')
    print('=' * 80)
    print()

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
