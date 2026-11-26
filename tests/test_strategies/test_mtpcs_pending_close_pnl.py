"""MTPCS 已平仓后，延迟实际盈亏仍可跨进程补写。"""
import asyncio
import importlib
import json
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.btc_eth_aggressive import close_evidence


@pytest.fixture
def pending(monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    monkeypatch.delenv('BINANCE_CLASSIC_CUTOVER_MS',raising=False)
    saved={}
    async def write(sql,name,key,payload):
        saved[key]=json.loads(payload);return 'INSERT 0 1'
    async def read(sql,name,cursor):
        return [dict(state_key=k,state_data=v) for k,v in saved.items() if v['status'] in ('pending','awaiting_terminal') and k > cursor][:100]
    db=SimpleNamespace(execute=AsyncMock(side_effect=write),fetch_all=AsyncMock(side_effect=read))
    ledger=MagicMock(account_id='test',strategy_name='btc_eth')
    ledger._record_epoch.return_value='epoch'
    ledger._receipt_in_epoch.return_value=True
    ledger._epoch_trades.side_effect=lambda fills,epoch:fills
    ledger.db.execute=AsyncMock(return_value='UPDATE 1')
    order=dict(symbol='ETHUSDT',side='SELL',orderId=7,origQty='1',executedQty='.4',
               status='CANCELED',reduceOnly=True)
    client=SimpleNamespace(use_unified_account=True,trade_logger=ledger,
        get_order=AsyncMock(return_value=order),get_user_trades=AsyncMock(return_value=[]))
    strategy=SimpleNamespace(binance=client,db_manager=db,instance_id='btc_eth')
    position=SimpleNamespace(direction='LONG',entry_order_id=5,tp2_order_id=6,
                             initial_quantity=D(1),entry_price=D(100))
    fill=dict(symbol='ETHUSDT',side='SELL',orderId=7,id=9,qty='.4',price='110',realizedPnl='4')
    return strategy,position,order,fill,saved


@pytest.mark.asyncio
async def test_delayed_facts_survive_full_position_removal_and_restart(pending):
    s,p,order,fill,saved=pending
    assert not await close_evidence.record_or_defer_close(s,'ETHUSDT',p,order,False)
    assert len(saved)==1 and next(iter(saved.values()))['status']=='pending'
    s.binance.get_user_trades.return_value=[fill]
    restarted=SimpleNamespace(binance=s.binance,db_manager=s.db_manager,instance_id=s.instance_id,positions={})
    assert await close_evidence.retry_pending_close_pnl(restarted)==1
    assert next(iter(saved.values()))['status']=='done'
    assert s.binance.trade_logger.db.execute.await_args.args[1]==D(4)
    assert await close_evidence.retry_pending_close_pnl(restarted)==0
    s.binance.trade_logger.db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_row_pnl_write_stays_pending_until_exact_row_appears(pending):
    s,p,order,fill,saved=pending
    s.binance.get_user_trades.return_value=[fill]
    s.binance.trade_logger.db.execute.return_value='UPDATE 0'
    assert not await close_evidence.record_or_defer_close(s,'ETHUSDT',p,order,True)
    assert next(iter(saved.values()))['status']=='pending'
    s.binance.trade_logger.db.execute.return_value='UPDATE 1'
    assert await close_evidence.retry_pending_close_pnl(s)==1
    assert s.binance.trade_logger.db.execute.await_count==2


@pytest.mark.asyncio
async def test_pending_save_failure_does_not_claim_bookkeeping_complete(pending):
    s,p,order,fill,saved=pending
    s.db_manager.execute.side_effect=OSError('offline DB unavailable')
    assert not await close_evidence.record_or_defer_close(s,'ETHUSDT',p,order,False)
    assert not saved
    s.binance.get_user_trades.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['account','mode','order','direction'])
async def test_pending_retry_rejects_changed_identity(pending,monkeypatch,change):
    s,p,order,fill,saved=pending
    await close_evidence.record_or_defer_close(s,'ETHUSDT',p,order,False)
    s.binance.get_user_trades.return_value=[fill]
    if change=='account':
        monkeypatch.setenv('TRADE_ACCOUNT_ID','other');s.binance.trade_logger.account_id='other'
    elif change=='mode':
        s.binance.use_unified_account=False;monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1000')
    elif change=='direction':next(iter(saved.values()))['entry']['direction']='SHORT'
    else:s.binance.get_order.return_value=dict(order,executedQty='.5')
    assert await close_evidence.retry_pending_close_pnl(s)==0
    assert next(iter(saved.values()))['status']=='pending'
    s.binance.trade_logger.db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('module',['strategies.btc_eth.strategy','strategies.btc_eth_aggressive.strategy'])
async def test_slow_bookkeeping_never_holds_position_protection_cycle(pending, monkeypatch, module):
    source,p,order,fill,saved=pending
    strategy_module=importlib.import_module(module)
    s=object.__new__(strategy_module.BTCEthStrategy)
    s.db_manager=source.db_manager;s.binance=source.binance;s.instance_id=source.instance_id
    s.positions={};s._cycle_count=0;s._unverified_entry_time_symbols=set();s._untracked_stop_retry_symbols=set()
    for name in ('_enforce_allocation_holdings','_check_legacy_untracked_time_stop','_retry_pending_cancellations',
                 '_cleanup_residual_orders','_retry_rebuild_pending','_retry_protection_pending',
                 '_maybe_cleanup_expired_claims'):
        setattr(s,name,AsyncMock())
    monkeypatch.delenv('TRADE_ACCOUNT_ID',raising=False)
    if hasattr(strategy_module,'recover_reserved_protection'):
        monkeypatch.setattr(strategy_module,'recover_reserved_protection',AsyncMock())
    waiting=asyncio.Event()
    async def slow(_):await waiting.wait()
    monkeypatch.setattr(close_evidence,'retry_pending_close_pnl',slow)
    await asyncio.wait_for(s.update_positions(),timeout=.5)
    s._enforce_allocation_holdings.assert_awaited_once()
    s._retry_protection_pending.assert_awaited_once()
    assert not s._pending_close_pnl_task.done()
    waiting.set();await s._pending_close_pnl_task


@pytest.mark.asyncio
async def test_incomplete_first_page_does_not_starve_later_pending_orders(pending):
    s,p,order,fill,saved=pending
    await close_evidence.record_or_defer_close(s,'ETHUSDT',p,order,False)
    payload=next(iter(saved.values()))
    key=next(iter(saved))
    # Persistent malformed rows cannot be settled, but must not monopolize every pass.
    rows=[dict(state_key=f'close-pnl:{i:064x}',state_data={}) for i in range(100)]
    rows.append(dict(state_key=key,state_data=payload))
    rows.sort(key=lambda row:row['state_key'])
    async def page(sql,name,*args):
        cursor=args[0] if args else ''
        return [row for row in rows if row['state_key'] > cursor][:100]
    s.db_manager.fetch_all.side_effect=page
    s.binance.get_user_trades.return_value=[fill]
    assert await close_evidence.retry_pending_close_pnl(s)==0
    assert await close_evidence.retry_pending_close_pnl(s)==1
    assert saved[key]['status']=='done'


@pytest.mark.asyncio
async def test_unconfirmed_exit_settles_after_risk_exit_removed_position(pending):
    s,p,order,fill,saved = pending
    close_evidence.remember_close_order(s, 'ETHUSDT', p, order['orderId'], D(1), 'TP1')
    assert await close_evidence.queue_unconfirmed_close(s, 'ETHUSDT', p, order['orderId'], False)
    assert next(iter(saved.values()))['status'] == 'awaiting_terminal'
    s.binance.get_user_trades.return_value = [fill]
    restarted = SimpleNamespace(binance=s.binance, db_manager=s.db_manager, instance_id=s.instance_id, positions={})
    assert await close_evidence.retry_pending_close_pnl(restarted) == 1
    assert next(iter(saved.values()))['status'] == 'done'
    assert s.binance.trade_logger.db.execute.await_args.args[1] == D(4)
