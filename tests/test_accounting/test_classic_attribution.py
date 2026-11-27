from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import pytest
from shared.accounting.classic_attribution import classic_plan, ClassicAttributionBlocked


def facts(monkeypatch):
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1795582464000')
    start=datetime(2026,11,25,5,33,tzinfo=timezone.utc)
    policy=SimpleNamespace(account_id='default',classic_flat_at=start,receipt_sha256='a'*64)
    end=start+timedelta(hours=1)
    fills=[dict(account_id='default',symbol='ETHUSDT',trade_id=1,order_id=9,side='BUY',qty=Decimal('1'),commission=Decimal('.1'),commission_asset='USDT',realized_pnl=Decimal(0),trade_time_utc=start+timedelta(seconds=1))]
    trades=[dict(id=4,account_id='default',symbol='ETHUSDT',order_id='9',strategy='btc_eth_aggressive',executed_at=start+timedelta(seconds=1))]
    income=[dict(account_id='default',symbol='ETHUSDT',tran_id=12,income_type='FUNDING_FEE',income=Decimal('-.01'),asset='USDT',trade_id=None,event_time_utc=start+timedelta(minutes=5))]
    cursors=[dict(account_id='default',source='fapi/userTrades',symbol='ETHUSDT',income_type=None,window_start_ms=int(start.timestamp()*1000),covered_until_ms=int(end.timestamp()*1000),page_complete=True,request_status='ok')]
    for kind in ['REALIZED_PNL','COMMISSION','FUNDING_FEE']:
        cursors.append(dict(cursors[0],source='fapi/income',symbol=None,income_type=kind))
    data=dict(incomes=income,fills=fills,trades=trades,cursors=cursors,conditions=[],manual=[],existing=[],owners=[])
    return policy,end,data


def test_classic_funding_uses_native_replay_without_pm_final(monkeypatch):
    p,end,d=facts(monkeypatch)
    plan=classic_plan(p,end,d,{'ETHUSDT':Decimal(1)},[])
    assert plan['derived_rows'][0]['strategy']=='btc_eth_aggressive'
    assert plan['derived_rows'][0]['income']==Decimal('-.01')


def test_existing_funding_proof_is_preserved_when_independent_replay_agrees(monkeypatch):
    p,end,d=facts(monkeypatch)
    d['existing']=[dict(d['incomes'][0],strategy='btc_eth_aggressive',method='verified_position_interval',evidence={'old_closed_lot':'proof'},algorithm_version=1)]
    plan=classic_plan(p,end,d,{'ETHUSDT':Decimal(1)},[])
    assert plan['derived_rows']==[] and not plan['conflicts']


@pytest.mark.parametrize('fault',['coverage','position','owner','income_identity','owner_conflict'])
def test_missing_or_conflicting_proof_never_invents_owner(monkeypatch,fault):
    p,end,d=facts(monkeypatch);qty={'ETHUSDT':Decimal(1)}
    if fault=='coverage':d['cursors'][0]['page_complete']=False
    elif fault=='position':qty['ETHUSDT']=Decimal(2)
    elif fault=='owner':d['trades']=[]
    else:
        d['existing']=[dict(d['incomes'][0],strategy='hrs' if fault=='owner_conflict' else 'btc_eth_aggressive',income=Decimal('2') if fault=='income_identity' else Decimal('-.01'),method='verified_position_interval',evidence={},algorithm_version=1)]
    if fault in ['coverage','income_identity','owner_conflict']:
        with pytest.raises(ClassicAttributionBlocked):classic_plan(p,end,d,qty,[])
    else:
        plan=classic_plan(p,end,d,qty,[])
        assert not plan['derived_rows'] and plan['rejected']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[None,'revoked','fresh_coverage','new_symbol','position_gap','uid','wrong_client','no_policy','watermark_stale','partial','caller_policy_changed','exact_fee'])
async def test_runtime_rechecks_inside_serializable_before_write(monkeypatch,failure):
    from shared.accounting import classic_attribution as m
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    p,end,data=facts(monkeypatch)
    if failure=='exact_fee':data['incomes'][0].update(income_type='COMMISSION',trade_id=1,income=Decimal('-.1'),event_time_utc=data['fills'][0]['trade_time_utc'])
    p.payload={'classic_cutover_ms':1795582464000}
    p.exchange_uid_sha256='a'*64
    p.classic_flat_evidence_sha256='b'*64
    phases=[]
    income_lock_held=False
    @asynccontextmanager
    async def locked(connection, account):
        nonlocal income_lock_held
        assert account=='default' and connection is db_connection
        assert not income_lock_held
        income_lock_held=True
        phases.append('income_lock')
        try:yield
        finally:
            income_lock_held=False
            phases.append('income_unlock')
    class Connection:
        @asynccontextmanager
        async def transaction(self,**kw):
            assert income_lock_held
            phases.append(kw['isolation']);yield
        async def fetch(self,sql,*args):
            phases.append('read:'+('orders' if 'order_strategy_evidence' in sql else 'source'))
            if sql==m._TRADE_SQL:return [] if failure=='partial' else data['trades']
            if sql==m._CLASSIC_CONDITIONS:return []
            if sql==m._MANUAL_SQL:return []
            if sql==m._EXISTING_SQL:return []
            return []
    connection=Connection()
    db_connection=connection
    class Pool:
        @asynccontextmanager
        async def acquire(self):yield connection
    db=SimpleNamespace(pool=Pool())
    class Client:
        use_unified_account=False;testnet=False;base_url='https://fapi.binance.com'
    monkeypatch.setattr(m,'ReadOnlyBinanceClient',Client)
    monkeypatch.setattr(m.time,'time',lambda:end.timestamp()+1)
    async def uid(*args,**kwargs):
        if failure=='uid':raise ValueError('wrong_uid')
        return p.exchange_uid_sha256
    monkeypatch.setattr(m,'signed_spot_uid_sha256',uid)
    async def read_policy(target,*args,**kwargs):
        return None if failure=='no_policy' or (failure=='revoked' and target is connection) else p
    monkeypatch.setattr(m,'read_history_acceptance',read_policy)
    monkeypatch.setattr(m,'income_account_lock',locked)
    monkeypatch.setattr(m,'require_income_integrity',AsyncMock())
    counter=0
    async def facts_read(*args):
        nonlocal counter
        from copy import deepcopy
        counter+=1; d=deepcopy(data)
        if failure=='watermark_stale':
            for row in d['cursors']:row['covered_until_ms']-=300000
        if counter==2 and failure=='partial':d['trades']=[]
        if counter==2 and failure=='fresh_coverage':d['cursors'][0]['page_complete']=False
        if counter==2 and failure=='new_symbol':d['fills'].append(dict(d['fills'][0],symbol='BTCUSDT'))
        return d
    monkeypatch.setattr(m,'_facts',facts_read)
    async def position(*args):
        assert not income_lock_held
        phases.append('signed_position')
        return (None,'post_cutoff_fill',None) if failure=='position_gap' else (Decimal(1),None,{'cutoff':1})
    monkeypatch.setattr(m,'verified_position_at_cutoff',position)
    monkeypatch.setattr(m,'_fetch_links',AsyncMock(return_value=[]))
    async def write(conn,plan,**kwargs):
        phases.append('write')
        assert 'serializable' in phases and phases.index('signed_position')<phases.index('serializable')
        if plan['derived_rows'] and failure!='exact_fee':assert plan['derived_rows'][0]['evidence']['classic_history_acceptance']['receipt_sha256']==p.receipt_sha256
        return {'income_rows_seen':len(plan['derived_rows'])}
    writer=AsyncMock(side_effect=write);monkeypatch.setattr(m,'apply_plan',writer)
    if failure in ['revoked','fresh_coverage','new_symbol','uid','wrong_client','caller_policy_changed']:
        with pytest.raises(ValueError):await m.reconcile_classic_attribution(db,object() if failure=='wrong_client' else Client(),acceptance=SimpleNamespace(receipt_sha256='other') if failure=='caller_policy_changed' else None)
        writer.assert_not_called()
    else:
        result=await m.reconcile_classic_attribution(db,Client())
        if failure in ['position_gap','no_policy','watermark_stale']:assert result['ready'] is False;writer.assert_not_called()
        elif failure=='partial':
            assert result['ready'] is False and result['pending']
            assert result['write_result']['income_rows_seen']==0
        else:
            assert result['ready'] and result['pending']==[]
            assert result['write_result']['income_rows_seen']==1
            writer.assert_awaited_once()
    assert not income_lock_held
    assert phases.count('income_lock')==phases.count('income_unlock')


def test_unproven_inventory_is_pending_even_without_income(monkeypatch):
    p,end,d=facts(monkeypatch);d['incomes']=[];d['trades']=[]
    plan=classic_plan(p,end,d,{'ETHUSDT':Decimal(1)},[])
    assert not plan['derived_rows'] and plan['rejected'][0]['scope']=='position_replay'


def test_existing_order_owner_conflict_and_flat_boundary_are_rejected(monkeypatch):
    p,end,d=facts(monkeypatch)
    d['owners']=[dict(account_id='default',symbol='ETHUSDT',order_id=9,strategy='hrs')]
    with pytest.raises(ClassicAttributionBlocked,match='order_owner_conflict'):
        classic_plan(p,end,d,{'ETHUSDT':Decimal(1)},[])
    d['owners']=[];d['fills'][0]['trade_time_utc']=p.classic_flat_at
    with pytest.raises(ClassicAttributionBlocked,match='boundary_collision'):
        classic_plan(p,end,d,{'ETHUSDT':Decimal(1)},[])


def test_position_scope_and_existing_order_proof_are_explicit(monkeypatch):
    p,end,d=facts(monkeypatch)
    with pytest.raises(ClassicAttributionBlocked,match='position_scope'):
        classic_plan(p,end,d,{},[])
    d['owners']=[dict(account_id='default',symbol='ETHUSDT',order_id=9,strategy='btc_eth_aggressive',evidence={'prior':'unchanged'})]
    assert classic_plan(p,end,d,{'ETHUSDT':Decimal(1)},[])['order_rows']==[]


@pytest.mark.asyncio
async def test_fact_reader_uses_fixed_account_window_without_fact_writes(monkeypatch):
    from shared.accounting import classic_attribution as m
    p,end,d=facts(monkeypatch)
    class Connection:
        async def fetch(self,sql,*args):
            assert args[0]=='default' and sql.lstrip().startswith('SELECT')
            if sql==m._INCOME_SQL:
                assert args[1:]==(p.classic_flat_at,end)
                return d['incomes']
            if sql==m._CURSOR_SQL:
                assert args[1:]==(int(p.classic_flat_at.timestamp()*1000),int(end.timestamp()*1000))
                return d['cursors']
            assert 'execution_fact_fill' in sql and args[1:]==(p.classic_flat_at,end)
            return d['fills']
    result=await m._facts(Connection(),'default',p.classic_flat_at,end)
    assert result=={k:d[k] for k in ('incomes','fills','cursors')}
