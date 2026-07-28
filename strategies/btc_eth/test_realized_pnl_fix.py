"""
btc_eth 策略平仓盈亏回写测试

测试目标：验证 BTCEthStrategy._close_position 方法中平仓盈亏
回写 trade_records.realized_pnl 的逻辑正确性。

覆盖场景：
1. 平仓后调用 update_realized_pnl 回写盈亏
2. LONG 方向盈亏计算正确
3. SHORT 方向盈亏计算正确
4. avgPrice 为 0 时回退到 current_price
5. 回写失败不阻断平仓流程

关键代码位置：
- strategies/btc_eth/strategy.py 的 _close_position: 第2698-2726行
"""
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal
from datetime import datetime

import pytest
import sys
import os

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from strategies.btc_eth.strategy import BTCEthStrategy, PositionState
from shared.trade_logger import TradeLogger


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def btc_strategy():
    """构造 BTCEthStrategy 实例，mock 所有外部依赖

    被 mock 的依赖：
    - binance: 币安客户端（AsyncMock）
    - kline_service: K线服务（AsyncMock）
    - notification: 通知客户端（AsyncMock）
    - db_manager: 数据库管理器（AsyncMock）
    """
    config = {
        "strategy": {
            "symbols": ["BTCUSDT"],
            "timeframes": ["1h"],
            "risk": {
                "position_sizing": {
                    "min_close_notional_usdt": 20,
                },
                "close_limit_order": {
                    "max_retries": 1,
                    "retry_interval_seconds": 1,
                    "poll_interval_seconds": 1,
                    "timeout_seconds": 2,
                },
                "frequency_control": {},
            },
            "scoring": {},
        },
        "binance": {},
    }

    binance_client = AsyncMock()
    kline_service = AsyncMock()
    notification_client = AsyncMock()
    db_manager = AsyncMock()

    strategy = BTCEthStrategy(
        config=config,
        binance_client=binance_client,
        kline_service=kline_service,
        notification_client=notification_client,
        db_manager=db_manager,
    )

    # mock 精度相关方法，避免真实 API 调用
    strategy._get_symbol_precision = AsyncMock(return_value={
        "stepSize": "0.001",
        "tickSize": Decimal("0.01"),
    })

    # mock 订单簿
    binance_client.get_orderbook = AsyncMock(return_value={
        "bids": [["50000.0", "1.0"]],
        "asks": [["50100.0", "1.0"]],
    })

    # mock 交易执行相关
    binance_client.place_order = AsyncMock(return_value={
        "orderId": 123456,
        "avgPrice": "0",
        "status": "FILLED",
    })
    binance_client.get_open_orders = AsyncMock(return_value=[])
    binance_client.get_ticker_price = AsyncMock(return_value="50000.0")
    binance_client.cancel_order = AsyncMock()

    # mock 通知
    notification_client.send_trade_notification = AsyncMock()

    return strategy


def make_position(
    direction: str = "LONG",
    entry_price: Decimal = Decimal("100"),
    current_quantity: Decimal = Decimal("1.0"),
    initial_quantity: Decimal = Decimal("1.0"),
) -> PositionState:
    """创建 PositionState 实例的辅助函数"""
    pos = PositionState()
    pos.direction = direction
    pos.entry_price = entry_price
    pos.current_quantity = current_quantity
    pos.initial_quantity = initial_quantity
    pos.entry_time = datetime.now()
    return pos


# ---------------------------------------------------------------------------
# F2: btc_eth 策略回写测试
# ---------------------------------------------------------------------------


class TestBtcEthRealizedPnlWriteback:
    """btc_eth 策略平仓盈亏回写测试"""

    # ==================== 平仓后调用 update_realized_pnl ====================

    async def test_close_position_calls_update_realized_pnl(self, btc_strategy):
        """平仓后调用 update_realized_pnl 回写盈亏

        验证：
        - 平仓成功后调用了 trade_logger.update_realized_pnl
        - 传入的 order_id 来自平仓单
        - 传入的 realized_pnl 为 Decimal 类型
        """
        strategy = btc_strategy
        symbol = "BTCUSDT"
        position = make_position(
            direction="LONG",
            entry_price=Decimal("100"),
            current_quantity=Decimal("1.0"),
        )

        # 设置 trade_logger
        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy.binance.trade_logger = mock_trade_logger

        # 设置平仓返回结果（avgPrice 有值，触发 pnl 计算）
        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 999888,
            "avgPrice": "110",
            "status": "FILLED",
        })

        result = await strategy._close_position(
            symbol=symbol,
            position=position,
            close_quantity=Decimal("1.0"),
            close_reason="TP1",
            current_price=Decimal("110"),
        )

        assert result is True
        # 验证 update_realized_pnl 被调用
        mock_trade_logger.update_realized_pnl.assert_called_once()
        call_kwargs = mock_trade_logger.update_realized_pnl.call_args.kwargs
        assert call_kwargs["order_id"] == "999888"
        assert isinstance(call_kwargs["realized_pnl"], Decimal)
        assert call_kwargs["side"] == "SELL"  # 做多平仓方向为 SELL
        assert call_kwargs["symbol"] == symbol
        assert "executed_at" in call_kwargs

    # ==================== LONG 方向 pnl 计算 ====================

    async def test_close_position_long_pnl_calculation(self, btc_strategy):
        """LONG 方向：entry_price=100, exit_price=110, quantity=1 => pnl=10

        验证：做多平仓盈亏 = (出场价 - 入场价) * 数量
        """
        strategy = btc_strategy
        position = make_position(
            direction="LONG",
            entry_price=Decimal("100"),
            current_quantity=Decimal("1.0"),
        )

        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy.binance.trade_logger = mock_trade_logger

        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 1,
            "avgPrice": "110",
            "status": "FILLED",
        })

        await strategy._close_position(
            symbol="BTCUSDT",
            position=position,
            close_quantity=Decimal("1.0"),
            close_reason="TP1",
            current_price=Decimal("110"),
        )

        call_kwargs = mock_trade_logger.update_realized_pnl.call_args.kwargs
        pnl = call_kwargs["realized_pnl"]
        assert pnl == Decimal("10"), f"LONG pnl 应为 10，实际为 {pnl}"
        assert call_kwargs["side"] == "SELL", "LONG 平仓方向应为 SELL"

    async def test_close_position_long_loss(self, btc_strategy):
        """LONG 方向亏损：entry_price=100, exit_price=90, quantity=1 => pnl=-10"""
        strategy = btc_strategy
        position = make_position(
            direction="LONG",
            entry_price=Decimal("100"),
            current_quantity=Decimal("1.0"),
        )

        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy.binance.trade_logger = mock_trade_logger

        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 1,
            "avgPrice": "90",
            "status": "FILLED",
        })

        await strategy._close_position(
            symbol="BTCUSDT",
            position=position,
            close_quantity=Decimal("1.0"),
            close_reason="STOP_LOSS",
            current_price=Decimal("90"),
        )

        call_kwargs = mock_trade_logger.update_realized_pnl.call_args.kwargs
        pnl = call_kwargs["realized_pnl"]
        assert pnl == Decimal("-10"), f"LONG 亏损 pnl 应为 -10，实际为 {pnl}"

    # ==================== SHORT 方向 pnl 计算 ====================

    async def test_close_position_short_pnl_calculation(self, btc_strategy):
        """SHORT 方向：entry_price=110, exit_price=100, quantity=1 => pnl=10

        验证：做空平仓盈亏 = (入场价 - 出场价) * 数量
        """
        strategy = btc_strategy
        position = make_position(
            direction="SHORT",
            entry_price=Decimal("110"),
            current_quantity=Decimal("1.0"),
        )

        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy.binance.trade_logger = mock_trade_logger

        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 1,
            "avgPrice": "100",
            "status": "FILLED",
        })

        await strategy._close_position(
            symbol="BTCUSDT",
            position=position,
            close_quantity=Decimal("1.0"),
            close_reason="TP1",
            current_price=Decimal("100"),
        )

        call_kwargs = mock_trade_logger.update_realized_pnl.call_args.kwargs
        pnl = call_kwargs["realized_pnl"]
        assert pnl == Decimal("10"), f"SHORT pnl 应为 10，实际为 {pnl}"
        assert call_kwargs["side"] == "BUY", "SHORT 平仓方向应为 BUY"

    async def test_close_position_short_loss(self, btc_strategy):
        """SHORT 方向亏损：entry_price=90, exit_price=100, quantity=1 => pnl=-10"""
        strategy = btc_strategy
        position = make_position(
            direction="SHORT",
            entry_price=Decimal("90"),
            current_quantity=Decimal("1.0"),
        )

        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy.binance.trade_logger = mock_trade_logger

        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 1,
            "avgPrice": "100",
            "status": "FILLED",
        })

        await strategy._close_position(
            symbol="BTCUSDT",
            position=position,
            close_quantity=Decimal("1.0"),
            close_reason="STOP_LOSS",
            current_price=Decimal("100"),
        )

        call_kwargs = mock_trade_logger.update_realized_pnl.call_args.kwargs
        pnl = call_kwargs["realized_pnl"]
        assert pnl == Decimal("-10"), f"SHORT 亏损 pnl 应为 -10，实际为 {pnl}"

    # ==================== avgPrice 为 0 时回退到 current_price ====================

    async def test_close_position_avg_price_zero_fallback(self, btc_strategy):
        """avgPrice 为 0 时回退到 current_price 计算 pnl

        验证：avgPrice="0" 时，使用 current_price（110）计算 pnl
        LONG: (110 - 100) * 1 = 10
        """
        strategy = btc_strategy
        position = make_position(
            direction="LONG",
            entry_price=Decimal("100"),
            current_quantity=Decimal("1.0"),
        )

        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy.binance.trade_logger = mock_trade_logger

        # avgPrice 返回 "0"
        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 1,
            "avgPrice": "0",
            "status": "FILLED",
        })

        await strategy._close_position(
            symbol="BTCUSDT",
            position=position,
            close_quantity=Decimal("1.0"),
            close_reason="TP1",
            current_price=Decimal("110"),
        )

        call_kwargs = mock_trade_logger.update_realized_pnl.call_args.kwargs
        pnl = call_kwargs["realized_pnl"]
        assert pnl == Decimal("10"), f"avgPrice 回退后 pnl 应为 10，实际为 {pnl}"

    async def test_close_position_avg_price_zero_no_current_price(self, btc_strategy):
        """avgPrice 和 current_price 都为 0 时跳过 pnl 计算

        验证：avgPrice="0" 且 current_price=None 时，不调用 update_realized_pnl
        """
        strategy = btc_strategy
        position = make_position(
            direction="LONG",
            entry_price=Decimal("100"),
            current_quantity=Decimal("1.0"),
        )

        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy.binance.trade_logger = mock_trade_logger

        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 1,
            "avgPrice": "0",
            "status": "FILLED",
        })

        # 不传 current_price，且 get_ticker_price 返回 "0"
        strategy.binance.get_ticker_price = AsyncMock(return_value="0")
        strategy.binance.get_orderbook = AsyncMock(return_value={
            "bids": [["0", "0"]],
            "asks": [["0", "0"]],
        })

        # 由于 orderbook 返回 0 价格，limit_price 会是 0，需要特殊处理
        # 这里直接测试 avgPrice 为 0 且 current_price 为 0 的情况
        # 实际上 _close_position 会先获取 orderbook 价格...
        # 简化测试：直接 mock get_ticker_price 返回 50000

        # 重置：让 get_ticker_price 返回 50000，但 avgPrice 为 0
        # 在 _close_position 中，current_price 由参数传入
        result = await strategy._close_position(
            symbol="BTCUSDT",
            position=position,
            close_quantity=Decimal("1.0"),
            close_reason="TP1",
            current_price=Decimal("0"),  # 传入 0
        )

        # 由于 avgPrice="0" 且 current_price=Decimal("0")，
        # exit_price = Decimal("0") 或 Decimal("0")，均为 0
        # 所以 exit_price <= 0，跳过 pnl 计算
        # 但平仓成功
        assert result is True
        mock_trade_logger.update_realized_pnl.assert_not_called()

    # ==================== 回写失败不阻断平仓流程 ====================

    async def test_close_position_writeback_fail_not_blocking(self, btc_strategy):
        """回写失败不阻断平仓流程

        验证：update_realized_pnl 抛出异常时，平仓流程继续，返回 True
        """
        strategy = btc_strategy
        position = make_position(
            direction="LONG",
            entry_price=Decimal("100"),
            current_quantity=Decimal("1.0"),
        )

        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(
            side_effect=Exception("回写失败")
        )
        strategy.binance.trade_logger = mock_trade_logger

        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 1,
            "avgPrice": "110",
            "status": "FILLED",
        })

        # 不应抛出异常，平仓应成功
        result = await strategy._close_position(
            symbol="BTCUSDT",
            position=position,
            close_quantity=Decimal("1.0"),
            close_reason="TP1",
            current_price=Decimal("110"),
        )

        assert result is True, "回写失败不应影响平仓结果"

    async def test_close_position_no_trade_logger(self, btc_strategy):
        """trade_logger 为 None 时跳过回写，不抛异常

        验证：binance.trade_logger 为 None 时，平仓流程正常完成
        """
        strategy = btc_strategy
        position = make_position(
            direction="LONG",
            entry_price=Decimal("100"),
            current_quantity=Decimal("1.0"),
        )

        # trade_logger 为 None
        strategy.binance.trade_logger = None

        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 1,
            "avgPrice": "110",
            "status": "FILLED",
        })

        # 不应抛出异常
        result = await strategy._close_position(
            symbol="BTCUSDT",
            position=position,
            close_quantity=Decimal("1.0"),
            close_reason="TP1",
            current_price=Decimal("110"),
        )

        assert result is True

    # ==================== 部分平仓场景 ====================

    async def test_close_position_partial_close(self, btc_strategy):
        """部分平仓时 pnl 按实际平仓数量计算

        验证：平仓数量为 0.5，pnl = (110 - 100) * 0.5 = 5
        """
        strategy = btc_strategy
        position = make_position(
            direction="LONG",
            entry_price=Decimal("100"),
            current_quantity=Decimal("1.0"),
            initial_quantity=Decimal("1.0"),
        )

        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy.binance.trade_logger = mock_trade_logger

        strategy.binance.place_order = AsyncMock(return_value={
            "orderId": 1,
            "avgPrice": "110",
            "status": "FILLED",
        })

        await strategy._close_position(
            symbol="BTCUSDT",
            position=position,
            close_quantity=Decimal("0.5"),
            close_reason="TP1",
            current_price=Decimal("110"),
        )

        call_kwargs = mock_trade_logger.update_realized_pnl.call_args.kwargs
        pnl = call_kwargs["realized_pnl"]
        assert pnl == Decimal("5"), f"部分平仓 pnl 应为 5，实际为 {pnl}"


# ===========================================================================
# F4: calculate_pnl 静态方法测试
# ===========================================================================


class TestCalculatePnl:
    """TradeLogger.calculate_pnl 静态方法测试"""

    # ==================== LONG 方向 ====================

    def test_long_profit(self):
        """LONG 盈利：entry=100, exit=110, qty=1 => pnl=10"""
        pnl = TradeLogger.calculate_pnl('LONG', Decimal('100'), Decimal('110'), Decimal('1'))
        assert pnl == Decimal('10')

    def test_long_loss(self):
        """LONG 亏损：entry=100, exit=90, qty=1 => pnl=-10"""
        pnl = TradeLogger.calculate_pnl('LONG', Decimal('100'), Decimal('90'), Decimal('1'))
        assert pnl == Decimal('-10')

    def test_long_zero(self):
        """LONG 持平：entry=100, exit=100, qty=1 => pnl=0"""
        pnl = TradeLogger.calculate_pnl('LONG', Decimal('100'), Decimal('100'), Decimal('1'))
        assert pnl == Decimal('0')

    # ==================== SHORT 方向 ====================

    def test_short_profit(self):
        """SHORT 盈利：entry=110, exit=100, qty=1 => pnl=10"""
        pnl = TradeLogger.calculate_pnl('SHORT', Decimal('110'), Decimal('100'), Decimal('1'))
        assert pnl == Decimal('10')

    def test_short_loss(self):
        """SHORT 亏损：entry=90, exit=100, qty=1 => pnl=-10"""
        pnl = TradeLogger.calculate_pnl('SHORT', Decimal('90'), Decimal('100'), Decimal('1'))
        assert pnl == Decimal('-10')

    # ==================== 边界情况 ====================

    def test_partial_quantity(self):
        """部分平仓：entry=100, exit=110, qty=0.5 => pnl=5"""
        pnl = TradeLogger.calculate_pnl('LONG', Decimal('100'), Decimal('110'), Decimal('0.5'))
        assert pnl == Decimal('5')

    def test_invalid_direction(self):
        """不支持的持仓方向抛出 ValueError"""
        with pytest.raises(ValueError, match="不支持的持仓方向"):
            TradeLogger.calculate_pnl('INVALID', Decimal('100'), Decimal('110'), Decimal('1'))