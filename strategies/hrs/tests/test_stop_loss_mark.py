"""
测试 HRS 策略止损打标（_writeback_pnl_for_full_close 的亏损分支）。

条件单止损自动平仓被 detect_take_profit_fills 判定为"全部平仓"后，
回写 PnL 时若为亏损，必须补打 STOP_LOSS 标记，供风控看板
"最近止损次数"统计；盈利（止盈）不打标。

对应需求：把"止损打标"机制接入 HRS 条件单自动平仓路径。
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from strategies.hrs.strategy import HRSStrategy
from unittest.mock import AsyncMock, MagicMock


def make_strategy() -> HRSStrategy:
    """构造绕过 __init__ 的策略实例，注入被测方法所需依赖 mock。"""
    strategy = object.__new__(HRSStrategy)

    tl = MagicMock()
    tl.insert_pnl_summary = AsyncMock(return_value=True)
    tl.log_stop_loss = AsyncMock()
    strategy.binance_client = MagicMock()
    strategy.binance_client.trade_logger = tl

    # 实际 PnL 来自币安 API：亏损 -30
    strategy._get_actual_pnl_from_binance = AsyncMock(return_value=Decimal('-30'))
    return strategy


async def test_全部平仓为亏损_补打止损标():
    """全部平仓 PnL 为负时应打 STOP_LOSS 标记且方向映射正确。"""
    strategy = make_strategy()
    await strategy._writeback_pnl_for_full_close(
        symbol='SUIUSDT',
        direction='long',
        entry_price=1.0,
        entry_quantity=100,
        atr=0.02,
        current_price=0.98,
        pos={'entry_time': datetime.now(timezone.utc)},
    )

    strategy.binance_client.trade_logger.log_stop_loss.assert_awaited_once()
    kwargs = strategy.binance_client.trade_logger.log_stop_loss.await_args.kwargs
    assert kwargs['symbol'] == 'SUIUSDT'
    assert kwargs['side'] == 'SELL'  # long 平仓方向 SELL
    assert kwargs['realized_pnl'] == Decimal('-30')


async def test_全部平仓为盈利_不打止损标():
    """全部平仓 PnL 为正（止盈）时不应打 STOP_LOSS 标记。"""
    strategy = make_strategy()
    strategy._get_actual_pnl_from_binance = AsyncMock(return_value=Decimal('50'))
    await strategy._writeback_pnl_for_full_close(
        symbol='SUIUSDT',
        direction='long',
        entry_price=1.0,
        entry_quantity=100,
        atr=0.02,
        current_price=1.1,
        pos={'entry_time': datetime.now(timezone.utc)},
    )

    strategy.binance_client.trade_logger.log_stop_loss.assert_not_awaited()


async def test_全部平仓PnL回写失败_不影响主流程不打标():
    """insert_pnl_summary 失败时不设置标记，也不应抛错。"""
    strategy = make_strategy()
    strategy.binance_client.trade_logger.insert_pnl_summary = AsyncMock(return_value=False)
    strategy._get_actual_pnl_from_binance = AsyncMock(return_value=Decimal('-30'))
    await strategy._writeback_pnl_for_full_close(
        symbol='SUIUSDT',
        direction='long',
        entry_price=1.0,
        entry_quantity=100,
        atr=0.02,
        current_price=0.98,
        pos={'entry_time': datetime.now(timezone.utc)},
    )
    strategy.binance_client.trade_logger.log_stop_loss.assert_not_awaited()