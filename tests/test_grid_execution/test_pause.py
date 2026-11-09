from decimal import Decimal

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.store import DecisionStore, migrate


class PauseExchange:
    def __init__(self):
        self.ordinary = [{"symbol": "ETHUSDT", "clientOrderId": "eg-a",
                          "orderId": 1, "status": "NEW", "executedQty": "0"}]

    async def cancel_order(self, client_id):
        self.ordinary = []
        return {"orderId": 1, "clientOrderId": client_id,
                "status": "CANCELED", "executedQty": "0"}

    async def open_orders(self):
        return list(self.ordinary)

    async def open_stops(self):
        return []

    async def position(self):
        return [{"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0"}]


@pytest.mark.asyncio
async def test_pause_cancels_all_unprotected_exposure_orders(tmp_path):
    path = tmp_path / "pause.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch("decision-1")
    store.prepare_order(epoch, 1, "ENTRY", "BUY", Decimal("1900"),
                        Decimal("0.02"), "eg-a")
    store.record_order("eg-a", "1", "NEW", Decimal(0))
    store.set_engine_state("ACTIVE")
    exchange = PauseExchange()
    assert await GridEngine(store, exchange).pause(epoch,
        stop_loss_low=Decimal("1850"), stop_loss_high=Decimal("2150")) is True
    assert store.engine_state()["state"] == "PAUSED"
    assert not exchange.ordinary
    store.close()
