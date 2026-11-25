from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution.risk import RiskLimits, RiskStatus, beijing_day_start_ms
from strategies.grid.execution.risk_tracker import RiskTracker
from strategies.grid.execution.store import DecisionStore, migrate


@pytest.mark.asyncio
async def test_drawdown_survives_days_and_restarts(tmp_path):
    path = tmp_path / 'risk.sqlite'
    migrate(path)
    exchange = AsyncMock()
    exchange.income.return_value = []
    exchange.position.return_value = [{'positionAmt': '0'}]
    day = beijing_day_start_ms(1800000000000)
    for index in range(5):
        store = DecisionStore(path)
        tracker = RiskTracker(store, exchange, RiskLimits(Decimal(15), Decimal(30)))
        exchange.account_info.return_value = {'accountEquity': str(300 - 7 * index)}
        await tracker.assess(day + index * 86400000 + 1000)
        exchange.account_info.return_value = {'accountEquity': str(293 - 7 * index)}
        result = await tracker.assess(day + index * 86400000 + 300000)
        store.close()
    assert result.status == RiskStatus.EXIT_LOCKED
    assert result.drawdown_usdt == 35
