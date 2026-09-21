"""
MTPCS 激进版（btc_eth_aggressive）ReduceOnly 修复功能测试

针对修复（对齐原版 btc_eth 防双重平仓机制）的核心分支验证：

A. _cancel_symbol_conditional_orders（L3142）：
   1. cancel_all_algo_orders 正常返回 → True
   2. 抛 BinanceAPIError(-4046) 无挂单 → True（静默跳过）
   3. 抛 BinanceAPIError(其他 code) → False（不阻断主流程）
   4. 抛普通 Exception → False

B. _close_position 入口（L3216）：
   5. 入口同步 closed=True → 直接返回 True，不下单、不取消条件单

C. _close_position except 三分支（L3406，平仓单抛 -4118 / -2022）：
   6. -4118 → 取消条件单 → 同步 closed=True → 分支触发（filled=True + break）
   7. -2022 → 同上
   8. 同步 partially_closed → 缩量后重试成功
   9. 同步 partially_closed 但缩量后为 0 → break 返回 False
   10. 同步后持仓未变 → break 返回 False（避免无限重试）

注意（实测发现的真实缺陷）：
  用例 6/7 的 closed 分支中，place_order 抛异常后 order_result 仍为 None，
  break 后执行 order_result.get('orderId') 抛 AttributeError，被 _close_position
  外层 except 捕获，最终返回 False 并误发"平仓失败"错误通知。
  测试如实断言该真实行为并标注为缺陷，供修复参考。
"""
import sys
import os
from decimal import Decimal
from unittest.mock import ANY, AsyncMock

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import yaml
import pytest
from shared.binance_api import BinanceAPIError
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy, PositionState


def load_config() -> dict:
    """加载激进版策略配置文件"""
    config_path = os.path.join(PROJECT_ROOT, "strategies", "btc_eth_aggressive", "config.yaml")
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def make_position(direction='LONG', quantity=Decimal('0.1')):
    """构造测试持仓状态（默认做多 0.1 BTC）"""
    pos = PositionState()
    pos.direction = direction
    pos.current_quantity = quantity
    pos.entry_price = Decimal('60000')
    return pos


def make_get_position_side_effect(*results):
    """按调用顺序返回持仓结果（入口同步一次、except 内同步一次）"""
    it = iter(results)

    async def _get_position(symbol):
        return next(it)

    return _get_position


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def strategy(config):
    """构造策略实例并注入测试 mock（不触碰真实交易所/数据库）"""
    mock_binance = AsyncMock()
    s = BTCEthStrategy(
        config=config,
        binance_client=mock_binance,
        kline_service=AsyncMock(),
        notification_client=AsyncMock(),
        db_manager=None,
    )
    # 屏蔽与本次修复无关的辅助方法
    s._mark_stop_loss_if_needed = AsyncMock()
    s._cleanup_position_orders = AsyncMock()
    # 预置精度缓存，避免真实查询交易所
    s.symbol_precision['BTCUSDT'] = {
        'stepSize': '0.001',
        'tickSize': Decimal('0.01'),
        'quantityPrecision': 3,
        'pricePrecision': 2,
    }
    # 订单簿与轮询：默认最优价 60000、订单已成交
    s.binance.get_orderbook = AsyncMock(return_value={
        'bids': [['60000.0', '1.0']],
        'asks': [['60000.0', '1.0']],
    })
    s.binance.get_open_orders = AsyncMock(return_value=[])
    # 取消条件单默认成功
    s.binance.cancel_all_algo_orders = AsyncMock(return_value={'code': 200})
    return s


# ============================================================================
# A. _cancel_symbol_conditional_orders（用例 1-4）
# ============================================================================

class TestCancelSymbolConditionalOrders:
    """_cancel_symbol_conditional_orders 的 4 个分支"""

    async def test_normal_success_returns_true(self, strategy):
        """用例1：cancel_all_algo_orders 正常返回 → True"""
        result = await strategy._cancel_symbol_conditional_orders('BTCUSDT', 'TP1')
        assert result is True
        strategy.binance.cancel_all_algo_orders.assert_awaited_once_with('BTCUSDT')

    async def test_4046_no_pending_returns_true(self, strategy):
        """用例2：抛 BinanceAPIError(-4046) 无挂单 → True（静默跳过）"""
        strategy.binance.cancel_all_algo_orders = AsyncMock(
            side_effect=BinanceAPIError(-4046, 'no existing algo order'))
        result = await strategy._cancel_symbol_conditional_orders('BTCUSDT', 'TP1')
        assert result is True

    async def test_other_api_error_returns_false(self, strategy):
        """用例3：抛 BinanceAPIError(其他 code) → False（不阻断主流程）"""
        strategy.binance.cancel_all_algo_orders = AsyncMock(
            side_effect=BinanceAPIError(-2011, 'unknown order'))
        result = await strategy._cancel_symbol_conditional_orders('BTCUSDT', 'TP1')
        assert result is False

    async def test_generic_exception_returns_false(self, strategy):
        """用例4：抛普通 Exception → False"""
        strategy.binance.cancel_all_algo_orders = AsyncMock(
            side_effect=RuntimeError('connection lost'))
        result = await strategy._cancel_symbol_conditional_orders('BTCUSDT', 'TP1')
        assert result is False


# ============================================================================
# B. _close_position 入口（用例 5）
# ============================================================================

class TestClosePositionEntrySyncClosed:
    """_close_position 入口同步 closed=True 分支"""

    async def test_entry_sync_closed_returns_true_without_order(self, strategy):
        """用例5：入口同步发现交易所无持仓 → 返回 True，且不下单、不取消条件单"""
        strategy.binance.get_position = make_get_position_side_effect([])
        pos = make_position()
        result = await strategy._close_position(
            'BTCUSDT', pos, Decimal('0.1'), 'TP1', Decimal('60000'))
        assert result is True
        strategy.binance.place_order.assert_not_awaited()
        strategy.binance.cancel_all_algo_orders.assert_not_awaited()
        # 本地持仓已同步为 0（防双重平仓）
        assert pos.current_quantity == Decimal('0')
        # 条件单已平仓：用当前价估算盈亏并回写，止损打标透传估算的 pnl
        strategy._mark_stop_loss_if_needed.assert_awaited_once_with(
            'BTCUSDT', pos, 'TP1', Decimal('0.0'))


# ============================================================================
# C. _close_position except 三分支（用例 6-10）
# ============================================================================

class TestClosePositionReduceOnlyExcept:
    """平仓单抛 BinanceAPIError(-2022 / -4118) 时的三分支"""

    async def test_4118_sync_closed_branch(self, strategy):
        """用例6：-4118 → 取消条件单 → 同步 closed=True → 返回 True（视为成功）

        修复后（L3419-3429）：closed 分支直接调用 _mark_stop_loss_if_needed
        并 return True，不再因 order_result 为空触发 AttributeError。
        """
        strategy.binance.place_order = AsyncMock(
            side_effect=BinanceAPIError(-4118, 'ReduceOnly Order Failed'))
        strategy.binance.get_position = make_get_position_side_effect(
            [{'symbol': 'BTCUSDT', 'positionAmt': '0.1'}],  # 入口同步：有持仓
            [],                                              # except 内同步：已平仓
        )
        pos = make_position()
        result = await strategy._close_position(
            'BTCUSDT', pos, Decimal('0.1'), 'TP1', Decimal('60000'))

        assert result is True
        # 下单前(L3218)与 except 内(L3416)各取消一次条件单
        assert strategy.binance.cancel_all_algo_orders.await_count == 2
        strategy.binance.cancel_all_algo_orders.assert_awaited_with('BTCUSDT')
        # 止损打标被调用一次（与入口 closed 分支一致），透传估算的 pnl
        strategy._mark_stop_loss_if_needed.assert_awaited_once_with(
            'BTCUSDT', pos, 'TP1', Decimal('0.0'))
        # 不应再触发"平仓失败"错误通知（原缺陷：order_result 空引用）
        assert strategy.notification.send_error_notification.await_count == 0

    async def test_2022_sync_closed_branch(self, strategy):
        """用例7：-2022 → 取消条件单 → 同步 closed=True → 返回 True（视为成功）"""
        strategy.binance.place_order = AsyncMock(
            side_effect=BinanceAPIError(-2022, 'ReduceOnly Order is rejected'))
        strategy.binance.get_position = make_get_position_side_effect(
            [{'symbol': 'BTCUSDT', 'positionAmt': '0.1'}],
            [],
        )
        pos = make_position()
        result = await strategy._close_position(
            'BTCUSDT', pos, Decimal('0.1'), 'TP1', Decimal('60000'))

        assert result is True
        assert strategy.binance.cancel_all_algo_orders.await_count == 2
        strategy._mark_stop_loss_if_needed.assert_awaited_once_with(
            'BTCUSDT', pos, 'TP1', Decimal('0.0'))
        assert strategy.notification.send_error_notification.await_count == 0

    async def test_closed_take_profit_fallback_insert_pnl_summary(self, strategy):
        """用例11：closed 分支 update_realized_pnl 未命中 + 止盈原因 → insert_pnl_summary 兜底"""
        # 注入受控 trade_logger：模式二降级匹配未命中，触发 PnL 汇总兜底
        mock_trade_logger = AsyncMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=False)
        mock_trade_logger.insert_pnl_summary = AsyncMock()
        mock_trade_logger.strategy_name = "测试策略"
        strategy.binance.trade_logger = mock_trade_logger

        strategy.binance.place_order = AsyncMock(
            side_effect=BinanceAPIError(-2022, 'ReduceOnly Order is rejected'))
        strategy.binance.get_position = make_get_position_side_effect(
            [{'symbol': 'BTCUSDT', 'positionAmt': '0.1'}],
            [],
        )
        pos = make_position()
        result = await strategy._close_position(
            'BTCUSDT', pos, Decimal('0.1'), 'TP1', Decimal('60000'))

        assert result is True
        # 模式二降级匹配未命中
        mock_trade_logger.update_realized_pnl.assert_awaited_once()
        # 止盈类原因（TP1）触发 PnL 汇总兜底插入
        mock_trade_logger.insert_pnl_summary.assert_awaited_once_with(
            realized_pnl=Decimal('0.0'),
            symbol='BTCUSDT',
            side='SELL',
            strategy='测试策略',
            executed_at=ANY,
            close_reason='TP1',
        )
        # 止损打标：closed 分支透传估算 pnl
        strategy._mark_stop_loss_if_needed.assert_awaited_once_with(
            'BTCUSDT', pos, 'TP1', Decimal('0.0'))

    async def test_partially_closed_retry_success(self, strategy):
        """用例8：-4118 → 同步 partially_closed(0.05) → 缩量 0.05 后重试成功 → True"""
        call_count = {'n': 0}

        async def place_order_with_retry(**kwargs):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise BinanceAPIError(-4118, 'ReduceOnly Order Failed')
            return {'orderId': 888, 'avgPrice': '60000'}

        strategy.binance.place_order = AsyncMock(side_effect=place_order_with_retry)
        strategy.binance.get_position = make_get_position_side_effect(
            [{'symbol': 'BTCUSDT', 'positionAmt': '0.1'}],
            [{'symbol': 'BTCUSDT', 'positionAmt': '0.05'}],  # 部分平仓
        )
        pos = make_position()
        result = await strategy._close_position(
            'BTCUSDT', pos, Decimal('0.1'), 'TP1', Decimal('60000'))

        assert result is True
        # 两次下单：第一次 0.1 被拒，缩量后第二次 0.05 成交
        assert call_count['n'] == 2
        first_qty = strategy.binance.place_order.await_args_list[0].kwargs['quantity']
        second_qty = strategy.binance.place_order.await_args_list[1].kwargs['quantity']
        assert first_qty == Decimal('0.1')
        assert second_qty == Decimal('0.05')
        # 缩量后剩余持仓 0.05 全平 → 本地归零
        assert pos.current_quantity == Decimal('0')

    async def test_partially_closed_adjust_to_zero_break(self, strategy):
        """用例9：-4118 → partially_closed 但缩量后为 0（0.0004 < step 0.001）→ break → False"""
        strategy.binance.place_order = AsyncMock(
            side_effect=BinanceAPIError(-4118, 'ReduceOnly Order Failed'))
        strategy.binance.get_position = make_get_position_side_effect(
            [{'symbol': 'BTCUSDT', 'positionAmt': '0.1'}],
            [{'symbol': 'BTCUSDT', 'positionAmt': '0.0004'}],  # 缩量后 (0.0004//0.001)*0.001=0
        )
        pos = make_position()
        result = await strategy._close_position(
            'BTCUSDT', pos, Decimal('0.1'), 'TP1', Decimal('60000'))

        assert result is False
        # 首次被拒后即 break，不再重试
        assert strategy.binance.place_order.await_count == 1

    async def test_sync_unchanged_break(self, strategy):
        """用例10：-2022 → 同步后持仓未变 → break 返回 False（避免无限重试）"""
        strategy.binance.place_order = AsyncMock(
            side_effect=BinanceAPIError(-2022, 'ReduceOnly Order is rejected'))
        strategy.binance.get_position = make_get_position_side_effect(
            [{'symbol': 'BTCUSDT', 'positionAmt': '0.1'}],
            [{'symbol': 'BTCUSDT', 'positionAmt': '0.1'}],  # 持仓未变
        )
        pos = make_position()
        result = await strategy._close_position(
            'BTCUSDT', pos, Decimal('0.1'), 'TP1', Decimal('60000'))

        assert result is False
        # 下单前(L3218)与 except 内(L3416)各取消一次条件单
        assert strategy.binance.cancel_all_algo_orders.await_count == 2
        # 同步未变立即 break，未继续重试
        assert strategy.binance.place_order.await_count == 1
        # 本地持仓未被错误扣减
        assert pos.current_quantity == Decimal('0.1')


# ============================================================================
# D. _write_close_pnl helper 分支（用例 12-14，单测 helper 本身）
# ============================================================================

class TestWriteClosePnl:
    """_write_close_pnl 盈亏回写 helper 的三个独立分支

    直接单测 helper，不依赖 _close_position 上下文，与既有用例 5/6/7 的
    closed 分支回写断言解耦。closed 场景持仓为：direction=FLAT、
    current_quantity=0、entry_price 保留（供估算盈亏）。
    """

    async def test_update_pnl_exception_returns_none(self, strategy):
        """用例12：update_realized_pnl 抛异常 → helper 返回 None，异常不外泄"""
        mock_trade_logger = AsyncMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(
            side_effect=RuntimeError('db connection lost'))
        strategy.binance.trade_logger = mock_trade_logger

        # closed 分支已同步后的持仓：方向 FLAT、数量 0、入场价保留
        pos = make_position(direction='FLAT', quantity=Decimal('0'))
        # 参数有效（数量/价格/入场价均 > 0），可走到 update_realized_pnl 调用处
        result = await strategy._write_close_pnl(
            'BTCUSDT', pos, 'SELL', 'TP1',
            current_price=Decimal('100'), close_quantity=Decimal('0.1'),
        )
        # 异常被 helper 内部 except 捕获，返回 None 且不向外传播
        assert result is None

    async def test_close_quantity_none_skips_write(self, strategy):
        """用例13：close_quantity=None → 数量无效跳过回写，update_realized_pnl 未被调用"""
        mock_trade_logger = AsyncMock()
        strategy.binance.trade_logger = mock_trade_logger

        pos = make_position(direction='FLAT', quantity=Decimal('0'))
        result = await strategy._write_close_pnl(
            'BTCUSDT', pos, 'SELL', 'TP1',
            current_price=Decimal('100'), close_quantity=None,
        )
        assert result is None
        # 数量无效提前 return，未触达回写调用
        mock_trade_logger.update_realized_pnl.assert_not_awaited()

    async def test_trade_logger_none_returns_none(self, strategy):
        """用例14：trade_logger 为 None → helper 返回 None，不抛异常"""
        # 显式覆盖 AsyncMock 自动创建的 magic attribute，模拟未注入交易记录器
        strategy.binance.trade_logger = None

        pos = make_position(direction='FLAT', quantity=Decimal('0'))
        result = await strategy._write_close_pnl(
            'BTCUSDT', pos, 'SELL', 'TP1',
            current_price=Decimal('100'), close_quantity=Decimal('0.1'),
        )
        assert result is None
