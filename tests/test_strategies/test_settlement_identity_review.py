"""发布集成：结算完整量及减仓进度不能跨账户复用。"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.mtpcs_close_progress import load_close_progress, save_close_progress
from strategies.new_coin.strategy import NewCoinStrategy


@pytest.fixture
def newcoin(monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID', 'review-account')
    monkeypatch.delenv('BINANCE_CLASSIC_CUTOVER_MS', raising=False)
    n=object.__new__(NewCoinStrategy)
    n.my_record_name='new_coin'
    opened=datetime.now(timezone.utc)-timedelta(hours=1)
    n.positions={'AAVEUSDT':{'order_id':100,'entry_time':opened.isoformat(),'quantity':0}}
    entry=dict(position_id='AAVEUSDT_100',quantity='1',metadata={'entry_order_id':100},
               opened_at=opened.replace(tzinfo=None),account_id='review-account',
               executed_at=opened, strategy='new_coin', side='SELL', symbol='AAVEUSDT',order_id='100')
    exits=[{'order_id':'101'}]
    n.db=MagicMock(fetch_all=AsyncMock(side_effect=lambda sql,*args: [entry] if 'short_positions' in sql else exits))
    n.binance_client=MagicMock(use_unified_account=True)
    def order(symbol, oid):
        qty={100:'1',101:'.3',102:'.7'}[oid]
        return dict(symbol=symbol,side='SELL' if oid==100 else 'BUY',orderId=oid,
                    status='FILLED',origQty=qty,executedQty=qty,time=int(opened.timestamp()*1000))
    n.binance_client.get_order=AsyncMock(side_effect=order)
    n.binance_client.get_user_trades=AsyncMock(side_effect=lambda symbol,order_id,**kwargs:[
        dict(id=int(order_id)+1000,orderId=int(order_id),symbol=symbol,side='BUY',
             qty='.3' if order_id=='101' else '.7',realizedPnl='3' if order_id=='101' else '-14')])
    return n, entry, exits


@pytest.mark.asyncio
async def test_missing_last_exit_record_cannot_settle_partial_tp(newcoin):
    n,entry,exits=newcoin
    assert await n._get_position_pnl('AAVEUSDT',100) is None
    exits.append({'order_id':'102'})
    assert await n._get_position_pnl('AAVEUSDT',100)==D('-11')


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['no_order','wrong_account','wrong_entry','unknown_quantity'])
async def test_untrusted_original_quantity_keeps_settlement_pending(newcoin,change):
    n,entry,exits=newcoin;exits.append({'order_id':'102'})
    if change=='no_order':n.positions['AAVEUSDT'].pop('order_id')
    elif change=='wrong_account':entry['account_id']='other'
    elif change=='wrong_entry':entry['metadata']['entry_order_id']=99
    else:entry['quantity']='NaN'
    assert await n._get_position_pnl('AAVEUSDT',100) is None


def progress_fixture():
    data={}
    async def execute(sql,strategy,key,payload):data[key]=json.loads(payload)
    async def fetch(sql,strategy,key):return {'state_data':data[key]} if key in data else None
    s=SimpleNamespace(instance_id='same-instance',db_manager=MagicMock(
        execute=AsyncMock(side_effect=execute),fetch_one=AsyncMock(side_effect=fetch)),
        binance=SimpleNamespace(use_unified_account=True,trade_logger=SimpleNamespace(account_id='review-account')))
    def position():return SimpleNamespace(direction='LONG',entry_order_id=7,tp2_order_id=9,
        initial_quantity=D(1),close_progress={},_close_progress_loaded=False)
    return s,position,data


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['account','epoch','direction','missing_binding','entry'])
async def test_progress_rejects_same_numeric_order_in_different_identity(monkeypatch,change):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','review-account')
    monkeypatch.delenv('BINANCE_CLASSIC_CUTOVER_MS',raising=False)
    s,position,data=progress_fixture();first=position();first.close_progress={'TP1':D('.1')}
    assert await save_close_progress(s,'ETHUSDT',first)
    restored=position()
    if change=='account':
        monkeypatch.setenv('TRADE_ACCOUNT_ID','other');s.binance.trade_logger.account_id='other'
    elif change=='epoch':
        monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1000');s.binance.use_unified_account=False
    elif change=='direction':restored.direction='SHORT'
    elif change=='entry':restored.entry_order_id=8
    else:data['close-progress:ETHUSDT'].pop('context',None)
    assert not await load_close_progress(s,'ETHUSDT',restored)
    assert restored.close_progress=={}


@pytest.mark.asyncio
async def test_progress_same_bound_identity_restores(monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','review-account')
    monkeypatch.delenv('BINANCE_CLASSIC_CUTOVER_MS',raising=False)
    s,position,data=progress_fixture();first=position();first.close_progress={'TP1':D('.1')}
    assert await save_close_progress(s,'ETHUSDT',first)
    restored=position();assert await load_close_progress(s,'ETHUSDT',restored)
    assert restored.close_progress=={'TP1':D('.1')}


@pytest.mark.asyncio
async def test_new_lot_with_distinct_entry_and_protection_starts_fresh(monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','review-account')
    monkeypatch.delenv('BINANCE_CLASSIC_CUTOVER_MS',raising=False)
    s,position,data=progress_fixture();first=position();first.close_progress={'TP1':D('.1')}
    assert await save_close_progress(s,'ETHUSDT',first)
    new=position();new.entry_order_id=8;new.tp2_order_id=10;new.direction='SHORT'
    assert await load_close_progress(s,'ETHUSDT',new)
    assert new.close_progress=={}
