from decimal import Decimal

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.store import DecisionStore, migrate


class ExitExchange:
    def __init__(self):
        self.position_amt = Decimal("0.02")
        self.ordinary = [{"symbol": "ETHUSDT", "clientOrderId": "eg-entry",
                          "orderId": 1, "status": "NEW", "executedQty": "0"}]
        self.algo = []
        self.close_calls = []
        self.trades = {}
        self.ack_close = False
        self.closed_ids = set()
        self.cancel_calls = []

    async def open_orders(self):
        return list(self.ordinary)

    async def open_stops(self):
        return list(self.algo)

    async def position(self):
        return [{"symbol": "ETHUSDT", "positionSide": "BOTH",
                 "positionAmt": str(self.position_amt)}]

    async def cancel_order(self, client_id):
        self.cancel_calls.append(client_id)
        self.ordinary = [x for x in self.ordinary if x["clientOrderId"] != client_id]
        return {"orderId": 1, "clientOrderId": client_id,
                "status": "CANCELED", "executedQty": "0"}

    async def query_order(self, client_id):
        if client_id in self.closed_ids:
            return {"orderId": 2, "clientOrderId": client_id,
                    "status": "FILLED", "executedQty": "0.02"}
        return {"orderId": 1, "clientOrderId": client_id,
                "status": "CANCELED", "executedQty": "0"}

    async def close_position(self, side, qty, client_id):
        self.close_calls.append((side, qty, client_id))
        self.position_amt = Decimal(0)
        self.closed_ids.add(client_id)
        self.trades[2] = [{"symbol": "ETHUSDT", "side": side, "orderId": 2,
                           "id": 77, "qty": str(qty), "price": "2000"}]
        return {"orderId": 2, "clientOrderId": client_id,
                "status": "NEW" if self.ack_close else "FILLED",
                "executedQty": "0" if self.ack_close else str(qty)}

    async def order_trades(self, order_id):
        return self.trades.get(order_id, [])

    async def cancel_stop(self, client_id):
        self.algo = [x for x in self.algo if x["clientAlgoId"] != client_id]
        return {"clientAlgoId": client_id, "code": "200"}


@pytest.mark.asyncio
async def test_exit_persists_state_before_cancel_and_uses_reduce_only_close(tmp_path):
    path = tmp_path / "exit.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1900"),
                        Decimal("0.02"), "eg-entry")
    store.prepare_order(epoch, 0, "FILLED_ENTRY", "BUY", Decimal("1950"),
                        Decimal("0.02"), "eg-prior")
    store.record_order("eg-prior", "99", "FILLED", Decimal("0.02"))
    store.record_fill("eg-prior", "99", "trade-prior", Decimal("0.02"),
                      Decimal("1950"))
    exchange = ExitExchange()
    engine = GridEngine(store, exchange)
    assert await engine.exit_all(epoch) is True
    assert store.engine_state()["state"] == "FLAT"
    assert exchange.close_calls[0][0:2] == ("SELL", Decimal("0.02"))
    assert not store.active_orders()
    store.close()


@pytest.mark.asyncio
async def test_ack_market_close_is_resolved_by_client_id(tmp_path):
    path = tmp_path / "ack.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    store.prepare_order(epoch, 0, "FILLED_ENTRY", "BUY", Decimal("1950"),
                        Decimal("0.02"), "eg-prior")
    store.record_order("eg-prior", "99", "FILLED", Decimal("0.02"))
    store.record_fill("eg-prior", "99", "trade-prior", Decimal("0.02"),
                      Decimal("1950"))
    exchange = ExitExchange()
    exchange.ordinary = []
    exchange.ack_close = True
    assert await GridEngine(store, exchange).exit_all(epoch)
    assert store.engine_state()["state"] == "FLAT"
    assert len(exchange.close_calls) == 1
    store.close()


@pytest.mark.asyncio
async def test_unknown_close_result_never_blindly_repeats(tmp_path):
    path = tmp_path / "exit.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    exchange = ExitExchange()
    exchange.ordinary = []

    async def unknown(*_):
        raise TimeoutError("lost reply")

    exchange.close_position = unknown
    assert await GridEngine(store, exchange).exit_all(epoch) is False
    assert store.engine_state()["state"] == "LOCKED"
    store.close()
