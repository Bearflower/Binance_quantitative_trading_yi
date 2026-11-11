from decimal import Decimal

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.planner import ExchangeFilters, GridPlan, PlannedOrder
from strategies.grid.execution.risk import RiskAssessment, RiskStatus
from strategies.grid.execution.store import DecisionStore, migrate


class FakeExchange:
    def __init__(self):
        self.orders = {}
        self.stops = {}
        self.position_amt = Decimal(0)
        self.next_order_id = 1
        self.next_stop_id = 1

    async def verify_account(self, leverage):
        assert leverage == 10
        return ExchangeFilters(Decimal("0.01"), Decimal("0.001"),
                               Decimal("0.01"), Decimal("5"))

    async def account_info(self):
        return {"accountStatus": "NORMAL", "totalAvailableBalance": "100"}

    async def open_orders(self):
        return [dict(row) for row in self.orders.values()
                if row["status"] in ("NEW", "PARTIALLY_FILLED")]

    async def open_stops(self):
        return [dict(row) for row in self.stops.values()]

    async def position(self):
        return [{"symbol": "ETHUSDT", "positionSide": "BOTH",
                 "positionAmt": str(self.position_amt)}]

    async def place_limit(self, side, qty, price, client_id):
        row = {"symbol": "ETHUSDT", "clientOrderId": client_id,
               "orderId": self.next_order_id, "side": side,
               "price": str(price), "origQty": str(qty),
               "status": "NEW", "executedQty": "0"}
        self.next_order_id += 1
        self.orders[client_id] = row
        return dict(row)

    async def query_order(self, client_id):
        return dict(self.orders[client_id])

    async def cancel_order(self, client_id):
        row = self.orders[client_id]
        row["status"] = "CANCELED"
        return dict(row)

    async def place_stop(self, side, trigger, client_id):
        row = {"symbol": "ETHUSDT", "clientAlgoId": client_id,
               "algoId": self.next_stop_id, "side": side,
               "triggerPrice": str(trigger), "algoStatus": "NEW"}
        self.next_stop_id += 1
        self.stops[client_id] = row
        return dict(row)

    async def query_stop(self, client_id):
        return dict(self.stops[client_id])

    async def cancel_stop(self, client_id):
        self.stops.pop(client_id)
        return {"clientAlgoId": client_id, "code": "200"}

    def fill(self, client_id, trade_id):
        row = self.orders[client_id]
        qty = Decimal(row["origQty"])
        price = Decimal(row["price"])
        self.position_amt += qty if row["side"] == "BUY" else -qty
        row["status"] = "FILLED"
        row["executedQty"] = str(qty)
        return {"e": "ORDER_TRADE_UPDATE", "o": {
            "s": "ETHUSDT", "c": client_id, "i": row["orderId"],
            "S": row["side"], "x": "TRADE", "X": "FILLED",
            "l": str(qty), "z": str(qty), "L": str(price), "t": trade_id}}


def _decision(store, now, state):
    params = {"stop_loss_low": "1850", "stop_loss_high": "2150"}
    store.publish({"symbol": "ETHUSDT", "state": state,
                   "calculated_at_ms": now, "executable": True,
                   "last_closed_kline_ms": {"15m": now - 60_000,
                                            "1h": now - 60_000,
                                            "4h": now - 60_000},
                   "feasibility": {"feasible": state == "OSCILLATION"},
                   "grid_params": params, "margin_used": "100"}, "hash")
    return store.next_decision()


@pytest.mark.asyncio
async def test_buy_sell_pair_pause_exit_and_rebuild_cycle(tmp_path):
    path = tmp_path / "cycle.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = FakeExchange()
    engine = GridEngine(store, exchange)
    risk = RiskAssessment(RiskStatus.ALLOW, ())
    filters = await exchange.verify_account(10)
    levels = (Decimal("1900"), Decimal("1950"), Decimal("2000"), Decimal("2050"))
    plan = GridPlan((PlannedOrder(1, "BUY", levels[1], Decimal("0.02")),
                     PlannedOrder(3, "SELL", levels[3], Decimal("0.02"))),
                    levels, Decimal("80"), Decimal("8"))
    now = 1_800_000_000_000
    first = _decision(store, now, "OSCILLATION")
    assert await engine.apply_decision(first, plan, risk, leverage=10,
        now_ms=now + 1000, stop_loss_low=Decimal("1850"),
        stop_loss_high=Decimal("2150"))
    epoch = store.active_epoch_id()
    buy_id = next(k for k, v in exchange.orders.items() if v["side"] == "BUY")
    assert await engine.on_order_update(exchange.fill(buy_id, 1001), epoch_id=epoch,
        levels=levels, filters=filters, stop_loss_low=Decimal("1850"),
        stop_loss_high=Decimal("2150"))
    assert exchange.position_amt == Decimal("0.02")
    assert len(exchange.stops) == 1
    reverse_id = next(k for k, v in exchange.orders.items()
                      if v["side"] == "SELL" and v["price"] == "2000")
    assert await engine.on_order_update(exchange.fill(reverse_id, 1002), epoch_id=epoch,
        levels=levels, filters=filters, stop_loss_low=Decimal("1850"),
        stop_loss_high=Decimal("2150"))
    assert exchange.position_amt == 0
    assert not exchange.stops
    warning = _decision(store, now + 60_000, "EARLY_WARNING_15M")
    assert await engine.apply_decision(warning, None, risk, leverage=10,
        now_ms=now + 61_000, stop_loss_low=Decimal("1850"),
        stop_loss_high=Decimal("2150"))
    assert not await exchange.open_orders()
    trend = _decision(store, now + 120_000, "NORMAL_STRONG_TREND")
    assert await engine.apply_decision(trend, None, risk, leverage=10,
        now_ms=now + 121_000, stop_loss_low=Decimal("1850"),
        stop_loss_high=Decimal("2150"))
    assert store.engine_state()["state"] == "FLAT"
    next_signal = _decision(store, now + 180_000, "OSCILLATION")
    assert await engine.apply_decision(next_signal, plan, risk, leverage=10,
        now_ms=now + 181_000, stop_loss_low=Decimal("1850"),
        stop_loss_high=Decimal("2150"))
    assert store.active_epoch_id() != epoch
    assert len(await exchange.open_orders()) == 2
    store.close()
