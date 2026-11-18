from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from shared.entry_risk_policy import RiskPolicyStore


class Connection:
    def __init__(self):
        self.released = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, query, *args):
        if "SET state='released'" in query:
            self.released.append(args[0])

    async def fetch(self, query, *args):
        return [dict(client_order_id='risk_test', account_id='default',
                     strategy_id='btc_eth_aggressive', symbol='SOLUSDT',
                     state='submitting', quantity=Decimal('0.34'))]


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class Exchange:
    def __init__(self, positions, status='FILLED', *, account_positions=None,
                 all_positions=None, open_orders=None, algo_orders=None):
        self.positions = positions
        self.status = status
        self.account_positions = [] if account_positions is None else account_positions
        self.all_positions = positions if all_positions is None else all_positions
        self.open_orders = [] if open_orders is None else open_orders
        self.algo_orders = [] if algo_orders is None else algo_orders

    async def get_order_by_client_id(self, symbol, client_id):
        return dict(symbol=symbol, clientOrderId=client_id, status=self.status,
                    executedQty='0.34')

    async def get_position(self, symbol=None):
        return self.positions if symbol is not None else self.all_positions

    async def get_um_account_positions(self):
        return self.account_positions

    async def get_open_orders(self, symbol=None):
        return self.open_orders if symbol is not None else []

    async def get_open_algo_orders(self, symbol=None):
        return self.algo_orders


@pytest.mark.asyncio
async def test_terminal_filled_order_releases_when_pm_omits_flat_symbol():
    connection = Connection()
    store = RiskPolicyStore(type('DB', (), {'pool': Pool(connection)})(), 'default')
    store.lock = lambda conn, strategy: _noop()
    await store.reconcile('btc_eth_aggressive', Exchange([]))
    assert connection.released == ['risk_test']


@pytest.mark.asyncio
async def test_maintenance_release_ignores_unrelated_pending_entry():
    class SharedAccountExchange(Exchange):
        async def get_open_orders(self, symbol=None):
            return [] if symbol else [dict(symbol='BTCUSDT', reduceOnly=False,
                                           clientOrderId='other_strategy_entry')]

    connection = Connection()
    store = RiskPolicyStore(type('DB', (), {'pool': Pool(connection)})(), 'default')
    store.lock = lambda conn, strategy: _noop()
    await store.reconcile('hrs', SharedAccountExchange([]), enforce_legacy_orders=False)
    assert connection.released == ['risk_test']


@pytest.mark.asyncio
async def test_maintenance_release_keeps_other_flat_pending_symbol():
    connection = Connection()
    store = RiskPolicyStore(type('DB', (), {'pool': Pool(connection)})(), 'default')
    store.lock = lambda conn, strategy: _noop()
    await store.reconcile('hrs', Exchange([]), enforce_legacy_orders=False,
                          eligible_symbols={'ARIAUSDT'})
    assert connection.released == []


@pytest.mark.asyncio
@pytest.mark.parametrize('inventory', [
    {'account_positions': [dict(symbol='SOLUSDT', positionAmt='0.34')]},
    {'all_positions': [dict(symbol='SOLUSDT', positionAmt='-0.34')]},
    {'open_orders': [dict(clientOrderId='pending')]},
    {'algo_orders': [dict(algoId='pending')]},
])
async def test_flat_filtered_position_does_not_release_with_other_exposure(inventory):
    connection = Connection()
    store = RiskPolicyStore(type('DB', (), {'pool': Pool(connection)})(), 'default')
    store.lock = lambda conn, strategy: _noop()
    await store.reconcile('btc_eth_aggressive', Exchange([], **inventory))
    assert connection.released == []


@pytest.mark.asyncio
@pytest.mark.parametrize('positions,status', [
    ([dict(symbol='SOLUSDT', positionAmt='-0.34')], 'FILLED'),
    ([], 'NEW'),
])
async def test_reservation_stays_occupied_until_terminal_and_flat(positions, status):
    connection = Connection()
    store = RiskPolicyStore(type('DB', (), {'pool': Pool(connection)})(), 'default')
    store.lock = lambda conn, strategy: _noop()
    await store.reconcile('btc_eth_aggressive', Exchange(positions, status))
    assert connection.released == []


async def _noop():
    return None
