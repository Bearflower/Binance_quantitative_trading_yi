"""Grid attribution must survive retries and preserve exchange identities."""

from decimal import Decimal
import sqlite3
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution.pg_projection import GridPgProjector, read_snapshot
from strategies.grid.execution.store import DecisionStore, migrate


def test_snapshot_includes_order_stop_and_verified_fill(tmp_path):
    journal = tmp_path / "execution.sqlite3"
    migrate(journal)
    store = DecisionStore(journal)
    epoch = store.create_epoch("signal-1")
    store.prepare_order(epoch, 2, "ENTRY", "BUY", Decimal("2000"),
                        Decimal("0.01"), "eg-entry-1")
    store.record_order("eg-entry-1", "123", "FILLED", Decimal("0.01"))
    store.record_fill("eg-entry-1", "123", "456", Decimal("0.01"), Decimal("2000"))
    store.conn.execute("""INSERT INTO protection
        (client_algo_id,epoch_id,side,trigger_price,status,exchange_algo_id)
        VALUES('eg-s-1',?,'SELL','1900','NEW','789')""", (epoch,))
    store.close()
    orders, stops, fills = read_snapshot(journal)
    assert len(orders) == len(stops) == len(fills) == 1
    assert orders[0]["client_order_id"] == "eg-entry-1"
    assert orders[0]["exchange_order_id"] == "123"
    assert stops[0]["client_algo_id"] == "eg-s-1"
    assert fills[0]["trade_id"] == "456"


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class RecordingPg:
    def __init__(self):
        self.rows = {}
        self.fills = {}

    def transaction(self):
        return _Transaction()

    async def fetch(self, sql, account_id):
        return [{"client_order_id": client_id} for (account, client_id), row in self.rows.items()
                if account == account_id and client_id.startswith("eg-s-")
                and row[3] is not None and row[10] in ("FILLED", "CANCELED", "EXPIRED")]

    async def fetchval(self, sql, account_id):
        return sum(1 for account, _ in self.rows if account == account_id)

    async def execute(self, sql, *args):
        if "trading.trade_records" in sql:
            identity = (args[0], args[1])
            previous = self.rows.get(identity)
            self.rows[identity] = args
            return "UPDATE 1" if previous else "INSERT 0 1"
        identity = (args[0], args[1])
        self.fills[identity] = args
        return "INSERT 0 1"


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['REJECTED', 'ABSENT'])
async def test_unexecuted_intent_projection_keeps_null_venue_id_and_pnl(tmp_path, status):
    journal, notices = tmp_path/'journal', tmp_path/'notices'
    migrate(journal)
    store = DecisionStore(journal)
    epoch = store.create_epoch('signal')
    store.prepare_order(epoch, 1, 'ENTRY', 'SELL', Decimal('1700'), Decimal('.02'), 'eg-unused')
    if status == 'REJECTED':
        store.record_maker_rejection('eg-unused')
    else:
        # ABSENT is written only by the separately tested operator recovery.
        store.conn.execute("UPDATE order_intent SET status='ABSENT'")
    store.close()
    conn = sqlite3.connect(notices)
    conn.execute('CREATE TABLE fact(order_id,trade_id,side,quantity,price,commission,realized_pnl,commission_asset,trade_time_ms,created_at_ms)')
    conn.close()
    pg = RecordingPg()
    projector = GridPgProjector(journal, notices, 'grid_classic', AsyncMock(), pg)
    assert await projector.run_once() == (1, 0)
    record = pg.rows[('grid_classic', 'eg-unused')]
    assert record[3] is None and record[7] == 0 and record[9] == 0
    assert record[10] == status and record[12] is None and record[13] is None
    assert not pg.fills


@pytest.mark.asyncio
async def test_projection_replay_attributes_normal_and_triggered_stop(tmp_path):
    journal = tmp_path / "execution.sqlite3"
    notices = tmp_path / "notices.sqlite3"
    migrate(journal)
    store = DecisionStore(journal)
    epoch = store.create_epoch("signal-1")
    store.prepare_order(epoch, 2, "ENTRY", "BUY", Decimal("2000"),
                        Decimal("0.01"), "eg-entry-1")
    store.record_order("eg-entry-1", "123", "FILLED", Decimal("0.01"))
    store.record_fill("eg-entry-1", "123", "456", Decimal("0.01"), Decimal("2000"))
    store.conn.execute("""INSERT INTO protection
        (client_algo_id,epoch_id,side,trigger_price,status,exchange_algo_id)
        VALUES('eg-s-1',?,'SELL','1900','NEW','789')""", (epoch,))
    store.close()
    conn = sqlite3.connect(notices)
    conn.execute("""CREATE TABLE fact (order_id text,trade_id text,side text,
        quantity text,price text,commission text,realized_pnl text,
        commission_asset text,trade_time_ms integer,created_at_ms integer)""")
    conn.execute("""INSERT INTO fact VALUES
        ('123','456','BUY','0.01','2000','0.01','0','USDT',1000000,1000000)""")
    conn.commit()
    conn.close()
    stop_trade = {"symbol": "ETHUSDT", "side": "SELL", "orderId": 321,
                  "id": 654, "qty": "0.01", "price": "1900",
                  "commission": "0.01", "commissionAsset": "USDT",
                  "realizedPnl": "-1", "time": 1000001}
    exchange = AsyncMock()
    exchange.query_stop.return_value = {"clientAlgoId": "eg-s-1",
        "algoStatus": "FINISHED", "actualOrderId": "321"}
    exchange.query_order_by_id.return_value = {"symbol": "ETHUSDT", "side": "SELL",
        "orderId": 321, "status": "FILLED", "executedQty": "0.01", "time": 1000001}
    exchange.order_trades.return_value = [stop_trade]
    pg = RecordingPg()
    projector = GridPgProjector(journal, notices, "grid_pm_ethusdt_01", exchange, pg)
    assert await projector.run_once() == (2, 2)
    assert await projector.run_once() == (1, 1)
    assert len(pg.rows) == 2
    assert len(pg.fills) == 2
    assert pg.rows[("grid_pm_ethusdt_01", "eg-entry-1")][3] == "123"
    assert pg.rows[("grid_pm_ethusdt_01", "eg-s-1")][3:5] == ("321", "789")
    assert pg.rows[("grid_pm_ethusdt_01", "eg-s-1")][-1] == "STOP_LOSS"

@pytest.mark.asyncio
async def test_unchanged_projection_does_not_rewrite_and_uses_fill_time(tmp_path):
    from unittest.mock import AsyncMock
    journal, notices = tmp_path / 'journal', tmp_path / 'notices'
    migrate(journal)
    store = DecisionStore(journal)
    epoch = store.create_epoch('signal')
    store.prepare_order(epoch, 1, 'ENTRY', 'BUY', Decimal('2000'), Decimal('.01'), 'eg-midnight')
    store.record_order('eg-midnight', '123', 'FILLED', Decimal('.01'))
    store.record_fill('eg-midnight', '123', '456', Decimal('.01'), Decimal('2000'))
    store.conn.execute('UPDATE order_intent SET created_at_ms=1')
    store.close()
    conn = sqlite3.connect(notices)
    conn.execute('CREATE TABLE fact(order_id,trade_id,side,quantity,price,commission,realized_pnl,commission_asset,trade_time_ms,created_at_ms)')
    conn.execute("INSERT INTO fact VALUES('123','456','BUY','.01','2000','.01','0','USDT',86400001,2)")
    conn.commit()
    conn.close()
    pg = RecordingPg()
    pg.execute = AsyncMock(wraps=pg.execute)
    projector = GridPgProjector(journal, notices, 'grid_pm_ethusdt_01', AsyncMock(), pg)
    await projector.run_once()
    assert pg.rows[('grid_pm_ethusdt_01', 'eg-midnight')][11].day == 2
    pg.execute.reset_mock()
    await projector.run_once()
    pg.execute.assert_not_called()
    pg.rows.clear()
    with pytest.raises(ValueError, match='count differs'):
        await projector.run_once()
    await projector.run_once()
    assert len(pg.rows) == 1

@pytest.mark.asyncio
async def test_stop_child_reaches_notice_pg_and_dashboard_once(tmp_path):
    from strategies.grid.execution.notification_worker import NotificationWorker
    from strategies.grid.execution.dashboard_snapshot import calculate_snapshot
    execution, notices = tmp_path / 'journal', tmp_path / 'notices'
    migrate(execution)
    store = DecisionStore(execution)
    epoch = store.create_epoch('signal')
    store.conn.execute('UPDATE grid_epoch SET created_at_ms=1000')
    for cid, role, side, order_id, trade_id, price in [
        ('eg-entry', 'ENTRY', 'BUY', '123', '456', '2000'),
        ('eg-stop-321', 'STOP', 'SELL', '321', '654', '1900')]:
        store.prepare_order(epoch, 0 if role == 'ENTRY' else -1, role, side,
                            Decimal(price), Decimal('.01'), cid)
        store.record_order(cid, order_id, 'FILLED', Decimal('.01'))
        store.record_fill(cid, order_id, trade_id, Decimal('.01'), Decimal(price))
    store.conn.execute("INSERT INTO protection VALUES('eg-s-1',?,'SELL','1900','TRIGGERED','789')", (epoch,))
    trades = [dict(symbol='ETHUSDT', side=side, orderId=oid, id=tid, qty='.01', price=price,
                   realizedPnl=pnl, commission='.01', commissionAsset='USDT', time=at)
              for side, oid, tid, price, pnl, at in [('BUY',123,456,'2000','0',1500),
                                                    ('SELL',321,654,'1900','-1',2000)]]
    store.conn.execute("UPDATE order_intent SET created_at_ms=1000 WHERE role='ENTRY'")
    exchange = AsyncMock()
    exchange.order_trades.side_effect = lambda oid, start, end: [t for t in trades if t['orderId']==oid and start <= t['time'] <= end]
    position = {'symbol':'ETHUSDT', 'positionSide':'BOTH', 'positionAmt':'0'}
    exchange.position.return_value = [position]
    exchange.income.return_value = []
    exchange.query_stop.return_value = {'clientAlgoId':'eg-s-1', 'algoStatus':'FINISHED', 'actualOrderId':'321'}
    exchange.query_order_by_id.return_value = {'symbol':'ETHUSDT', 'side':'SELL', 'orderId':321, 'status':'FILLED','executedQty':'.01','time':2000}
    sender = AsyncMock(return_value=True)
    worker = NotificationWorker(execution, notices, exchange, sender)
    assert await worker.run_once(now_ms=3000) == 2
    assert await worker.run_once(now_ms=4000) == 0
    pg = RecordingPg()
    projector = GridPgProjector(execution, notices, 'grid_pm_ethusdt_01', exchange, pg)
    await projector.run_once()
    assert len(pg.rows) == 2
    assert len(pg.fills) == 2
    assert pg.rows[('grid_pm_ethusdt_01','eg-s-1')][-1] == 'STOP_LOSS'
    exchange.pm = False
    classic = GridPgProjector(execution, notices, 'grid_pm_ethusdt_01', exchange, pg)
    await classic.run_once()
    assert len(pg.rows) == len(pg.fills) == 4
    assert pg.rows[('grid_pm_ethusdt_01', 'eg-s-1')][-2] == 'PM_USER_TRADES'
    assert pg.rows[('grid_pm_ethusdt_01:classic', 'eg-s-1')][-2] == 'FAPI_USER_TRADES'
    import sqlite3
    worker.notices.row_factory = sqlite3.Row
    facts = [dict(row) for row in worker.notices.execute('SELECT * FROM fact')]
    fills = [dict(row) for row in store.conn.execute('SELECT f.*,o.epoch_id,o.side FROM fill f JOIN order_intent o USING(client_order_id)')]
    snapshot = calculate_snapshot(epochs=[{'id':epoch,'created_at_ms':1000,'status':'CLOSED'}],
        fills=fills, facts=facts, funding=[], position=position,
        engine={'state':'FLAT'},open_orders=0,now_ms=4000)
    assert Decimal(snapshot['cumulative_pnl_usdt']) == Decimal('-1.02')
    assert Decimal(snapshot['net_qty']) == 0
    worker.close()
    store.close()
