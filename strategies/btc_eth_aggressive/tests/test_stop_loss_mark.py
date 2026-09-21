"""
测试 MTPCS 激进版止损打标（_mark_stop_loss_if_needed）与平仓前持仓同步（_sync_position_with_exchange）。

覆盖范围：
1. 止盈平仓（TP1/TP2/TRAILING_STOP）不打 STOP_LOSS 标记。
2. 止损/保护性平仓（TIME_STOP/EXTREME/LIQUIDATION）打 STOP_LOSS 标记。
3. 平仓方向映射：LONG→SELL，SHORT→BUY。
4. trade_logger 缺失时静默跳过，不抛错。
5. realized_pnl 透传给 log_stop_loss。
6. _sync_position_with_exchange：closed / partially_closed / 正常 / 异常 四个分支。

对应需求：激进版补齐平仓前持仓同步与止损打标，与 btc_eth 原版对齐，
供风控看板"最近止损次数"统计（不做止盈误标）。
"""
from decimal import Decimal
from types import SimpleNamespace

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy
from unittest.mock import AsyncMock, MagicMock


def make_strategy() -> BTCEthStrategy:
    """构造绕过 __init__ 的策略实例，注入被测方法所需 mock 属性。"""
    strategy = object.__new__(BTCEthStrategy)
    return strategy


def make_position(direction: str = "LONG", quantity: Decimal = Decimal("10")):
    """构造含 direction / current_quantity 的最小持仓桩。"""
    return SimpleNamespace(direction=direction, current_quantity=quantity)


def install_trade_logger(strategy: BTCEthStrategy, trade_logger=None):
    """注入 binance.trade_logger；默认注入 AsyncMock。"""
    strategy.binance = MagicMock()
    strategy.binance.trade_logger = trade_logger if trade_logger is not None else AsyncMock()
    return strategy.binance.trade_logger


async def test_止损打标_止盈不打标():
    """止盈 close_reason（TP1/TP2/TRAILING_STOP）应跳过打标。"""
    for reason in ("TP1", "TP2", "TRAILING_STOP"):
        strategy = make_strategy()
        tl = install_trade_logger(strategy)
        await strategy._mark_stop_loss_if_needed("BTCUSDT", make_position("LONG"), reason)
        tl.log_stop_loss.assert_not_awaited()


async def test_止损打标_止损类必打标():
    """止损类 close_reason 应打 STOP_LOSS 标记且方向映射正确。"""
    for reason in ("TIME_STOP", "TIME_STOP_REVIEW", "EXTREME", "LIQUIDATION"):
        strategy = make_strategy()
        tl = install_trade_logger(strategy)
        await strategy._mark_stop_loss_if_needed("BTCUSDT", make_position("LONG"), reason)
        tl.log_stop_loss.assert_awaited_once()
        kwargs = tl.log_stop_loss.await_args.kwargs
        assert kwargs["symbol"] == "BTCUSDT"
        assert kwargs["side"] == "SELL"  # LONG 平仓方向 SELL


async def test_止损打标_平仓方向映射():
    """SHORT 平仓方向为 BUY。"""
    strategy = make_strategy()
    tl = install_trade_logger(strategy)
    await strategy._mark_stop_loss_if_needed("ETHUSDT", make_position("SHORT"), "TIME_STOP")
    tl.log_stop_loss.assert_awaited_once()
    assert tl.log_stop_loss.await_args.kwargs["side"] == "BUY"


async def test_止损打标_realized_pnl_透传():
    """已实现盈亏应透传给 log_stop_loss。"""
    strategy = make_strategy()
    tl = install_trade_logger(strategy)
    await strategy._mark_stop_loss_if_needed(
        "BTCUSDT", make_position("LONG"), "TIME_STOP", Decimal("-10.5")
    )
    tl.log_stop_loss.assert_awaited_once()
    assert tl.log_stop_loss.await_args.kwargs["realized_pnl"] == Decimal("-10.5")


async def test_止损打标_trade_logger缺失_静默跳过():
    """无 trade_logger 时应静默跳过，不抛错。"""
    strategy = make_strategy()
    install_trade_logger(strategy, trade_logger=None)
    await strategy._mark_stop_loss_if_needed("BTCUSDT", make_position("LONG"), "TIME_STOP")


async def test_止损打标_trade_logger缺log_stop_loss_静默跳过():
    """trade_logger 无 log_stop_loss 属性时应静默跳过。"""
    strategy = make_strategy()
    install_trade_logger(strategy, trade_logger=MagicMock())
    # 移除 log_stop_loss 属性模拟旧版本
    del strategy.binance.trade_logger.log_stop_loss
    await strategy._mark_stop_loss_if_needed("BTCUSDT", make_position("LONG"), "TIME_STOP")


async def test_持仓同步_交易所空列表视为已平仓():
    """PM 账户对已平仓 symbol 返回空列表，应标记 closed 并清零本地持仓。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[])
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is True
    assert result["partially_closed"] is False
    assert pos.current_quantity == Decimal("0")
    assert pos.direction == "FLAT"


async def test_持仓同步_posAmt为零视为已平仓():
    """显式返回 posAmt≈0 的边缘情况，同样标记 closed。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{"symbol": "BTCUSDT", "positionAmt": "0"}])
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is True
    assert pos.current_quantity == Decimal("0")
    assert pos.direction == "FLAT"


async def test_持仓同步_部分平仓个数同步():
    """实际持仓小于本地记录时，标记 partially_closed 并同步数量。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{"symbol": "BTCUSDT", "positionAmt": "-6"}])
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is False
    assert result["partially_closed"] is True
    assert result["actual_quantity"] == Decimal("6")
    assert pos.current_quantity == Decimal("6")


async def test_持仓同步_持仓量正常():
    """实际持仓 >= 本地记录时，状态正常不改变。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{"symbol": "BTCUSDT", "positionAmt": "-12"}])
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is False
    assert result["partially_closed"] is False
    assert result["actual_quantity"] == Decimal("12")
    assert pos.current_quantity == Decimal("10")


async def test_持仓同步_查询异常返回None不抛错():
    """查询交易所持仓异常时应返回 actual_quantity=None 且不抛错。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(side_effect=RuntimeError("网络异常"))
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is False
    assert result["partially_closed"] is False
    assert result["actual_quantity"] is None