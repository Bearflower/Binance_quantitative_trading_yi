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
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from strategies.new_coin.strategy import NewCoinStrategy
from unittest.mock import AsyncMock, MagicMock


def make_strategy() -> NewCoinStrategy:
    """构造绕过 __init__ 的策略实例，注入被测方法需要的依赖 mock。"""
    strategy = object.__new__(NewCoinStrategy)
    strategy.config = {'strategy': {'name': 'new_coin', 'record_name': 'new_coin'}}
    # 派生属性由 __init__ 生成，本 fixture 绕过了 __init__，需手动补齐以忠实镜像新契约
    strategy.my_record_name = 'new_coin'
    strategy.positions = {}
    strategy.consecutive_losses = 0

    # 交易执行器：持仓管理、取消条件单均为空操作
    strategy.trading_executor = MagicMock()
    strategy.trading_executor.check_position_management = AsyncMock()
    strategy.trading_executor._update_short_position_closed = AsyncMock()
    strategy.trading_executor.cancel_all_algo_orders = AsyncMock(return_value={'failed': 0})
    strategy.trading_executor.clear_position_tracking = MagicMock()
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
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
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


async def test_真实止损监控_字符串入场时间不会阻断清仓():
    """不 mock 止损监控方法，覆盖持仓里真实的 ISO 字符串时间。"""
    strategy = make_strategy()
    strategy.stop_loss_monitor = {}
    strategy._add_to_stop_loss_monitor = NewCoinStrategy._add_to_stop_loss_monitor.__get__(strategy)
    symbol = 'NEWCOINUSDT'
    strategy.positions[symbol] = make_position_entry()

    await strategy._monitor_positions()

    assert symbol not in strategy.positions
    assert strategy.consecutive_losses == 1
    assert symbol in strategy.stop_loss_monitor
    strategy.binance_client.trade_logger.log_stop_loss.assert_awaited_once()
    await strategy._monitor_positions()
    assert strategy.consecutive_losses == 1


async def test_止损打标_pnl为None不能用单笔亏损代替整仓结算():
    """单张订单的亏损不能替代完整仓位盈亏，须保留结算身份。"""
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
    tl.log_stop_loss.assert_not_awaited()
    assert strategy.positions[symbol]['settlement_pending']


async def test_止损打标_pnl为None且无法判定_不打标保留结算():
    """无法判定盈亏时保留仓位并等待完整成交，不应误打标或清理身份。"""
    strategy = make_strategy()
    strategy._get_position_pnl = AsyncMock(return_value=None)
    strategy.db.fetch_one = AsyncMock(return_value=None)  # 查不到平仓单
    symbol = 'NEWCOIN3USDT'
    strategy.positions[symbol] = make_position_entry()

    await strategy._monitor_positions()

    strategy.binance_client.trade_logger.log_stop_loss.assert_not_awaited()
    assert strategy.positions[symbol]['settlement_pending']


async def test_止损打标_trade_logger缺失_静默跳过():
    """无 trade_logger 时不应调用打标，且不抛错。"""
    strategy = make_strategy()
    strategy.binance_client.trade_logger = None
    strategy.positions['NEWCOINUSDT'] = make_position_entry()

    await strategy._monitor_positions()


async def test_真实订单两笔成交合计盈亏且排除撤单(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "test")
    strategy = make_strategy()
    symbol = 'PFEUSDT'
    strategy.positions[symbol] = make_position_entry()
    strategy.db.fetch_all = AsyncMock(return_value=[
        {'order_id': '678808'}, {'order_id': '678821'},
    ])
    strategy.binance_client.get_order = AsyncMock(side_effect=lambda symbol, order_id: {
        'symbol':symbol, 'side':'BUY', 'orderId': order_id, 'status': 'CANCELED' if order_id == 678808 else 'FILLED',
        'executedQty': '0' if order_id == 678808 else '10',
    })
    strategy.binance_client.get_user_trades = AsyncMock(return_value=[
        {'symbol':'PFEUSDT','side':'BUY','id': 58293, 'orderId': 678821, 'realizedPnl': '-1.53967682'},
        {'symbol':'PFEUSDT','side':'BUY','id': 58294, 'orderId': 678821, 'realizedPnl': '-0.18982317'},
    ])
    async def fills_for_order(_symbol, *, order_id, limit):
        if order_id == '678808':
            return []
        return [
            {'symbol':'PFEUSDT','side':'BUY','id': 58293, 'orderId': 678821, 'realizedPnl': '-1.53967682', 'qty': '9'},
            {'symbol':'PFEUSDT','side':'BUY','id': 58294, 'orderId': 678821, 'realizedPnl': '-0.18982317', 'qty': '1'},
        ]
    strategy.binance_client.get_user_trades.side_effect = fills_for_order
    strategy._settlement_entry_quantity = AsyncMock(return_value=Decimal('10'))
    pnl = await NewCoinStrategy._get_position_pnl(strategy, symbol, Decimal('100'))
    assert pnl == Decimal('-1.72949999')
    assert 'order_id IS NOT NULL' in strategy.db.fetch_all.await_args.args[0]
    assert strategy.binance_client.get_user_trades.await_count == 1
