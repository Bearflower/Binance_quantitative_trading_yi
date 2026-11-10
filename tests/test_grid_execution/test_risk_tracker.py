from decimal import Decimal

import pytest

from strategies.grid.execution.risk import RiskLimits, RiskStatus, beijing_day
from strategies.grid.execution.risk_tracker import RiskTracker
from strategies.grid.execution.store import DecisionStore, migrate


class FakeRiskExchange:
    def __init__(self, equity="100"):
        self.equity = equity
        self.income_rows = []

    async def account_info(self):
        return {"accountStatus": "NORMAL", "accountEquity": self.equity,
                "totalAvailableBalance": self.equity}

    async def income(self, start_ms, end_ms):
        return list(self.income_rows)

    async def position(self):
        return [{"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0"}]


@pytest.mark.asyncio
async def test_fresh_flat_subaccount_bootstraps_verified_day_baseline(tmp_path):
    path = tmp_path / "risk.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = FakeRiskExchange()
    tracker = RiskTracker(store, exchange, RiskLimits(Decimal("10"), Decimal("20")))
    now = 1_800_000_000_000
    result = await tracker.assess(now)
    assert result.status == RiskStatus.ALLOW
    assert store.latest_equity_checkpoint(beijing_day(now))["baseline_equity"] == "100"
    exchange.equity = "96"
    result = await tracker.assess(now + 60_000)
    assert result.status == RiskStatus.EXIT_LOCKED
    store.close()


@pytest.mark.asyncio
async def test_transfer_or_missing_baseline_locks(tmp_path):
    path = tmp_path / "risk.sqlite3"
    migrate(path)
    store = DecisionStore(path)
    exchange = FakeRiskExchange()
    exchange.income_rows = [{"incomeType": "TRANSFER", "income": "50",
                             "asset": "USDT", "time": 1_800_000_000_000}]
    tracker = RiskTracker(store, exchange, RiskLimits(Decimal("10"), Decimal("20")))
    assert (await tracker.assess(1_800_000_000_000)).status == RiskStatus.UNKNOWN_LOCKED
    store.close()
