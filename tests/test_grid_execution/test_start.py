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


@pytest.mark.asyncio
@pytest.mark.parametrize('rejected_index', [0, 1])
async def test_maker_rejection_cleans_up_and_consumes_signal_without_unknown_intent(tmp_path, rejected_index):
    from strategies.grid.execution.exchange import MakerOrderRejected
    from unittest.mock import AsyncMock
    path = tmp_path / 'rejected.sqlite3'
    migrate(path)
    store = DecisionStore(path)
    exchange = StartExchange()
    accepted = exchange.place_limit
    async def place(side, qty, price, cid):
        if len(exchange.sent) == rejected_index:
            raise MakerOrderRejected(cid)
        return await accepted(side, qty, price, cid)
    async def cancel(cid):
        row = next(r for r in exchange.ordinary if r['clientOrderId'] == cid)
        exchange.ordinary.remove(row)
        return dict(row, status='CANCELED')
    exchange.place_limit, exchange.cancel_order = place, AsyncMock(side_effect=cancel)
    now = 1_800_000_000_000
    decision = _decision(store, now)
    plan = GridPlan(tuple(PlannedOrder(i, 'BUY', Decimal(1900+50*i), Decimal('0.02')) for i in range(2)),
                    (Decimal('1900'), Decimal('1950')), Decimal('77'), Decimal('7.7'))
    assert not await GridEngine(store, exchange).start_grid(decision, plan,
        RiskAssessment(RiskStatus.ALLOW, ()), leverage=10, now_ms=now)
    assert store.engine_state()['state'] == 'FLAT'
    assert store.cursor() == decision['decision_id']
    assert not store.active_orders() and not exchange.ordinary
    rejected = [r for r in store.all_orders() if r['status'] == 'REJECTED']
    assert len(rejected) == 1 and not rejected[0]['exchange_order_id']
    assert store.conn.execute("SELECT value FROM schema_meta WHERE key=?",
                             ('order_rejection:'+rejected[0]['client_order_id'],)).fetchone()
    assert exchange.cancel_order.await_count == rejected_index
    store.close()
