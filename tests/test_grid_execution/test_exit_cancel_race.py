"""A filled startup order can disappear from DELETE before its stream event."""
from decimal import Decimal as D

import pytest

from shared.binance_api import BinanceAPIError
from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.planner import GridPlan, PlannedOrder
from strategies.grid.execution.risk import RiskAssessment, RiskStatus
from strategies.grid.execution.store import DecisionStore, migrate
from tests.test_grid_execution.test_start import _decision


class FilledBeforeCancel:
    def __init__(self, *, start_flat=False):
        self.position_qty = D(0) if start_flat else D('.064')
        self.entry = dict(symbol='ETHUSDT', clientOrderId='eg-entry', orderId=1, side='BUY',
                          price='1691.92', origQty='.064', executedQty='.064', status='FILLED')
        self.cancel_calls = []
        self.query_calls = []
        self.close_calls = []
        self.limit_calls = []
        self.closed = None
        self.query_failure = None
        self.cancel_error = -2011

    async def open_orders(self): return []
    async def open_stops(self): return []
    async def position(self):
        return [dict(symbol='ETHUSDT', positionSide='BOTH', positionAmt=str(self.position_qty))]
    async def verify_account(self, leverage): return object()
    async def account_info(self): return dict(totalAvailableBalance='300')

    async def place_limit(self, side, qty, price, client):
        self.limit_calls.append(client)
        self.entry.update(clientOrderId=client, side=side, origQty=str(qty), price=str(price))
        self.position_qty = qty
        # The exchange accepts a passive order then fills it before position GET.
        return dict(self.entry, status='NEW', executedQty='0')

    async def cancel_order(self, client):
        self.cancel_calls.append(client)
        raise BinanceAPIError(self.cancel_error, 'Unknown order sent')

    async def query_order(self, client):
        self.query_calls.append(client)
        if self.query_failure:
            raise self.query_failure
        return dict(self.closed if self.closed and client == self.closed['clientOrderId'] else self.entry)

    async def close_position(self, side, qty, client):
        self.close_calls.append((side, qty, client))
        self.position_qty = D(0)
        self.closed = dict(symbol='ETHUSDT', clientOrderId=client, orderId=2, side=side,
                           origQty=str(qty), executedQty=str(qty), status='FILLED')
        return dict(self.closed)

    async def order_trades(self, order_id, start, end):
        row = self.entry if order_id == 1 else self.closed
        if D(row['executedQty']) == 0:
            return []
        return [dict(symbol='ETHUSDT', side=row['side'], orderId=order_id, id=100+order_id,
                     qty=row['executedQty'], price='1691.92' if order_id == 1 else '1691.52')]


@pytest.fixture
def race(tmp_path):
    path = tmp_path / 'exit-race.sqlite'; migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch('startup', levels=(D('1691.92'), D('1710.19')),
                               stop_loss_low=D(1500), stop_loss_high=D(1900))
    store.prepare_order(epoch, 0, 'ENTRY', 'BUY', D('1691.92'), D('.064'), 'eg-entry')
    store.record_order('eg-entry', '1', 'NEW', D(0))
    store.set_engine_state('LOCKED', 'position changed during grid start')
    yield store, FilledBeforeCancel(), epoch
    store.close()


@pytest.mark.asyncio
async def test_filled_startup_order_is_queried_after_delete_2011_and_exit_settles(race):
    store, exchange, epoch = race
    engine = GridEngine(store, exchange)
    assert await engine.exit_all(epoch)
    assert exchange.cancel_calls == ['eg-entry'] and exchange.query_calls == ['eg-entry']
    assert len(exchange.close_calls) == 1
    assert store.get_order('eg-entry')['status'] == 'FILLED'
    assert store.active_epoch_id() is None and not store.active_orders()
    assert sum((D(row['quantity']) * (1 if row['side']=='BUY' else -1)
                for row in store.all_fills()), D(0)) == 0
    # Resolving the cancellation must not clear the original operational lock.
    assert store.engine_state()['state'] == 'LOCKED'
    assert store.engine_state()['lock_reason'] == 'position changed during grid start'
    assert await engine.reconcile(allow_locked=True)
    assert exchange.cancel_calls == ['eg-entry'] and len(exchange.close_calls) == 1


@pytest.mark.asyncio
async def test_real_start_path_stops_batch_and_journals_both_fills_after_cancel_race(tmp_path):
    path = tmp_path / 'startup.sqlite'; migrate(path); store = DecisionStore(path)
    try:
        now = 1_800_000_000_000
        decision = _decision(store, now)
        plan = GridPlan((PlannedOrder(0,'BUY',D('1691.92'),D('.064')),
                         PlannedOrder(1,'SELL',D('1710.19'),D('.064'))),
                        (D('1691.92'),D('1710.19')),D(220),D(22),D(1500),D(1900))
        exchange = FilledBeforeCancel(start_flat=True)
        assert not await GridEngine(store,exchange).start_grid(decision,plan,
            RiskAssessment(RiskStatus.ALLOW,()),leverage=10,now_ms=now)
        assert len(exchange.limit_calls) == len(exchange.close_calls) == 1
        assert store.latest_epoch()['status'] == 'CLOSED'
        assert len(store.all_fills()) == 2
        assert store.engine_state()['state'] == 'LOCKED'
        assert store.cursor() != decision['decision_id']
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('clientOrderId','foreign'),('orderId',999),('symbol','BTCUSDT'),('side','SELL'),
    ('origQty','.128'),('price','1692'),('executedQty','NaN'),('executedQty','-.01'),
    ('executedQty','.128'),('executedQty','.032'),('executedQty','Infinity'),
    ('orderId',True),('orderId',0),('status','NEW'),('status','PARTIALLY_FILLED'),
])
async def test_2011_requires_exact_bounded_terminal_order_and_never_clears_ambiguity(race,field,value):
    store, exchange, epoch = race
    exchange.entry[field] = value
    exchange.position_qty = D(0)
    assert not await GridEngine(store,exchange).exit_all(epoch)
    assert store.engine_state()['state'] == 'LOCKED'
    assert store.engine_state()['lock_reason'] == 'cancel result unknown during exit'
    assert store.get_order('eg-entry')['status'] == 'NEW'
    assert store.active_epoch_id() == epoch


@pytest.mark.asyncio
@pytest.mark.parametrize('status,quantity', [
    ('CANCELED','0'), ('CANCELED','.032'), ('EXPIRED','0'), ('EXPIRED','.032'),
])
async def test_2011_terminal_partial_or_empty_order_requires_matching_fills(race,status,quantity):
    store,exchange,epoch = race
    exchange.entry.update(status=status,executedQty=quantity)
    exchange.position_qty = D(quantity)
    assert await GridEngine(store,exchange).exit_all(epoch)
    assert store.active_epoch_id() is None
    assert len(exchange.close_calls) == (1 if D(quantity) else 0)
    assert sum((D(row['quantity']) * (1 if row['side']=='BUY' else -1)
                for row in store.all_fills()),D(0)) == 0
    assert store.engine_state()['state'] == 'LOCKED'


@pytest.mark.asyncio
async def test_2011_cannot_reduce_already_journaled_execution(race):
    store,exchange,epoch = race
    store.record_order('eg-entry','1','PARTIALLY_FILLED',D('.032'))
    exchange.entry.update(status='CANCELED',executedQty='.016')
    exchange.position_qty = D(0)
    assert not await GridEngine(store,exchange).exit_all(epoch)
    assert D(store.get_order('eg-entry')['executed_qty']) == D('.032')
    assert store.engine_state()['lock_reason'] == 'cancel result unknown during exit'


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['clientOrderId','orderId','symbol','side','origQty','price','executedQty','status'])
async def test_2011_missing_order_evidence_keeps_epoch_locked(race,field):
    store,exchange,epoch = race
    del exchange.entry[field]
    exchange.position_qty = D(0)
    assert not await GridEngine(store,exchange).exit_all(epoch)
    assert store.get_order('eg-entry')['status'] == 'NEW'
    assert store.engine_state()['lock_reason'] == 'cancel result unknown during exit'


@pytest.mark.asyncio
async def test_terminal_order_without_complete_trade_history_does_not_close_epoch(race):
    store,exchange,epoch = race
    async def missing_trades(*args):
        return []
    exchange.order_trades = missing_trades
    assert not await GridEngine(store,exchange).exit_all(epoch)
    assert store.active_epoch_id() == epoch
    assert store.engine_state()['state'] == 'LOCKED'


@pytest.mark.asyncio
async def test_missing_terminal_query_keeps_cancellation_unresolved(race):
    store,exchange,epoch = race
    exchange.position_qty = D(0)
    exchange.query_failure = BinanceAPIError(-2013,'Order does not exist')
    assert not await GridEngine(store,exchange).exit_all(epoch)
    assert store.get_order('eg-entry')['status'] == 'NEW'
    assert store.engine_state()['state'] == 'LOCKED'


@pytest.mark.asyncio
async def test_other_cancel_errors_do_not_gain_2011_resolution_privilege(race):
    store,exchange,epoch = race
    exchange.position_qty = D(0)
    exchange.cancel_error = -1003
    assert not await GridEngine(store,exchange).exit_all(epoch)
    assert exchange.query_calls == [] and store.engine_state()['state'] == 'LOCKED'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('clientOrderId','foreign'),('orderId',999),('symbol','BTCUSDT'),('side','SELL'),
    ('executedQty','NaN'),('executedQty','-.01'),('executedQty','.128'),
    ('orderId',True),('orderId',0),
])
async def test_successful_delete_also_requires_owned_bounded_terminal_evidence(race,field,value):
    store,exchange,epoch = race
    exchange.position_qty = D(0)
    response = dict(exchange.entry,status='CANCELED',executedQty='0')
    response[field] = value
    async def cancel(client):
        return response
    exchange.cancel_order = cancel
    assert not await GridEngine(store,exchange).exit_all(epoch)
    assert store.get_order('eg-entry')['status'] == 'NEW'
    assert store.engine_state()['lock_reason'] == 'cancel result unknown during exit'
