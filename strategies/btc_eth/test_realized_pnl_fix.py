"""MTPCS 平仓只按已核实的普通订单记账，另保留理论收益公式的单测。"""
import json
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


@pytest.mark.parametrize("side,reason", [
    ("SELL", "TIME_STOP"), ("BUY", "TP1"), ("SELL", "TRAILING_STOP"),
])
async def test_records_confirmed_close_at_strategy_entry(btc_strategy, monkeypatch, side, reason):
    strategy = btc_strategy
    amount = Decimal("0.5")
    position = make_position(direction="LONG" if side == "SELL" else "SHORT")
    order = dict(orderId=123456, symbol="BTCUSDT", side=side, status="FILLED",
                 reduceOnly=True, origQty="0.5", executedQty="0.5", avgPrice="200")
    strategy.binance.get_position.return_value = [dict(symbol="BTCUSDT",
        positionAmt="1" if side == "SELL" else "-1")]
    strategy.binance.get_order.return_value = order
    monkeypatch.setenv('TRADE_ACCOUNT_ID', 'test')
    monkeypatch.delenv('BINANCE_CLASSIC_CUTOVER_MS', raising=False)
    strategy.binance.use_unified_account = True
    ledger = MagicMock(account_id='test', strategy_name='btc_eth')
    ledger._record_epoch.return_value = 'epoch'
    ledger._receipt_in_epoch.return_value = True
    ledger._epoch_trades.side_effect = lambda fills, epoch: fills
    ledger.db.execute = AsyncMock(return_value='UPDATE 1')
    strategy.binance.trade_logger = ledger
    strategy.binance.get_user_trades.return_value = [dict(
        symbol='BTCUSDT', side=side, orderId=123456, id=9, qty='0.5',
        price='200', realizedPnl='42')]
    strategy.db_manager.execute.return_value = 'INSERT 0 1'
    strategy.db_manager.fetch_one.return_value = None
    with patch("strategies.btc_eth.strategy.cancel_strategy_algos", new_callable=AsyncMock), patch(
        "asyncio.sleep", new_callable=AsyncMock):
        assert await strategy._close_position("BTCUSDT", position, amount, reason, Decimal("200"))
    strategy.binance.get_user_trades.assert_awaited_once_with('BTCUSDT', order_id='123456', limit=1000)
    ledger.db.execute.assert_awaited_once()
    args = ledger.db.execute.await_args.args
    assert args[1:5] == (Decimal('42'), 'STOP_LOSS' if reason == 'TIME_STOP' else None,
                         amount, Decimal('200'))
    assert args[6:10] == (side, 'test', 'BTCUSDT', '123456')
    queued = [json.loads(call.args[3]) for call in strategy.db_manager.execute.await_args_list
              if str(call.args[2]).startswith('close-pnl:')]
    assert [item['status'] for item in queued] == ['awaiting_terminal', 'pending', 'done']
    assert all(item['order']['orderId'] == 123456 and item['context']['account_id'] == 'test'
               for item in queued)
    assert Decimal(queued[0]['order']['origQty']) == amount
    assert 'executedQty' not in queued[0]['order']  # Acceptance does not invent a fill.
    assert position.current_quantity == amount


async def test_disappeared_canceled_order_keeps_position(btc_strategy):
    strategy = btc_strategy
    position = make_position()
    strategy.binance.get_position.return_value = [dict(symbol="BTCUSDT", positionAmt="1")]
    strategy.binance.get_order.return_value = dict(orderId=123456, symbol="BTCUSDT",
        side="SELL", reduceOnly=True, status="CANCELED")
    with patch("strategies.btc_eth.strategy.cancel_strategy_algos", new_callable=AsyncMock), patch(
        "asyncio.sleep", new_callable=AsyncMock
    ):
        assert not await strategy._close_position("BTCUSDT", position, Decimal("1"), "TP1", Decimal("200"))
    assert position.current_quantity == Decimal("1")
    strategy.binance.get_user_trades.assert_not_awaited()


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