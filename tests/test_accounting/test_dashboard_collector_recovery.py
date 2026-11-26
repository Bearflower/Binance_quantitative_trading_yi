from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard/backend"))
from dashboard.backend.services import data_service_docker as module
from shared.accounting.equity_bridge import classic_position_owner


@pytest.mark.asyncio
async def test_bad_recovery_cannot_initialize_dashboard(monkeypatch):
    client = SimpleNamespace(close=AsyncMock())
    db = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
    monkeypatch.setattr(module, "DatabaseManager", lambda **kw: db)
    monkeypatch.setattr(module, "BinanceClient", lambda **kw: client)
    monkeypatch.setattr(module, "verify_trading_mode", AsyncMock())
    monkeypatch.setattr(module, "verify_collector_ledger", AsyncMock(side_effect=ValueError("bad proof")))
    service = module.DataService()
    with pytest.raises(ValueError, match="bad proof"):
        await service._ensure_initialized()
    assert not service._initialized
    client.close.assert_awaited_once()
    db.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("covered,quantity,owner,expected", [
    (True, "0.04", "btc_eth_aggressive", "btc_eth_aggressive"),
    (False, "0.04", "btc_eth_aggressive", None),
    (True, "0.05", "btc_eth_aggressive", None),
    (True, "0.04", None, None),
])
async def test_signed_flat_recovery_still_requires_complete_owned_fill_path(monkeypatch, covered, quantity, owner, expected):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "default")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    dt = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc)
    cursors = [dict(account_id="default", source="fapi/userTrades", symbol="BNBUSDT",
                    income_type=None, window_start_ms=1000, covered_until_ms=3001,
                    page_complete=True, request_status="ok")] if covered else []
    fills = [dict(account_id="default", symbol="BNBUSDT", trade_id=1, order_id=2,
                  side="BUY", qty="0.04", trade_time_utc=dt(2000))]
    owners = [dict(account_id="default", symbol="BNBUSDT", order_id=2, strategy=owner)] if owner else []
    db = SimpleNamespace(fetch_one=AsyncMock(return_value={"flat_at": None}),
                         fetch_all=AsyncMock(side_effect=[cursors, fills, owners, []]))
    pos = dict(symbol="BNBUSDT", positionSide="BOTH", updateTime=3000, positionAmt=quantity)
    assert await classic_position_owner(db, pos, 1000, verified_flat_at=dt(1500)) == expected
