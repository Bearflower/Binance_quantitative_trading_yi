from decimal import Decimal

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.store import DecisionStore, migrate


class FakeExchange:
    def __init__(self):
        self.ordinary = []
        self.algo = []
        self.position_amt = Decimal(0)
        self.exact = {}

    async def open_orders(self):
        return self.ordinary

    async def open_stops(self):
        return self.algo

    async def position(self):
        return [{"symbol": "ETHUSDT", "positionSide": "BOTH",
                 "positionAmt": str(self.position_amt)}]

    async def query_order(self, client_id):
        return self.exact.get(client_id)


@pytest.mark.asyncio
async def test_reconcile_repairs_accepted_order(tmp_path):
    path = tmp_path / "journal.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1900"),
                        Decimal("0.02"), "eg-a")
    exchange = FakeExchange()
    exchange.ordinary = [{"symbol": "ETHUSDT", "clientOrderId": "eg-a",
                          "orderId": 42, "status": "NEW", "executedQty": "0"}]
    engine = GridEngine(store, exchange)
    assert await engine.reconcile() is True
    assert store.get_order("eg-a")["exchange_order_id"] == "42"
    assert store.get_order("eg-a")["status"] == "NEW"
    store.close()


@pytest.mark.asyncio
async def test_foreign_order_or_unmatched_position_locks(tmp_path):
    path = tmp_path / "journal.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = FakeExchange()
    exchange.ordinary = [{"symbol": "ETHUSDT", "clientOrderId": "manual-1"}]
    engine = GridEngine(store, exchange)
    assert await engine.reconcile() is False
    assert store.engine_state()["state"] == "LOCKED"
    store.close()

    other = tmp_path / "other.sqlite3"
    migrate(other)
    store = DecisionStore(other)
    exchange = FakeExchange()
    exchange.position_amt = Decimal("0.02")
    assert await GridEngine(store, exchange).reconcile() is False
    assert store.engine_state()["state"] == "LOCKED"
    store.close()
