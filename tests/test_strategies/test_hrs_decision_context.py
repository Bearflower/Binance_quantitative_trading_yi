from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_capture_financial_context_keeps_actual_values_and_unready_ledger_explicit():
    from strategies.hrs.decision_context import capture_financial_context
    strategy = SimpleNamespace(binance_client=SimpleNamespace(get_account_info=AsyncMock(
        return_value={'totalMarginBalance': '123.456', 'totalWalletBalance': '123', 'api_key': 'hidden'})),
        capital_mgr=SimpleNamespace(get_effective_margin_limit=AsyncMock(return_value=(52.31, 'db'))),
        trading_executor=SimpleNamespace(timeout_recovery=SimpleNamespace(pending=AsyncMock(return_value=[]))),
        db=SimpleNamespace(fetch_all=AsyncMock(side_effect=[[{'revision': 6}], [], [], [], []])))
    result = await capture_financial_context(strategy)
    assert result['account_equity'] == '123.456'
    assert result['allocation'] == '52.31'
    assert result['ledger_state'] is None
    assert result['replay_bootstrap_ready'] is False
    assert result['pending_timeout_entries'] == []
    assert 'hidden' not in str(result)
    assert set(result['state_tables']) == {'hrs_positions', 'hrs_blacklist', 'hrs_active_symbols'}


@pytest.mark.asyncio
async def test_missing_account_equity_is_not_filled_with_zero():
    from strategies.hrs.decision_context import capture_financial_context
    strategy = SimpleNamespace(binance_client=SimpleNamespace(get_account_info=AsyncMock(return_value={})),
        capital_mgr=SimpleNamespace(get_effective_margin_limit=AsyncMock(return_value=(52.31, 'db'))),
        trading_executor=SimpleNamespace(timeout_recovery=SimpleNamespace(pending=AsyncMock(return_value=[]))),
        db=SimpleNamespace(fetch_all=AsyncMock(side_effect=[[{'revision': 6}], [], [], [], []])))
    with pytest.raises(ValueError, match='account_equity_missing'):
        await capture_financial_context(strategy)
