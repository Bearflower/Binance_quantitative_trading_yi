"""账户切换后的补录只能查询当前模式，历史缺口保持可见。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_tuner.reconciler import nightly_exit as mod

BOUNDARY = datetime(2026, 11, 25, 4, 54, 24, tzinfo=timezone.utc)
CUTOVER = int(BOUNDARY.timestamp() * 1000)
GAP = dict(symbol='ETHUSDT', order_id=123, side='BUY')


def reconciler(pm=False, cutover=CUTOVER):
    return SimpleNamespace(account_id='default', pm=pm, cutover=cutover,
        excluded_strategies=[], state_schema='public', state_table='pnl_state',
        db=MagicMock(fetch_all=AsyncMock(return_value=[])),
        binance=SimpleNamespace(use_unified_account=pm, get_algo_order=AsyncMock(), get_order=AsyncMock()),
        _ensure_state_table=AsyncMock())


@pytest.mark.asyncio
async def test_classic_without_boundary_fails_before_db_or_exchange():
    rec = reconciler(cutover=None)
    with pytest.raises(ValueError, match='切换'):
        await mod.nightly_exit_repair(rec)
    rec._ensure_state_table.assert_not_awaited()
    rec.db.fetch_all.assert_not_awaited()
    rec.binance.get_algo_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_mode_configuration_mismatch_fails_before_io():
    rec = reconciler()
    rec.binance.use_unified_account = True
    with pytest.raises(ValueError, match='模式'):
        await mod.repair_order(rec, GAP)
    rec.db.fetch_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_mode_fill_never_reaches_exchange():
    rec = reconciler()
    rec.db.fetch_all.return_value = [dict(trade_time_utc=BOUNDARY-timedelta(milliseconds=1))]
    with pytest.raises(ValueError, match='当前账户模式'):
        await mod.repair_order(rec, GAP)
    rec.binance.get_algo_order.assert_not_awaited()
    rec.binance.get_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_current_mode_fill_never_reaches_exchange():
    rec = reconciler()
    with pytest.raises(ValueError, match='当前账户模式'):
        await mod.repair_order(rec, GAP)
    rec.binance.get_algo_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_classic_boundary_and_history_remain_separate():
    rec = reconciler()
    rec.db.fetch_all.side_effect = [[GAP], [dict(GAP,order_id=9)]]
    with patch.object(mod, 'repair_order', AsyncMock(return_value=True)) as repair, patch.object(mod,'log') as logger:
        await mod.nightly_exit_repair(rec)
    repair.assert_awaited_once_with(rec,GAP)
    current,historical = rec.db.fetch_all.await_args_list
    assert current.args[2] == f'classic:{CUTOVER}:default'
    assert current.args[3].lower == BOUNDARY and current.args[3].lower_inc
    assert current.args[4].lower == BOUNDARY.replace(tzinfo=None)+timedelta(hours=8)
    assert historical.args[2] == 'default'
    assert historical.args[3].upper == BOUNDARY and not historical.args[3].upper_inc
    logger.warning.assert_any_call('历史 PM 成交归属待补证据', historical_pending=1, status='BLOCKED_DATA')
    logger.info.assert_called_with('凌晨成交补录完成', inserted=1, unresolved=0, historical_pending=1)


@pytest.mark.asyncio
async def test_pm_without_cutover_retains_unbounded_existing_mode():
    rec=reconciler(pm=True,cutover=None)
    await mod.nightly_exit_repair(rec)
    assert rec.db.fetch_all.await_count == 1
    args=rec.db.fetch_all.await_args.args
    assert args[2]=='default' and args[3].lower is None and args[3].upper is None
