from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.binance_api import BinanceAPIError
from strategies.grid.execution import startup_recovery as recovery
from strategies.grid.execution.store import DecisionStore, migrate


NOW = 1_800_000_000_000


def fixture(tmp_path):
    p = tmp_path/'journal.sqlite3'
    migrate(p)
    store = DecisionStore(p)
    store.bind_account('classic:expected')
    epoch = store.create_epoch('signal')
    store.conn.execute('UPDATE grid_epoch SET created_at_ms=? WHERE id=?', (NOW-600000, epoch))
    store.prepare_order(epoch, 0, 'ENTRY', 'BUY', Decimal(1000), Decimal('.02'), 'eg-known')
    store.record_order('eg-known', '100', 'CANCELED', Decimal(0))
    store.prepare_order(epoch, 1, 'ENTRY', 'SELL', Decimal(1100), Decimal('.02'), 'eg-missing')
    store.set_engine_state('LOCKED', 'cancel result unknown during exit')
    exchange = SimpleNamespace(
        journal_identity=AsyncMock(return_value='classic:expected'),
        query_order=AsyncMock(side_effect=BinanceAPIError(-2013, 'absent')),
        position=AsyncMock(return_value=[dict(symbol='ETHUSDT', positionSide='BOTH', positionAmt='0')]),
        open_orders=AsyncMock(return_value=[]), open_stops=AsyncMock(return_value=[]),
        trade_history=AsyncMock(return_value=[]),
        order_history=AsyncMock(return_value=[dict(symbol='ETHUSDT', side='BUY', orderId=100,
            clientOrderId='eg-known', status='CANCELED', origQty='.02', executedQty='0')]))
    return store, exchange


@pytest.mark.asyncio
async def test_absence_proof_is_readonly_until_two_matching_samples_commit(tmp_path):
    store, exchange = fixture(tmp_path)
    before = recovery.snapshot(store)
    a = await recovery.capture(store, exchange, NOW)
    b = await recovery.capture(store, exchange, NOW+5001)
    assert recovery.snapshot(store) == before
    recovery.commit(store, a, b, NOW+5002)
    assert store.engine_state()['state'] == 'FLAT'
    assert store.latest_epoch()['status'] == 'CLOSED'
    assert store.get_order('eg-missing')['status'] == 'ABSENT'
    assert store.get_order('eg-missing')['exchange_order_id'] is None
    assert store.conn.execute("SELECT value FROM schema_meta WHERE key='position_recovery_cutoff_ms'").fetchone()[0] == str(NOW+5002)
    assert store.conn.execute("SELECT value FROM schema_meta WHERE key='startup_absence_recovery:1'").fetchone()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['account', 'query_error', 'found', 'position', 'orders', 'stops', 'missing_history', 'foreign_history', 'fill'])
async def test_absence_proof_rejects_incomplete_or_nonflat_evidence(tmp_path, fault):
    store, exchange = fixture(tmp_path)
    if fault == 'account': exchange.journal_identity.return_value = 'classic:other'
    if fault == 'query_error': exchange.query_order.side_effect = TimeoutError()
    if fault == 'found': exchange.query_order.side_effect = None; exchange.query_order.return_value = {}
    if fault == 'position': exchange.position.return_value[0]['positionAmt'] = '.02'
    if fault == 'orders': exchange.open_orders.return_value = [{}]
    if fault == 'stops': exchange.open_stops.return_value = [{}]
    if fault == 'missing_history': exchange.order_history.return_value = []
    if fault == 'foreign_history': exchange.order_history.return_value[0]['orderId'] = 999
    if fault == 'fill': exchange.trade_history.return_value = [dict(orderId=100, id=1, side='BUY', qty='.02', price='1000', symbol='ETHUSDT', time=NOW-100)]
    before = recovery.snapshot(store)
    with pytest.raises((ValueError, TimeoutError)):
        await recovery.capture(store, exchange, NOW)
    assert recovery.snapshot(store) == before
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['same_sample', 'stale', 'journal_changed', 'different_samples'])
async def test_absence_commit_requires_fresh_independent_unchanged_proof(tmp_path, fault):
    store, exchange = fixture(tmp_path)
    a = await recovery.capture(store, exchange, NOW)
    b = await recovery.capture(store, exchange, NOW+5001)
    when = NOW+5002
    if fault == 'same_sample': b = deepcopy(a)
    if fault == 'stale': when += 60000
    if fault == 'journal_changed': store.set_engine_state('LOCKED', 'operator hold')
    if fault == 'different_samples': b['identity'] = 'classic:other'
    with pytest.raises(ValueError): recovery.commit(store, a, b, when)
    assert store.engine_state()['state'] == 'LOCKED'
    assert store.get_order('eg-missing')['status'] == 'PREPARED'
    store.close()
