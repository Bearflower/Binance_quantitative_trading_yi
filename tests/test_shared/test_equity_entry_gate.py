from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest


def projection(**changes):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    return dict(account_id='test-account', strategy_id='hrs', policy_revision=6,
                ready=True, allowed=True, as_of=now, valid_until=now+timedelta(seconds=90),
                checked_at=now+timedelta(seconds=5), **changes)


@pytest.mark.asyncio
async def test_nonconfigured_strategies_keep_existing_policy(monkeypatch):
    from shared.equity_entry_gate import enforce_equity_gate
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES', 'hrs')
    conn = AsyncMock()
    await enforce_equity_gate(conn, 'btc_eth', 'test-account', 6)
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_equity_projection_requires_income_integrity(monkeypatch):
    from shared.equity_entry_gate import enforce_equity_gate
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES', 'hrs')
    conn = AsyncMock()
    conn.fetchrow.return_value = projection()
    conn.fetchval.return_value = 0
    await enforce_equity_gate(conn, 'hrs', 'test-account', 6)
    assert any('income_ingestion_attempt' in call.args[0] for call in conn.fetchval.await_args_list)
    assert any('entry_risk_reservation' in call.args[0] for call in conn.fetchval.await_args_list)
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [None, {'ready': False}, {'allowed': False},
    {'policy_revision': 5}, {'as_of': datetime(2026, 11, 18, tzinfo=timezone.utc)},
    {'valid_until': datetime(2026, 11, 16, tzinfo=timezone.utc)},
    {'valid_until': datetime(2026, 12, 1, tzinfo=timezone.utc)}])
async def test_missing_denied_stale_future_or_wrong_policy_projection_blocks(monkeypatch, change):
    from shared.equity_entry_gate import enforce_equity_gate, EquityGateDenied
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES', 'hrs')
    conn = AsyncMock()
    row = projection()
    if change:
        row.update(change)
    conn.fetchrow.return_value = None if change is None else row
    conn.fetchval.return_value = 0
    with pytest.raises(EquityGateDenied):
        await enforce_equity_gate(conn, 'hrs', 'test-account', 6)
