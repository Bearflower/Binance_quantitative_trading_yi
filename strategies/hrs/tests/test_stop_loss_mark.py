"""
测试 HRS 策略「全部平仓 PnL 回写」（_writeback_pnl_for_full_close）。

条件单止损自动平仓被 detect_take_profit_fills 判定为"全部平仓"后，回写 PnL：
- 只写一条 PNL_SUMMARY 记录，亏损时把 close_reason='STOP_LOSS' 一并带上
  （供风控看板"最近止损次数"统计），盈利（止盈）不带标记。
- 必须同时传 since=开仓时间做幂等去重，避免同一笔平仓被重复记账。

回归背景（2026-10-09）：旧实现先 insert_pnl_summary 再 mark_stop_loss，而
log_stop_loss 的降级匹配带 `order_type <> 'PNL_SUMMARY'`，必然匹配不到刚写入的
汇总记录 → 又插一条金额完全相同的记录，HRS 同一笔平仓被记两次，回撤虚高到 100%。
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from strategies.hrs.strategy import HRSStrategy
from unittest.mock import AsyncMock, MagicMock

# 北京时间（与 trade_records.executed_at 同口径的 naive 时间）
_BEIJING = timezone(timedelta(hours=8))


def make_strategy() -> HRSStrategy:
    """构造绕过 __init__ 的策略实例，注入被测方法所需依赖 mock。"""
    strategy = object.__new__(HRSStrategy)

    tl = MagicMock()
    tl.insert_pnl_summary = AsyncMock(return_value=True)
    tl.log_stop_loss = AsyncMock()
    tl.CLOSE_REASON_STOP_LOSS = "STOP_LOSS"
    strategy.binance_client = MagicMock()
    strategy.binance_client.trade_logger = tl
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[{
        'account_id': 'default', 'realized_pnl': Decimal('-30'),
        'pnl_source': 'exchange_order_matched',
    }])

    # 实际 PnL 来自币安 API：亏损 -30
    strategy._get_actual_pnl_from_binance = AsyncMock(return_value=Decimal('-30'))
    return strategy


async def test_全部平仓为亏损_单条写入且带止损标():
    """亏损时应只写一条 PNL_SUMMARY，close_reason=STOP_LOSS，方向映射正确。"""
    strategy = make_strategy()
    entry_time = datetime.now(timezone.utc)
    await strategy._writeback_pnl_for_full_close(
        symbol='SUIUSDT',
        direction='long',
        entry_price=1.0,
        entry_quantity=100,
        atr=0.02,
        current_price=0.98,
        pos={'opened_at': entry_time},
    )

    tl = strategy.binance_client.trade_logger
    # 只写一条：不再额外走 log_stop_loss 的兜底插入（旧实现会写两条）
    tl.insert_pnl_summary.assert_awaited_once()
    tl.log_stop_loss.assert_not_awaited()

    kwargs = tl.insert_pnl_summary.await_args.kwargs
    assert kwargs['symbol'] == 'SUIUSDT'
    assert kwargs['side'] == 'SELL'  # long 平仓方向 SELL
    assert kwargs['realized_pnl'] == Decimal('-30')
    assert kwargs['close_reason'] == 'STOP_LOSS'
    # since 必须是北京时间 naive，才能与 executed_at 同口径比较
    assert kwargs['since'] == entry_time.astimezone(_BEIJING).replace(tzinfo=None)
    assert kwargs['since'].tzinfo is None


async def test_全部平仓为盈利_不打止损标():
    """全部平仓 PnL 为正（止盈）时 close_reason 应为 None。"""
    strategy = make_strategy()
    strategy._get_actual_pnl_from_binance = AsyncMock(return_value=Decimal('50'))
    await strategy._writeback_pnl_for_full_close(
        symbol='SUIUSDT',
        direction='long',
        entry_price=1.0,
        entry_quantity=100,
        atr=0.02,
        current_price=1.1,
        pos={'opened_at': datetime.now(timezone.utc)},
    )

    tl = strategy.binance_client.trade_logger
    tl.insert_pnl_summary.assert_awaited_once()
    tl.log_stop_loss.assert_not_awaited()
    assert tl.insert_pnl_summary.await_args.kwargs['close_reason'] is None


async def test_做空平仓方向映射为BUY():
    """short 持仓平仓方向应为 BUY。"""
    strategy = make_strategy()
    await strategy._writeback_pnl_for_full_close(
        symbol='SUIUSDT',
        direction='short',
        entry_price=1.0,
        entry_quantity=100,
        atr=0.02,
        current_price=1.02,
        pos={'opened_at': datetime.now(timezone.utc)},
    )
    assert strategy.binance_client.trade_logger.insert_pnl_summary.await_args.kwargs['side'] == 'BUY'


async def test_无opened_at时since为空且不报错():
    """持仓缺少 opened_at 时不能把理论值写成最终盈亏。"""
    strategy = make_strategy()
    # pos 无 opened_at → 跳过币安 API 路径，降级到理论 PnL 计算
    strategy._calculate_theoretical_total_pnl = AsyncMock(return_value=Decimal('-30'))
    result = await strategy._writeback_pnl_for_full_close(
        symbol='SUIUSDT',
        direction='long',
        entry_price=1.0,
        entry_quantity=100,
        atr=0.02,
        current_price=0.98,
        pos={},
    )
    assert result is None
    strategy.binance_client.trade_logger.insert_pnl_summary.assert_not_awaited()


async def test_全部平仓PnL回写失败_不影响主流程():
    """insert_pnl_summary 失败时只记警告，不应抛错。"""
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
        pos={'opened_at': datetime.now(timezone.utc)},
    )
    strategy.binance_client.trade_logger.log_stop_loss.assert_not_awaited()


async def test_已有理论汇总不能作为实盘核验成功():
    strategy = make_strategy()
    strategy._get_actual_pnl_from_binance = AsyncMock(return_value=Decimal('0.17484'))
    strategy.db.fetch_all = AsyncMock(return_value=[{
        'account_id': 'default', 'realized_pnl': Decimal('0.60636'),
        'pnl_source': 'theoretical_estimate',
    }])
    result = await strategy._writeback_pnl_for_full_close(
        symbol='ARIAUSDT', direction='short', entry_price=.23276,
        entry_quantity=31, atr=.01, current_price=.22712,
        pos={'opened_at': datetime.now(timezone.utc)},
    )
    assert result is None


async def test_实盘盈亏待证据时保留持仓和占用(monkeypatch):
    from strategies.hrs.executor import CloseOutcome

    strategy = make_strategy()
    strategy.position_manager = MagicMock()
    strategy.position_manager.cancel_all_orders = AsyncMock()
    strategy._writeback_pnl_for_full_close = AsyncMock(return_value=None)
    strategy._save_state = AsyncMock()
    strategy._reconcile_hrs_reservations = AsyncMock()
    pos = {'opened_at': datetime.now(timezone.utc), 'entry_quantity': 31, 'atr': .01}
    result = CloseOutcome(status='FILLED', closed_qty=Decimal('31'),
                          target_qty=Decimal('31'), success=True)
    completed = await strategy._finalize_close_if_filled(
        'ARIAUSDT', 'short', result, entry_price=.23276,
        exit_price=.22712, quantity=31, close_reason='时间止损', pos=pos,
    )
    assert completed is False
    strategy.position_manager.remove_position.assert_not_called()
    strategy._reconcile_hrs_reservations.assert_not_awaited()
    strategy._save_state.assert_awaited_once()
