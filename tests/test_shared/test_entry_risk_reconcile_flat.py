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
    def __init__(self, positions, status='FILLED'):
        self.positions = positions
        self.status = status

    async def get_order_by_client_id(self, symbol, client_id):
        return dict(symbol=symbol, clientOrderId=client_id, status=self.status,
                    executedQty='0.34')

    async def get_position(self, symbol):
        return self.positions

    async def get_open_orders(self):
        return []


@pytest.mark.asyncio
async def test_terminal_filled_order_releases_when_pm_omits_flat_symbol():
    connection = Connection()
    store = RiskPolicyStore(type('DB', (), {'pool': Pool(connection)})(), 'default')
    store.lock = lambda conn, strategy: _noop()
    await store.reconcile('btc_eth_aggressive', Exchange([]))
    assert connection.released == ['risk_test']


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
