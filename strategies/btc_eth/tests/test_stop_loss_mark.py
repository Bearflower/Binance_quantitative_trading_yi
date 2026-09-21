"""
测试 MTPCS 策略止损打标（_mark_stop_loss_if_needed）。

覆盖范围：
1. 止盈平仓（TP1/TP2/TRAILING_STOP）不打 STOP_LOSS 标记。
2. 止损/保护性平仓（TIME_STOP/EXTREME/LIQUIDATION）打 STOP_LOSS 标记。
3. 平仓方向映射：LONG→SELL，SHORT→BUY。
4. trade_logger 缺失时静默跳过，不抛错。
5. realized_pnl 透传给 log_stop_loss。

对应需求：把"止损打标"机制接入 btc_eth 策略止损平仓点，供风控看板
"最近止损次数"统计（不做止盈误标）。
"""
from decimal import Decimal
from types import SimpleNamespace

from strategies.btc_eth.strategy import BTCEthStrategy
from unittest.mock import AsyncMock, MagicMock


def make_strategy() -> BTCEthStrategy:
    """构造绕过 __init__ 的策略实例，注入被测方法所需 mock 属性。"""
    strategy = object.__new__(BTCEthStrategy)
    return strategy


def make_position(direction: str = "LONG"):
    """构造仅含 direction 的最小持仓桩。"""
    return SimpleNamespace(direction=direction)


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