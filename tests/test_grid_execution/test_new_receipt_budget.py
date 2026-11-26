"""无成交回执不重复消耗 REST；成交与终态仍保留保护链。"""
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.store import DecisionStore, migrate


@pytest.fixture
def engine(tmp_path):
    path = tmp_path / 'receipt.sqlite'
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch('decision', stop_loss_low=D(1800), stop_loss_high=D(2200))
    for i in range(11):
        store.prepare_order(epoch, i, 'ENTRY', 'BUY', D(1900+i), D('.02'), f'eg-{i}')
        store.record_order(f'eg-{i}', str(i+100), 'NEW', D(0))
    store.set_engine_state('ACTIVE')
    exchange = MagicMock(position=AsyncMock(return_value=[{'positionSide':'BOTH','positionAmt':'0'}]))
    exchange.open_orders = AsyncMock()
    exchange.open_stops = AsyncMock(return_value=[])
    result = GridEngine(store, exchange)
    result.reconcile = AsyncMock(return_value=True)
    result.exit_all = AsyncMock(return_value=False)
    result._secure_locked_epoch = AsyncMock(return_value=False)
    result.trailing = MagicMock(guard=AsyncMock(return_value=True), pending=MagicMock(return_value=False))
    result.pending_obligations = MagicMock(return_value=[])
    yield result, epoch
    store.close()


def event(i=0, status='NEW', kind='NEW', qty='0', total='0'):
    return {'e':'ORDER_TRADE_UPDATE', 'o':{'s':'ETHUSDT','c':f'eg-{i}',
        'i':100+i,'S':'BUY','x':kind,'X':status,'l':qty,'z':total,'L':'1900','t':1}}


async def apply(engine, epoch, item):
    return await engine.on_order_update(item, epoch_id=epoch, levels=(), filters=None,
                                       stop_loss_low=D(1800), stop_loss_high=D(2200))


@pytest.mark.asyncio
async def test_eleven_owned_new_receipts_have_no_redundant_rest(engine):
    e, epoch = engine
    for i in range(11):
        assert await apply(e, epoch, event(i))
    e.exchange.position.assert_not_awaited()
    e.exchange.open_orders.assert_not_awaited()
    e.exchange.open_stops.assert_not_awaited()
    e.reconcile.assert_not_awaited()
    e.trailing.guard.assert_not_awaited()
    assert all(o['status']=='NEW' and D(o['executed_qty'])==0 for o in e.store.all_orders())


@pytest.mark.asyncio
@pytest.mark.parametrize('status,kind,qty,total', [
    ('PARTIALLY_FILLED','TRADE','.01','.01'), ('FILLED','TRADE','.02','.02'),
    ('CANCELED','CANCELED','0','0'), ('EXPIRED','EXPIRED','0','0')])
async def test_fill_or_terminal_still_protects_and_reconciles(engine,status,kind,qty,total):
    e, epoch = engine
    assert await apply(e, epoch, event(status=status,kind=kind,qty=qty,total=total))
    e.exchange.position.assert_awaited_once()
    e.exchange.open_stops.assert_awaited_once()
    e.reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_locked_receipt_cannot_skip_cleanup(engine):
    e, epoch = engine
    e.store.set_engine_state('LOCKED', 'risk')
    assert not await apply(e, epoch, event())
    e._secure_locked_epoch.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_receipt_wrong_exchange_identity_locks(engine):
    e, epoch = engine
    row = event(); row['o']['i']=999
    assert not await apply(e, epoch, row)
    assert e.store.engine_state()['state']=='LOCKED'
    e._secure_locked_epoch.assert_awaited_once()
