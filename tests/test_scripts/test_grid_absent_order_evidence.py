"""Release proof for explicit maker denials and audited historical absence."""
from copy import deepcopy
from decimal import Decimal as D
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.binance_api import BinanceAPIError
from scripts import grid_release_guard as guard
from strategies.grid.execution import startup_recovery
from strategies.grid.execution.store import DecisionStore, migrate


NOW = 1_800_000_000_000


async def journal(tmp_path, monkeypatch, status):
    monkeypatch.setattr(guard.time, 'time', lambda: (NOW - 600_000) / 1000)
    path = tmp_path / 'journal.sqlite'; migrate(path)
    store = DecisionStore(path)
    store.bind_account('classic:expected')
    epoch = store.create_epoch('signal')
    store.prepare_order(epoch, 0, 'ENTRY', 'BUY', D(1000), D('.02'), 'eg-peer')
    store.record_order('eg-peer', '100', 'CANCELED', D(0))
    store.prepare_order(epoch, 1, 'ENTRY', 'SELL', D(1100), D('.02'), 'eg-missing')
    peer = dict(symbol='ETHUSDT', clientOrderId='eg-peer', orderId=100, side='BUY',
                status='CANCELED', origQty='.02', executedQty='0', price='1000', time=NOW-600_000)
    records = {'eg-peer': peer}
    async def query(cid):
        if cid in records:
            return dict(records[cid])
        raise BinanceAPIError(-2013, 'absent')
    exchange = SimpleNamespace(
        journal_identity=AsyncMock(return_value='classic:expected'),
        query_order=AsyncMock(side_effect=query),
        position=AsyncMock(return_value=[dict(symbol='ETHUSDT', positionSide='BOTH', positionAmt='0')]),
        open_orders=AsyncMock(return_value=[]), open_stops=AsyncMock(return_value=[]),
        trade_history=AsyncMock(return_value=[]), order_trades=AsyncMock(return_value=[]),
        order_history=AsyncMock(side_effect=lambda start,end: deepcopy(list(records.values()))),
        records=records)
    if status == 'ABSENT':
        store.set_engine_state('LOCKED', 'cancel result unknown during exit')
        first = await startup_recovery.capture(store, exchange, NOW-10_000)
        second = await startup_recovery.capture(store, exchange, NOW-5000)
        startup_recovery.commit(store, first, second, NOW-4999)
    else:
        store.record_maker_rejection('eg-missing')
        store.close_epoch(epoch)
        store.set_engine_state('FLAT')
    monkeypatch.setattr(guard.time, 'time', lambda: NOW / 1000)
    exchange.query_order.reset_mock()
    return store, exchange, epoch


def audit_key(status, epoch):
    return 'order_rejection:eg-missing' if status == 'REJECTED' else f'startup_absence_recovery:{epoch}'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['REJECTED','ABSENT'])
async def test_original_audit_plus_fresh_authoritative_absence_is_readonly(tmp_path,monkeypatch,status):
    store,exchange,epoch = await journal(tmp_path,monkeypatch,status)
    try:
        before = guard.journal_digest(store.conn)
        await guard.complete_history(store,exchange)
        assert guard.journal_digest(store.conn) == before
        assert any(call.args == ('eg-missing',) for call in exchange.query_order.await_args_list)
        exchange.order_history.assert_awaited()
        exchange.trade_history.assert_awaited()
        assert store.get_order('eg-missing')['status'] == status
    finally:
        store.close()


@pytest.mark.asyncio
async def test_historical_absence_does_not_require_canceling_other_owned_orders_first(tmp_path,monkeypatch):
    store,exchange,_ = await journal(tmp_path,monkeypatch,'ABSENT')
    try:
        epoch = store.create_epoch('new-signal')
        store.prepare_order(epoch,0,'ENTRY','BUY',D(900),D('.02'),'eg-live')
        store.record_order('eg-live','101','NEW',D(0))
        exchange.records['eg-live'] = dict(symbol='ETHUSDT',clientOrderId='eg-live',orderId=101,
            side='BUY',status='NEW',origQty='.02',executedQty='0',price='900',time=NOW)
        await guard.complete_history(store,exchange)
        assert store.get_order('eg-live')['status'] == 'NEW'
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['REJECTED','ABSENT'])
@pytest.mark.parametrize('fault', ['missing_audit','wrong_role','venue_id','execution','wrong_identity',
    'query_found','query_timeout','query_other_error','foreign_order','missing_peer','foreign_trade',
    'nonflat','unproven_prepared'])
async def test_unproven_absence_never_bypasses_complete_history(tmp_path,monkeypatch,status,fault):
    store,exchange,epoch = await journal(tmp_path,monkeypatch,status)
    try:
        if fault=='missing_audit': store.conn.execute('DELETE FROM schema_meta WHERE key=?',(audit_key(status,epoch),))
        if fault=='wrong_role': store.conn.execute("UPDATE order_intent SET role='REVERSE' WHERE client_order_id='eg-missing'")
        if fault=='venue_id': store.conn.execute("UPDATE order_intent SET exchange_order_id='999' WHERE client_order_id='eg-missing'")
        if fault=='execution': store.conn.execute("UPDATE order_intent SET executed_qty='.01' WHERE client_order_id='eg-missing'")
        if fault=='wrong_identity': exchange.journal_identity.return_value='classic:other'
        if fault.startswith('query_'):
            original = exchange.query_order.side_effect
            async def query(cid):
                if cid!='eg-missing': return await original(cid)
                if fault=='query_found': return {}
                if fault=='query_timeout': raise TimeoutError('no response')
                raise BinanceAPIError(-1003,'rate limit')
            exchange.query_order.side_effect=query
        if fault=='foreign_order': exchange.records['foreign']=dict(exchange.records['eg-peer'],clientOrderId='foreign',orderId=999)
        if fault=='missing_peer': exchange.order_history.side_effect=lambda start,end: []
        if fault=='foreign_trade': exchange.trade_history.return_value=[dict(orderId=999,id=1,symbol='ETHUSDT',side='BUY',qty='.01',price='1000',time=NOW-100)]
        if fault=='nonflat': exchange.position.return_value[0]['positionAmt']='.01'
        if fault=='unproven_prepared': store.conn.execute("UPDATE order_intent SET status='PREPARED' WHERE client_order_id='eg-missing'")
        before = guard.journal_digest(store.conn)
        with pytest.raises(Exception):
            await guard.complete_history(store,exchange)
        assert guard.journal_digest(store.conn)==before
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['code','first_identity','second_cid','epoch','history_hash',
    'journal_hash','same_sample','stale_commit','cutoff','missing_peer_history'])
async def test_absent_status_requires_its_exact_two_sample_audit(tmp_path,monkeypatch,fault):
    store,exchange,epoch = await journal(tmp_path,monkeypatch,'ABSENT')
    try:
        key=audit_key('ABSENT',epoch)
        evidence=json.loads(store.conn.execute('SELECT value FROM schema_meta WHERE key=?',(key,)).fetchone()[0])
        if fault=='code': evidence['first']['query_code']=-5022
        if fault=='first_identity': evidence['first']['identity']='classic:other'
        if fault=='second_cid': evidence['second']['client_order_id']='other'
        if fault=='epoch': evidence['first']['epoch_id']=epoch+1
        if fault=='history_hash': evidence['second']['history_sha256']='0'*64
        if fault=='journal_hash': evidence['second']['journal_sha256']='0'*64
        if fault=='same_sample': evidence['second']['checked_at_ms']=evidence['first']['checked_at_ms']
        if fault=='stale_commit': evidence['recovered_at_ms']=evidence['second']['checked_at_ms']+31_000
        if fault=='cutoff': store.conn.execute("DELETE FROM schema_meta WHERE key='position_recovery_cutoff_ms'")
        if fault=='missing_peer_history':
            for sample in (evidence['first'],evidence['second']):
                sample['history']['orders']=[]
                sample['history_sha256']=startup_recovery.fingerprint(sample['history'])
        store.conn.execute('UPDATE schema_meta SET value=? WHERE key=?',(json.dumps(evidence),key))
        with pytest.raises(Exception): await guard.complete_history(store,exchange)
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('api_code',-2013),('reason','UNKNOWN'),('observed_at_ms',NOW+1)])
async def test_rejected_status_cannot_be_invented_from_get_absence(tmp_path,monkeypatch,field,value):
    store,exchange,epoch = await journal(tmp_path,monkeypatch,'REJECTED')
    try:
        key=audit_key('REJECTED',epoch)
        evidence=json.loads(store.conn.execute('SELECT value FROM schema_meta WHERE key=?',(key,)).fetchone()[0])
        evidence[field]=value
        store.conn.execute('UPDATE schema_meta SET value=? WHERE key=?',(json.dumps(evidence),key))
        with pytest.raises(Exception): await guard.complete_history(store,exchange)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_legacy_minimal_rejection_audit_requires_the_same_fresh_proof(tmp_path,monkeypatch):
    store,exchange,epoch = await journal(tmp_path,monkeypatch,'REJECTED')
    try:
        key=audit_key('REJECTED',epoch)
        evidence=json.loads(store.conn.execute('SELECT value FROM schema_meta WHERE key=?',(key,)).fetchone()[0])
        evidence.pop('intent')
        store.conn.execute('UPDATE schema_meta SET value=? WHERE key=?',(json.dumps(evidence),key))
        await guard.complete_history(store,exchange)
        exchange.order_history.assert_awaited_once()
        exchange.trade_history.assert_awaited_once()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_first_order_definitely_rejected_can_have_no_other_order_history(tmp_path,monkeypatch):
    store,exchange,_ = await journal(tmp_path,monkeypatch,'REJECTED')
    try:
        store.conn.execute("DELETE FROM order_intent WHERE client_order_id='eg-peer'")
        exchange.records.clear()
        await guard.complete_history(store,exchange)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_nonordinary_executed_orders_still_require_complete_trade_quantities(tmp_path,monkeypatch):
    store,exchange,epoch = await journal(tmp_path,monkeypatch,'REJECTED')
    try:
        for i,side in enumerate(('BUY','SELL'),200):
            cid=f'eg-close-{i}'
            store.prepare_order(epoch,-i,'EXIT',side,D(1),D('.02'),cid)
            store.record_order(cid,str(i),'FILLED',D('.02'))
            exchange.records[cid]=dict(symbol='ETHUSDT',clientOrderId=cid,orderId=i,side=side,
                status='FILLED',origQty='.02',executedQty='.02',price='0',time=NOW)
        with pytest.raises(guard.GuardError):
            await guard.complete_history(store,exchange)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_new_rejection_audit_binds_the_durable_intent(tmp_path,monkeypatch):
    store,exchange,_ = await journal(tmp_path,monkeypatch,'REJECTED')
    try:
        store.conn.execute("UPDATE order_intent SET quantity='.03' WHERE client_order_id='eg-missing'")
        with pytest.raises(guard.GuardError,match='intent differs'):
            await guard.complete_history(store,exchange)
    finally:
        store.close()
