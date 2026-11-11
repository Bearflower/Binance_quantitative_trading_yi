from decimal import Decimal

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.planner import GridPlan, PlannedOrder
from strategies.grid.execution.risk import RiskAssessment, RiskStatus
from strategies.grid.execution.store import DecisionStore, migrate


class StartExchange:
    def __init__(self):
        self.ordinary = []
        self.sent = []

    async def open_orders(self):
        return list(self.ordinary)

    async def open_stops(self):
        return []

    async def position(self):
        return [{"symbol": "ETHUSDT", "positionSide": "BOTH",
                 "positionAmt": "0"}]

    async def verify_account(self, leverage):
        assert leverage == 10
        return object()

    async def account_info(self):
        return {"accountStatus": "NORMAL", "totalAvailableBalance": "100"}

    async def place_limit(self, side, qty, price, client_id):
        self.sent.append(client_id)
        row = {"symbol": "ETHUSDT", "clientOrderId": client_id,
               "orderId": len(self.sent), "status": "NEW", "executedQty": "0"}
        self.ordinary.append(row)
        return row


def _decision(store, now):
    payload = {"symbol": "ETHUSDT", "state": "OSCILLATION",
               "calculated_at_ms": now - 1000,
               "last_closed_kline_ms": {"15m": now - 60_000,
                                        "1h": now - 60_000,
                                        "4h": now - 60_000},
               "executable": True, "feasibility": {"feasible": True}}
    store.publish(payload, "hash")
    return store.next_decision()


@pytest.mark.asyncio
async def test_start_journals_before_exchange_write_and_acks_last(tmp_path):
    path = tmp_path / "start.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = StartExchange()
    now = 1_800_000_000_000
    decision = _decision(store, now)
    plan = GridPlan((PlannedOrder(0, "BUY", Decimal("1900"), Decimal("0.02")),),
                    (Decimal("1900"), Decimal("1950")), Decimal("38"), Decimal("3.8"))
    engine = GridEngine(store, exchange)
    assert await engine.start_grid(decision, plan,
                                   RiskAssessment(RiskStatus.ALLOW, ()),
                                   leverage=10, now_ms=now) is True
    assert store.cursor() == decision["decision_id"]
    assert store.engine_state()["state"] == "ACTIVE"
    assert len(exchange.sent) == 1
    assert store.get_order(exchange.sent[0])["status"] == "NEW"
    store.close()


@pytest.mark.asyncio
async def test_pm_zero_available_collateral_blocks_opening(tmp_path):
    path = tmp_path / "no_collateral.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = StartExchange()

    async def no_collateral():
        return {"accountStatus": "NORMAL", "accountEquity": "0",
                "totalAvailableBalance": "0"}

    exchange.account_info = no_collateral
    now = 1_800_000_000_000
    decision = _decision(store, now)
    plan = GridPlan((PlannedOrder(0, "BUY", Decimal("1900"), Decimal("0.02")),),
                    (Decimal("1900"), Decimal("1950")), Decimal("38"), Decimal("3.8"))
    assert await GridEngine(store, exchange).start_grid(
        decision, plan, RiskAssessment(RiskStatus.ALLOW, ()),
        leverage=10, now_ms=now) is False
    assert exchange.sent == []
    assert store.engine_state()["state"] == "LOCKED"
    store.close()


@pytest.mark.asyncio
async def test_stale_decision_or_risk_lock_never_writes(tmp_path):
    path = tmp_path / "start.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = StartExchange()
    now = 1_800_000_000_000
    decision = _decision(store, now)
    plan = GridPlan((PlannedOrder(0, "BUY", Decimal("1900"), Decimal("0.02")),),
                    (Decimal("1900"), Decimal("1950")), Decimal("38"), Decimal("3.8"))
    engine = GridEngine(store, exchange)
    assert await engine.start_grid(decision, plan,
                                   RiskAssessment(RiskStatus.UNKNOWN_LOCKED, ("missing",)),
                                   leverage=10, now_ms=now) is False
    assert exchange.sent == []
    store.close()
