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

    async def open_orders(self):
        return list(self.ordinary)

    async def open_stops(self):
        return list(self.algo)

    async def position(self):
        return [{"symbol": "ETHUSDT", "positionSide": "BOTH",
                 "positionAmt": str(self.position_amt)}]

    async def cancel_order(self, client_id):
        self.ordinary = [x for x in self.ordinary if x["clientOrderId"] != client_id]
        return {"orderId": 1, "clientOrderId": client_id,
                "status": "CANCELED", "executedQty": "0"}

    async def query_order(self, client_id):
        return {"orderId": 1, "clientOrderId": client_id,
                "status": "CANCELED", "executedQty": "0"}

    async def close_position(self, side, qty, client_id):
        self.close_calls.append((side, qty, client_id))
        self.position_amt = Decimal(0)
        return {"orderId": 2, "clientOrderId": client_id,
                "status": "FILLED", "executedQty": str(qty)}

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
    exchange = ExitExchange()
    engine = GridEngine(store, exchange)
    assert await engine.exit_all(epoch) is True
    assert store.engine_state()["state"] == "FLAT"
    assert exchange.close_calls[0][0:2] == ("SELL", Decimal("0.02"))
    assert not store.active_orders()
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
