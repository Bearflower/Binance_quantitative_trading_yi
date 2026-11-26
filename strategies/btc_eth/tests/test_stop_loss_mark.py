"""逐笔核对两套 MTPCS 的真实平仓与止损归属。"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncpg import Range

from strategies.btc_eth_aggressive.close_evidence import confirmed_close_order, record_close_fills
from shared.trade_logger import TradeLogger


@pytest.fixture
def exchange():
    client = MagicMock()
    owner = TradeLogger(AsyncMock(), "btc_eth", account_id="main")
    client.trade_logger = owner
    owner._record_epoch = MagicMock(return_value=Range())
    owner.db.execute.return_value = "UPDATE 1"
    client.get_order = AsyncMock(return_value=dict(
        symbol="BTCUSDT", side="SELL", orderId=42, status="FILLED", reduceOnly=True,
        executedQty="2", origQty="2",
    ))
    client.get_user_trades = AsyncMock(return_value=[
        dict(symbol="BTCUSDT", side="SELL", orderId=42, id=number,
             qty="1", price="10", realizedPnl=pnl)
        for number, pnl in [(70, "-0.151"), (71, "0.021")]
    ])
    return client


@pytest.mark.parametrize("is_stop,reason", [(True, "STOP_LOSS"), (False, None)])
async def test_records_exact_signed_close(exchange, is_stop, reason):
    order = await confirmed_close_order(exchange, "BTCUSDT", "SELL", 42, Decimal("2"))
    assert await record_close_fills(exchange, order, is_stop)
    sql, pnl, tag, quantity, price, strategy, side, account, symbol, order_id, _, status = (
        exchange.trade_logger.db.execute.await_args.args)
    assert (pnl, tag, quantity, price) == (Decimal("-0.130"), reason, 2, 10)
    assert (strategy, side, account, symbol, order_id) == ("btc_eth", "SELL", "main", "BTCUSDT", "42")
    assert "HAVING count(*)=1" in sql
    assert status == "FILLED"


@pytest.mark.parametrize("field,value", [
    ("symbol", "ETHUSDT"), ("orderId", 41), ("reduceOnly", False),
    ("executedQty", "1"), ("side", "BUY"), ("status", "NEW"),
])
async def test_unconfirmed_close_keeps_position(exchange, field, value):
    exchange.get_order.return_value[field] = value
    assert await confirmed_close_order(exchange, "BTCUSDT", "SELL", 42, 2) is None


@pytest.mark.parametrize("field,value", [
    ("symbol", "ETHUSDT"), ("id", 70), ("qty", "0.1"), ("realizedPnl", "NaN")
])
async def test_invalid_fill_never_books_pnl(exchange, field, value):
    exchange.get_user_trades.return_value[-1][field] = value
    with pytest.raises(ValueError):
        await record_close_fills(exchange, exchange.get_order.return_value, True)
    exchange.trade_logger.db.execute.assert_not_awaited()


async def test_partial_close_retains_live_protection_without_duplicate(monkeypatch):
    from types import SimpleNamespace
    from strategies.btc_eth_aggressive.close_evidence import remaining_protection

    position = SimpleNamespace(stop_loss_order_id=11, tp2_order_id=12)
    owned = AsyncMock(return_value=[
        dict(order_type="STOP_LOSS", side="SELL", reduceOnly=True, algo_id=11),
        dict(order_type="TAKE_PROFIT", tp_level=2, side="SELL", reduceOnly=True, algo_id=12),
    ])
    monkeypatch.setattr("shared.condition_orders.strategy_owned_algos", owned)
    assert await remaining_protection(object(), "BTCUSDT", position, "SELL") == [True, True]
    owned.return_value = []
    monkeypatch.setattr("shared.condition_orders.classic_algo_is_terminal", AsyncMock(return_value=False))
    with pytest.raises(ValueError, match="状态未确认"):
        await remaining_protection(object(), "BTCUSDT", position, "SELL")
    assert (position.stop_loss_order_id, position.tp2_order_id) == (11, 12)


async def test_partial_terminal_records_actual_fills_without_inventing_full_status(exchange):
    exchange.get_order.return_value.update(status='CANCELED',executedQty='1')
    exchange.get_user_trades.return_value=exchange.get_user_trades.return_value[:1]
    order=await confirmed_close_order(exchange,'BTCUSDT','SELL',42,Decimal('2'))
    assert order is not None
    assert await record_close_fills(exchange,order,True)
    args=exchange.trade_logger.db.execute.await_args.args
    assert args[1]==Decimal('-.151') and args[3]==1 and args[-1]=='CANCELED'
