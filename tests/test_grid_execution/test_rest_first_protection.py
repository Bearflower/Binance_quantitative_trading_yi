"""REST can discover an owned fill before its user-stream callback arrives."""
from decimal import Decimal

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.store import DecisionStore, migrate


class RestFirstExchange:
    def __init__(self):
        self.position_amt = Decimal('0.035')
        self.stops = {}
        self.list_lag = False
        self.invalid = {}
        self.submissions = 0
        self.events = []

    async def open_orders(self):
        return []

    async def query_order(self, client_id):
        return dict(symbol='ETHUSDT', clientOrderId=client_id, orderId=42,
                    status='FILLED', executedQty='0.035')

    async def order_trades(self, order_id, start_ms, end_ms):
        return [dict(symbol='ETHUSDT', side='BUY', orderId=42, id=7,
                     qty='0.035', price='1799.13')]

    async def position(self):
        return [dict(symbol='ETHUSDT', positionSide='BOTH',
                     positionAmt=str(self.position_amt))]

    async def open_stops(self):
        return [] if self.list_lag else list(self.stops.values())

    async def place_stop(self, side, trigger, client_id):
        self.submissions += 1
        self.events.append('stop')
        row = dict(symbol='ETHUSDT', clientAlgoId=client_id, algoId=91,
                   algoStatus='NEW', side=side, triggerPrice=str(trigger),
                   closePosition=True, workingType='MARK_PRICE',
                   orderType='STOP_MARKET', positionSide='BOTH')
        row.update(self.invalid)
        self.stops[client_id] = row
        return dict(row)

    async def query_stop(self, client_id):
        row = self.stops.get(client_id)
        return dict(row) if row else None


@pytest.fixture
def owned_fill(tmp_path):
    path = tmp_path / 'rest-first.sqlite3'
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch('decision-1', stop_loss_low=Decimal('1750'),
                               stop_loss_high=Decimal('1850'))
    store.prepare_order(epoch, 1, 'ENTRY', 'BUY', Decimal('1799.13'),
                        Decimal('0.035'), 'eg-entry')
    store.record_order('eg-entry', '42', 'NEW', Decimal(0))
    store.set_engine_state('ACTIVE')
    yield store, RestFirstExchange()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['ACTIVE', 'PAUSED'])
@pytest.mark.parametrize('list_lag', [False, True])
async def test_rest_first_fill_is_protected_before_success(owned_fill, state, list_lag):
    store, exchange = owned_fill
    store.set_engine_state(state)
    exchange.list_lag = list_lag
    engine = GridEngine(store, exchange)
    assert await engine.reconcile() is True
    assert len(store.fills('eg-entry')) == 1
    assert len(store.active_protections()) == 1
    assert exchange.submissions == 1
    assert store.engine_state()['state'] == state
    # A later pass must reuse the same durable stop.
    assert await engine.reconcile() is True
    assert exchange.submissions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [
    {'symbol': 'BTCUSDT'}, {'clientAlgoId': 'foreign-stop'},
    {'side': 'BUY'}, {'orderType': 'TAKE_PROFIT_MARKET'},
    {'triggerPrice': '1749'}, {'positionSide': 'LONG'},
    {'workingType': 'CONTRACT_PRICE'}, {'closePosition': False},
    {'closePosition': 'true'}, {'algoStatus': 'CANCELED'},
])
async def test_direct_query_requires_all_protection_fields(owned_fill, invalid):
    store, exchange = owned_fill
    exchange.list_lag = True
    exchange.invalid = invalid
    assert await GridEngine(store, exchange).reconcile() is False
    assert store.engine_state()['state'] == 'LOCKED'
    assert exchange.submissions == 1
    assert await GridEngine(store, exchange).reconcile(allow_locked=True) is False
    assert exchange.submissions == 1


@pytest.mark.asyncio
async def test_unknown_position_never_installs_stop(owned_fill):
    store, exchange = owned_fill
    exchange.position_amt = Decimal('0.04')
    assert await GridEngine(store, exchange).reconcile() is False
    assert store.engine_state()['lock_reason'] == 'position does not match journal fills'
    assert exchange.submissions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['EXITING', 'LOCKED'])
async def test_exit_and_lock_never_install_new_protection(owned_fill, state):
    store, exchange = owned_fill
    store.set_engine_state(state)
    assert await GridEngine(store, exchange).reconcile(allow_locked=True) is False
    assert exchange.submissions == 0


@pytest.mark.asyncio
async def test_unknown_prepared_stop_is_never_resubmitted(owned_fill):
    store, exchange = owned_fill
    store.conn.execute('''INSERT INTO protection
        (client_algo_id,epoch_id,side,trigger_price,status)
        VALUES(?,?,?,?,?)''', ('eg-pending', store.active_epoch_id(), 'SELL', '1750', 'UNKNOWN'))
    assert await GridEngine(store, exchange).reconcile() is False
    assert exchange.submissions == 0


@pytest.mark.asyncio
async def test_malformed_confirmation_logs_allowlisted_snapshot(owned_fill, caplog):
    store, exchange = owned_fill
    exchange.list_lag = True
    exchange.invalid = {'triggerPrice': 'not-a-price', 'secret': 'do-not-log'}
    assert await GridEngine(store, exchange).reconcile() is False
    assert 'grid protection snapshot invalid' in caplog.text
    assert 'not-a-price' in caplog.text
    assert 'do-not-log' not in caplog.text
    assert exchange.submissions == 1


@pytest.mark.asyncio
async def test_existing_prepared_stop_visible_only_by_exact_query_is_reused(owned_fill):
    store, exchange = owned_fill
    client_id = 'eg-pending'
    store.conn.execute('''INSERT INTO protection
        (client_algo_id,epoch_id,side,trigger_price,status)
        VALUES(?,?,?,?,?)''', (client_id, store.active_epoch_id(), 'SELL', '1750', 'PREPARED'))
    await exchange.place_stop('SELL', Decimal('1750'), client_id)
    exchange.submissions = 0
    exchange.list_lag = True
    assert await GridEngine(store, exchange).reconcile() is True
    assert exchange.submissions == 0


@pytest.mark.asyncio
async def test_unowned_stop_cannot_be_used_for_owned_position(owned_fill):
    store, exchange = owned_fill
    await exchange.place_stop('SELL', Decimal('1750'), 'manual-stop')
    exchange.submissions = 0
    assert await GridEngine(store, exchange).reconcile() is False
    assert store.engine_state()['lock_reason'] == 'foreign or unknown ETHUSDT algo order'
    assert exchange.submissions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('algo_id', [None, 10, 'not-numeric'])
@pytest.mark.parametrize('list_lag', [False, True])
async def test_known_exchange_stop_id_must_match(owned_fill, algo_id, list_lag):
    store, exchange = owned_fill
    client_id = 'eg-known'
    store.conn.execute('''INSERT INTO protection
        (client_algo_id,epoch_id,side,trigger_price,status,exchange_algo_id)
        VALUES(?,?,?,?,?,?)''', (client_id, store.active_epoch_id(), 'SELL', '1750', 'NEW', '9'))
    await exchange.place_stop('SELL', Decimal('1750'), client_id)
    exchange.stops[client_id]['algoId'] = algo_id
    exchange.list_lag = list_lag
    exchange.submissions = 0
    assert await GridEngine(store, exchange).reconcile() is False
    assert store.engine_state()['state'] == 'LOCKED'
    assert exchange.submissions == 0
    assert store.active_protections()[0]['exchange_algo_id'] == '9'


@pytest.mark.asyncio
@pytest.mark.parametrize('list_lag', [False, True])
async def test_outdated_trigger_cannot_satisfy_current_epoch_protection(owned_fill, list_lag):
    store, exchange = owned_fill
    client_id = 'eg-old'
    store.conn.execute('''INSERT INTO protection
        (client_algo_id,epoch_id,side,trigger_price,status,exchange_algo_id)
        VALUES(?,?,?,?,?,?)''', (client_id, store.active_epoch_id(), 'SELL', '1700', 'NEW', '9'))
    await exchange.place_stop('SELL', Decimal('1700'), client_id)
    exchange.stops[client_id]['algoId'] = 9
    exchange.list_lag = list_lag
    # The replacement is not confirmed; the old loose stop must not permit success.
    exchange.invalid = {'triggerPrice': '1700'}
    assert await GridEngine(store, exchange).reconcile() is False
    assert store.engine_state()['state'] == 'LOCKED'


@pytest.mark.asyncio
@pytest.mark.parametrize('lagging_view', ['position', 'orders'])
async def test_owned_fill_between_rest_snapshots_is_protected_without_lock(owned_fill, lagging_view, caplog):
    store, exchange = owned_fill
    if lagging_view == 'position':
        actual_position = exchange.position
        first = True

        async def position():
            nonlocal first
            if first:
                first = False
                return [dict(symbol='ETHUSDT', positionSide='BOTH', positionAmt='0')]
            return await actual_position()

        exchange.position = position
    else:
        actual_orders = exchange.open_orders
        first = True

        async def open_orders():
            nonlocal first
            if first:
                first = False
                return [dict(symbol='ETHUSDT', clientOrderId='eg-entry', orderId=42,
                             status='NEW', executedQty='0')]
            return await actual_orders()

        exchange.open_orders = open_orders

    assert await GridEngine(store, exchange).reconcile()
    assert store.engine_state()['state'] == 'ACTIVE'
    assert [f['trade_id'] for f in store.fills('eg-entry')] == ['7']
    assert exchange.submissions == 1
    stop = next(iter(exchange.stops.values()))
    assert stop['side'] == 'SELL' and Decimal(stop['triggerPrice']) == Decimal('1750')
    assert 'grid position snapshot mismatch' in caplog.text
