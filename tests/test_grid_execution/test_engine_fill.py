from decimal import Decimal

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.planner import ExchangeFilters
from strategies.grid.execution.store import DecisionStore, migrate


class FillExchange:
    def __init__(self):
        self.position_amt = Decimal("0.02")
        self.algo = []
        self.ordinary = []

    async def position(self):
        return [{"symbol": "ETHUSDT", "positionSide": "BOTH",
                 "positionAmt": str(self.position_amt)}]

    async def open_orders(self):
        return list(self.ordinary)

    async def open_stops(self):
        return list(self.algo)

    async def place_stop(self, side, trigger, client_id):
        row = {"symbol": "ETHUSDT", "clientAlgoId": client_id,
               "algoId": 9, "algoStatus": "NEW", "side": side,
               "triggerPrice": str(trigger)}
        self.algo.append(row)
        return row

    async def query_stop(self, client_id):
        return next((x for x in self.algo if x["clientAlgoId"] == client_id), None)

    async def place_limit(self, side, qty, price, client_id):
        row = {"symbol": "ETHUSDT", "clientOrderId": client_id,
               "orderId": 2, "status": "NEW", "executedQty": "0",
               "side": side, "price": str(price), "origQty": str(qty)}
        self.ordinary.append(row)
        return row


@pytest.mark.asyncio
async def test_fill_is_protected_before_reverse_order(tmp_path):
    path = tmp_path / "fill.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1950"),
                        Decimal("0.02"), "eg-entry")
    store.record_order("eg-entry", "1", "NEW", Decimal(0))
    store.set_engine_state("ACTIVE")
    exchange = FillExchange()
    event = {"e": "ORDER_TRADE_UPDATE", "o": {
        "s": "ETHUSDT", "c": "eg-entry", "i": 1, "S": "BUY",
        "x": "TRADE", "X": "FILLED", "l": "0.02", "z": "0.02",
        "L": "1950", "t": 15}}
    filters = ExchangeFilters(Decimal("0.01"), Decimal("0.001"),
                              Decimal("0.01"), Decimal("5"))
    assert await GridEngine(store, exchange).on_order_update(
        event, epoch_id=epoch,
        levels=(Decimal("1900"), Decimal("1950"), Decimal("2000")),
        filters=filters, stop_loss_low=Decimal("1850"),
        stop_loss_high=Decimal("2150")) is True
    assert len(exchange.algo) == 1
    assert len(exchange.ordinary) == 1
    assert exchange.ordinary[0]["side"] == "SELL"
    assert exchange.ordinary[0]["price"] == "2000"
    assert store.engine_state()["state"] == "ACTIVE"
    store.close()
