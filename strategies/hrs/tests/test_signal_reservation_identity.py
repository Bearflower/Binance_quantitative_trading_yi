from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from strategies.hrs.risk_budget import RiskDecision
from strategies.hrs.strategy import HRSStrategy


@pytest.mark.asyncio
async def test_hrs_reservation_records_durable_signal_identity():
    strategy = HRSStrategy.__new__(HRSStrategy)
    strategy.db = AsyncMock()
    strategy.binance_client = AsyncMock()
    strategy.trading_executor = SimpleNamespace(leverage=2)
    strategy.config = {"trading": {"max_positions": 3}}
    signal = {"symbol": "ONUSDT", "signal_id": 5682}
    decision = RiskDecision(
        allowed=True, reason="ok", quantity=Decimal("1"),
        worst_loss=Decimal("0.1"), exit_ratios=(Decimal("0.5"), Decimal("0")),
        policy_revision=4, effective_allocation=Decimal("30"),
    )
    store = SimpleNamespace(reconcile=AsyncMock(), reserve=AsyncMock(return_value="risk_5682"))
    with patch("shared.entry_risk_policy.RiskPolicyStore", return_value=store), \
         patch("strategies.hrs.strategy.try_claim_symbol",
               new=AsyncMock(return_value={"claimed": True, "claim_id": 1})):
        result = await strategy._reserve_hrs_entry(signal, decision, 10.0, 500.0)

    assert result.client_order_id == "risk_5682"
    assert store.reserve.await_args.kwargs["geometry"]["signal_id"] == 5682
