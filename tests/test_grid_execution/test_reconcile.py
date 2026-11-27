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

    async def order_trades(self, order_id, start_ms, end_ms):
        return self.trades.get(order_id, [])


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
async def test_restart_backfills_exact_trades_before_position_check(tmp_path):
    path = tmp_path / "journal.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1900"),
                        Decimal("0.02"), "eg-a")
    exchange = FakeExchange()
    exchange.trades = {42: [{"symbol": "ETHUSDT", "side": "BUY", "orderId": 42,
                             "id": 98765, "qty": "0.02", "price": "1900"}]}
    exchange.exact["eg-a"] = {"symbol": "ETHUSDT", "clientOrderId": "eg-a",
                              "orderId": 42, "status": "FILLED", "executedQty": "0.02"}
    exchange.position_amt = Decimal("0.02")
    # A filled position without exchange protection still blocks openings, but
    # the exact fill fact must survive for later exit/reconciliation.
    assert await GridEngine(store, exchange).reconcile() is False
    assert len(store.fills("eg-a")) == 1
    assert store.engine_state()["state"] == "LOCKED"
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


@pytest.mark.asyncio
async def test_rest_failure_during_reconcile_locks_openings(tmp_path):
    path = tmp_path / "rest-failure.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = FakeExchange()

    async def failed_orders():
        raise TimeoutError("orders unavailable")

    exchange.open_orders = failed_orders
    assert await GridEngine(store, exchange).reconcile() is False
    assert store.engine_state()["state"] == "LOCKED"
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["visible", "missing", "wrong_client", "wrong_id",
                                      "wrong_qty", "wrong_side", "wrong_symbol", "overfilled", "unacknowledged"])
async def test_missing_owned_order_after_stream_restore(tmp_path, scenario, caplog):
    from unittest.mock import AsyncMock
    from shared.binance_api import BinanceAPIError

    path = tmp_path / "restore.sqlite"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("reconnect")
    client_id = "eg-r-owned"
    store.prepare_order(epoch, 2, "REVERSE", "BUY", Decimal("1708.72"), Decimal(".077"), client_id)
    if scenario != "unacknowledged":
        store.record_order(client_id, "42", "NEW", 0)
    store.set_engine_state("ACTIVE")
    exchange = FakeExchange()
    missing = BinanceAPIError(-2013, "Order does not exist")
    exchange.query_order = AsyncMock(side_effect=missing)
    row = dict(orderId=42, clientOrderId=client_id, side="BUY", symbol="ETHUSDT",
               origQty="0.077", status="NEW", executedQty="0.000")
    for case, field, value in [("wrong_client", "clientOrderId", "foreign"),
                               ("wrong_id", "orderId", 43), ("wrong_qty", "origQty", ".078"),
                               ("overfilled", "executedQty", ".078"), ("wrong_side", "side", "SELL"), ("wrong_symbol", "symbol", "BTCUSDT")]:
        if scenario == case:
            row[field] = value
    exchange.query_order_by_id = AsyncMock(side_effect=missing if scenario == "missing" else [missing, row])
    result = await GridEngine(store, exchange).reconcile()
    assert result is (scenario == "visible")
    assert store.engine_state()["state"] == ("ACTIVE" if result else "LOCKED")
    assert store.get_order(client_id)["status"] == ("PREPARED" if scenario == "unacknowledged" else "NEW")
    if scenario == "unacknowledged":
        exchange.query_order_by_id.assert_not_awaited()
    else:
        assert 0 < exchange.query_order_by_id.await_count <= 3
        exchange.query_order_by_id.assert_awaited_with(42)
    assert client_id in caplog.text
    store.close()
