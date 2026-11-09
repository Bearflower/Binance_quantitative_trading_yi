from decimal import Decimal

import pytest

from strategies.grid.execution.fills import ProtectionCoordinator
from strategies.grid.execution.store import DecisionStore, migrate


class FakeExchange:
    def __init__(self):
        self.open = []
        self.placed = []
        self.canceled = []
        self.reject = False

    async def open_stops(self):
        return list(self.open)

    async def place_stop(self, side, trigger_price, client_id):
        self.placed.append((side, trigger_price, client_id))
        if self.reject:
            raise RuntimeError("stop rejected")
        row = {"symbol": "ETHUSDT", "side": side, "triggerPrice": str(trigger_price),
               "clientAlgoId": client_id, "algoId": len(self.placed), "algoStatus": "NEW"}
        self.open.append(row)
        return row

    async def query_stop(self, client_id):
        return next((x for x in self.open if x["clientAlgoId"] == client_id), None)

    async def cancel_stop(self, client_id):
        self.canceled.append(client_id)
        self.open = [x for x in self.open if x["clientAlgoId"] != client_id]


def _store(tmp_path):
    path = tmp_path / "execution.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    return store, epoch


@pytest.mark.asyncio
async def test_long_and_short_have_exchange_side_close_all_stops(tmp_path):
    store, epoch = _store(tmp_path)
    exchange = FakeExchange()
    protection = ProtectionCoordinator(store, exchange)
    first = await protection.ensure(epoch, Decimal("0.02"), Decimal("1850"), Decimal("2150"))
    assert first is True
    assert exchange.placed[0][:2] == ("SELL", Decimal("1850"))
    assert await protection.ensure(epoch, Decimal("0.02"), Decimal("1850"), Decimal("2150"))
    assert len(exchange.placed) == 1
    assert await protection.ensure(epoch, Decimal("-0.02"), Decimal("1850"), Decimal("2150"))
    assert exchange.placed[-1][:2] == ("BUY", Decimal("2150"))
    assert len(exchange.canceled) == 1
    store.close()


@pytest.mark.asyncio
async def test_stale_boundary_is_replaced_before_old_stop_is_retired(tmp_path):
    store, epoch = _store(tmp_path)
    exchange = FakeExchange()
    protection = ProtectionCoordinator(store, exchange)
    await protection.ensure(epoch, Decimal("0.02"), Decimal("1850"), Decimal("2150"))
    old_id = exchange.open[0]["clientAlgoId"]
    await protection.ensure(epoch, Decimal("0.02"), Decimal("1840"), Decimal("2150"))
    assert exchange.placed[-1][1] == Decimal("1840")
    assert exchange.canceled == [old_id]
    store.close()


@pytest.mark.asyncio
async def test_rejected_or_missing_stop_enters_exiting(tmp_path):
    store, epoch = _store(tmp_path)
    exchange = FakeExchange()
    exchange.reject = True
    protection = ProtectionCoordinator(store, exchange)
    assert await protection.ensure(epoch, Decimal("0.02"), Decimal("1850"), Decimal("2150")) is False
    assert store.engine_state()["state"] == "EXITING"
    store.close()


@pytest.mark.asyncio
async def test_flip_back_creates_new_client_id_after_old_stop_canceled(tmp_path):
    store, epoch = _store(tmp_path)
    exchange = FakeExchange()
    protection = ProtectionCoordinator(store, exchange)
    assert await protection.ensure(epoch, Decimal("0.02"), Decimal("1850"), Decimal("2150"))
    old_long_id = exchange.open[0]["clientAlgoId"]
    assert await protection.ensure(epoch, Decimal("-0.02"), Decimal("1850"), Decimal("2150"))
    assert await protection.ensure(epoch, Decimal("0.02"), Decimal("1850"), Decimal("2150"))
    new_long_id = exchange.open[0]["clientAlgoId"]
    assert old_long_id != new_long_id
    assert len(exchange.placed) == 3
    store.close()
