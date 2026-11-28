from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from shared.binance_api import BinanceAPIError
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

    async def order_trades(self, order_id, start_ms, end_ms):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("venue_state", ["FILLED", "NEW", "wrong_order"])
async def test_exit_queries_unknown_cancel_before_settling(tmp_path, venue_state):
    path = tmp_path / "exit.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    qty = Decimal("0.02")
    store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1900"), qty, "eg-entry")
    store.record_order("eg-entry", "1", "NEW", Decimal(0))
    exchange = ExitExchange()
    exchange.ordinary = []
    exchange.cancel_order = AsyncMock(side_effect=BinanceAPIError(-2011, "Unknown order sent"))
    original_query = exchange.query_order

    async def query(client_id):
        if client_id != "eg-entry":
            return await original_query(client_id)
        return {"symbol": "ETHUSDT", "clientOrderId": client_id,
                "orderId": 99 if venue_state == "wrong_order" else 1,
                "side": "BUY", "origQty": str(qty), "price": "1900",
                "status": "NEW" if venue_state == "NEW" else "FILLED",
                "executedQty": "0" if venue_state == "NEW" else str(qty)}

    exchange.query_order = AsyncMock(side_effect=query)
    exchange.trades[1] = [{"symbol": "ETHUSDT", "side": "BUY", "orderId": 1,
                           "id": 76, "qty": str(qty), "price": "1900"}]

    resolved = venue_state == "FILLED"
    assert await GridEngine(store, exchange).exit_all(epoch) is resolved
    exchange.cancel_order.assert_awaited_once_with("eg-entry")
    exchange.query_order.assert_awaited_once_with("eg-entry")
    assert len(exchange.close_calls) == 1
    assert store.engine_state()["state"] == ("FLAT" if resolved else "LOCKED")
    assert store.get_order("eg-entry")["status"] == ("FILLED" if resolved else "NEW")
    assert len(store.fills("eg-entry")) == int(resolved)
    store.close()


@pytest.mark.asyncio
async def test_queued_zero_fill_cancel_acks_do_not_retry_exit(tmp_path):
    path = tmp_path / "exit.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    engine = GridEngine(store, ExitExchange())
    engine.exit_all = AsyncMock(return_value=False)
    store.set_engine_state("LOCKED", "cancel result unknown during exit")
    for order_id in range(1, 4):
        cid = f"eg-entry-{order_id}"
        store.prepare_order(epoch, order_id, "ENTRY", "BUY", Decimal("1900"),
                            Decimal("0.02"), cid)
        store.record_order(cid, str(order_id), "NEW", Decimal(0))
        event = {"e": "ORDER_TRADE_UPDATE", "o": {"s": "ETHUSDT", "c": cid,
                 "i": order_id, "S": "BUY", "x": "CANCELED", "X": "CANCELED",
                 "l": "0", "z": "0", "L": "0", "t": 0}}
        await engine.on_order_update(event, epoch_id=epoch, levels=(), filters=None,
                                     stop_loss_low=Decimal("1800"), stop_loss_high=Decimal("2200"))
        assert store.get_order(cid)["status"] == "CANCELED"
    engine.exit_all.assert_not_awaited()
    assert store.engine_state()["state"] == "LOCKED"
    store.close()
