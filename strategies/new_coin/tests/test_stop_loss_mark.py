"""
测试新币做空策略止损打标（_monitor_positions 的 pnl<0 分支）。

覆盖范围：
1. 持仓已平仓且实际盈亏为负（止损）时，调用 trade_logger.log_stop_loss。
2. 平仓方向固定为 BUY（做空平仓方向）。
3. realized_pnl 透传实际已实现亏损。
4. trade_logger 缺失时静默跳过。

对应需求：把"止损打标"机制接入 new_coin 策略止损平仓点，供风控看板
"最近止损次数"统计（pnl>=0 止盈不打标）。
"""
from datetime import datetime, timedelta
from decimal import Decimal

from strategies.new_coin.strategy import NewCoinStrategy
from unittest.mock import AsyncMock, MagicMock


def make_strategy() -> NewCoinStrategy:
    """构造绕过 __init__ 的策略实例，注入被测方法需要的依赖 mock。"""
    strategy = object.__new__(NewCoinStrategy)
    strategy.config = {'strategy': {'db_strategy_name': '新币做空策略'}}
    strategy.positions = {}
    strategy.consecutive_losses = 0

    # 交易执行器：持仓管理、取消条件单均为空操作
    strategy.trading_executor = MagicMock()
    strategy.trading_executor.check_position_management = AsyncMock()
    strategy.trading_executor.cancel_all_algo_orders = AsyncMock(return_value={'failed': 0})
    strategy.trading_executor.position_tracking = {}

    # 数据库：无平仓订单记录（不影响止损打标）
    strategy.db = MagicMock()
    strategy.db.fetch_one = AsyncMock(return_value=None)

    # 币安客户端 + 止损打标用的 trade_logger
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_position = AsyncMock(return_value=[])
    strategy.binance_client.trade_logger = make_trade_logger()

    # 打桩内部辅助方法，聚焦止损打标分支
    strategy._get_position_pnl = AsyncMock(return_value=Decimal('-20'))
    strategy._check_max_drawdown = AsyncMock()
    strategy._check_consecutive_loss_pause = AsyncMock()
    strategy._add_to_stop_loss_monitor = AsyncMock()
    strategy._save_state = AsyncMock()
    return strategy


def make_trade_logger():
    """构造带 async 打标/盈亏回写方法的 trade_logger 桩。"""
    tl = MagicMock()
    tl.log_stop_loss = AsyncMock()
    tl.update_realized_pnl = AsyncMock()
    return tl


def make_position_entry() -> dict:
    """构造一笔早已入场（非刚入场）的做空持仓。"""
    long_ago = (datetime.now() - timedelta(hours=2)).isoformat()
    return {'entry_price': Decimal('100'), 'entry_time': long_ago}


async def test_止损打标_亏损平仓触发():
    """pnl<0 的止损平仓应调用 log_stop_loss（side=BUY，realized_pnl=pnl）。"""
    strategy = make_strategy()
    symbol = 'NEWCOINUSDT'
    strategy.positions[symbol] = make_position_entry()

    await strategy._monitor_positions()

    tl = strategy.binance_client.trade_logger
    tl.log_stop_loss.assert_awaited_once()
    kwargs = tl.log_stop_loss.await_args.kwargs
    assert kwargs['symbol'] == symbol
    assert kwargs['side'] == 'BUY'          # 做空平仓方向 BUY
    assert kwargs['realized_pnl'] == Decimal('-20')


async def test_止损打标_平仓后清理持仓():
    """止损打标完成后持仓应从本地移除。"""
    strategy = make_strategy()
    symbol = 'NEWCOINUSDT'
    strategy.positions[symbol] = make_position_entry()

    await strategy._monitor_positions()

    assert symbol not in strategy.positions


async def test_止损打标_pnl为None但平仓单为亏损_补正打标():
    """pnl=None 但 trade_records 中平仓单 realized_pnl<0 时，应补正止损打标并移除持仓。"""
    strategy = make_strategy()
    # pnl 查询失败返回 None，触发 pnl=None 分支
    strategy._get_position_pnl = AsyncMock(return_value=None)
    # trade_records 返回真实亏损的平仓单
    strategy.db.fetch_one = AsyncMock(
        return_value={'order_id': 'CLOSE123', 'realized_pnl': Decimal('-8.5')}
    )
    symbol = 'NEWCOIN2USDT'
    strategy.positions[symbol] = make_position_entry()

    await strategy._monitor_positions()

    tl = strategy.binance_client.trade_logger
    tl.log_stop_loss.assert_awaited_once()
    kwargs = tl.log_stop_loss.await_args.kwargs
    assert kwargs['symbol'] == symbol
    assert kwargs['side'] == 'BUY'
    assert kwargs['realized_pnl'] == Decimal('-8.5')
    assert symbol not in strategy.positions


async def test_止损打标_pnl为None且无法判定_不打标仅清理():
    """pnl=None 且查不到平仓单/无法判定盈亏时，不应误打标，仅清理持仓。"""
    strategy = make_strategy()
    strategy._get_position_pnl = AsyncMock(return_value=None)
    strategy.db.fetch_one = AsyncMock(return_value=None)  # 查不到平仓单
    symbol = 'NEWCOIN3USDT'
    strategy.positions[symbol] = make_position_entry()

    await strategy._monitor_positions()

    strategy.binance_client.trade_logger.log_stop_loss.assert_not_awaited()
    assert symbol not in strategy.positions


async def test_止损打标_trade_logger缺失_静默跳过():
    """无 trade_logger 时不应调用打标，且不抛错。"""
    strategy = make_strategy()
    strategy.binance_client.trade_logger = None
    strategy.positions['NEWCOINUSDT'] = make_position_entry()

    await strategy._monitor_positions()