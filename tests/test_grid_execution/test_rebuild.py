from decimal import Decimal

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.planner import GridPlan, PlannedOrder
from strategies.grid.execution.risk import RiskAssessment, RiskStatus
from strategies.grid.execution.store import DecisionStore, migrate


class CycleExchange:
    def __init__(self):
        self.ordinary = []
        self.next_id = 1

    async def verify_account(self, leverage):
        assert leverage == 10
        return object()

    async def account_info(self):
        return {"totalAvailableBalance": "100", "accountStatus": "NORMAL"}

    async def position(self):
        return [{"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0"}]

    async def open_orders(self):
        return list(self.ordinary)

    async def open_stops(self):
        return []

    async def place_limit(self, side, qty, price, client_id):
        row = {"symbol": "ETHUSDT", "clientOrderId": client_id,
               "orderId": self.next_id, "status": "NEW", "executedQty": "0"}
        self.next_id += 1
        self.ordinary.append(row)
        return row

    async def cancel_order(self, client_id):
        row = next(x for x in self.ordinary if x["clientOrderId"] == client_id)
        self.ordinary.remove(row)
        return {**row, "status": "CANCELED"}


def _publish(store, now, state):
    payload = {"symbol": "ETHUSDT", "state": state,
               "calculated_at_ms": now,
               "last_closed_kline_ms": {"15m": now - 60_000,
                                        "1h": now - 60_000,
                                        "4h": now - 60_000},
               "executable": True,
               "feasibility": {"feasible": state == "OSCILLATION"}}
    store.publish(payload, "hash")
    return store.next_decision()


@pytest.mark.asyncio
async def test_warning_pause_then_confirmed_exit_then_auto_rebuild(tmp_path):
    path = tmp_path / "cycle.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = CycleExchange()
    engine = GridEngine(store, exchange)
    plan = GridPlan((PlannedOrder(0, "BUY", Decimal("1900"), Decimal("0.02")),),
                    (Decimal("1900"), Decimal("1950")), Decimal("38"), Decimal("3.8"))
    risk = RiskAssessment(RiskStatus.ALLOW, ())
    now = 1_800_000_000_000
    first = _publish(store, now, "OSCILLATION")
    assert await engine.apply_decision(first, plan, risk, leverage=10,
                                       now_ms=now + 1000,
                                       stop_loss_low=Decimal("1850"),
                                       stop_loss_high=Decimal("2150")) is True
    first_epoch = store.active_epoch_id()
    warning = _publish(store, now + 60_000, "EARLY_WARNING_15M")
    assert await engine.apply_decision(warning, plan, risk, leverage=10,
                                       now_ms=now + 61_000,
                                       stop_loss_low=Decimal("1850"),
                                       stop_loss_high=Decimal("2150")) is True
    assert store.engine_state()["state"] == "PAUSED"
    assert not exchange.ordinary
    trend = _publish(store, now + 120_000, "NORMAL_STRONG_TREND")
    assert await engine.apply_decision(trend, plan, risk, leverage=10,
                                       now_ms=now + 121_000,
                                       stop_loss_low=Decimal("1850"),
                                       stop_loss_high=Decimal("2150")) is True
    assert store.engine_state()["state"] == "FLAT"
    assert store.active_epoch_id() is None
    fresh = _publish(store, now + 180_000, "OSCILLATION")
    assert await engine.apply_decision(fresh, plan, risk, leverage=10,
                                       now_ms=now + 181_000,
                                       stop_loss_low=Decimal("1850"),
                                       stop_loss_high=Decimal("2150")) is True
    assert store.active_epoch_id() != first_epoch
    assert len(exchange.ordinary) == 1
    store.close()


@pytest.mark.asyncio
async def test_changed_stop_boundary_is_not_acked_as_unchanged_plan(tmp_path):
    path = tmp_path / "stops.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = CycleExchange()
    engine = GridEngine(store, exchange)
    plan = GridPlan((PlannedOrder(0, "BUY", Decimal("1900"), Decimal("0.02")),),
                    (Decimal("1900"), Decimal("1950")), Decimal("38"), Decimal("3.8"))
    risk = RiskAssessment(RiskStatus.ALLOW, ())
    now = 1_800_000_000_000
    first = _publish(store, now, "OSCILLATION")
    first["grid_params"] = {"stop_loss_low": "1850", "stop_loss_high": "2150"}
    assert await engine.apply_decision(first, plan, risk, leverage=10,
        now_ms=now + 1000, stop_loss_low=Decimal("1850"),
        stop_loss_high=Decimal("2150"))
    old_epoch = store.active_epoch_id()
    changed = _publish(store, now + 60_000, "OSCILLATION")
    changed["grid_params"] = {"stop_loss_low": "1840", "stop_loss_high": "2150"}
    assert await engine.apply_decision(changed, plan, risk, leverage=10,
        now_ms=now + 61_000, stop_loss_low=Decimal("1840"),
        stop_loss_high=Decimal("2150"))
    assert store.active_epoch_id() != old_epoch
    store.close()
