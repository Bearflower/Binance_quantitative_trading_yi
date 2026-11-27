from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_tuner.reconciler import income_reconciler as module


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,expected', [
    ('缺少有效的 PM 期末或经典期初权益', True),
    ('signed account identity mismatch', False),
])
async def test_missing_cutover_evidence_preserves_cursor_and_identity_errors(monkeypatch, reason, expected):
    monkeypatch.setattr(module, 'account_cutover_ms', lambda _: 1795000000000)
    gate = AsyncMock(side_effect=ValueError(reason))
    monkeypatch.setattr(module, 'verify_cutover_ledger', gate)
    logger = Mock()
    monkeypatch.setattr(module, 'logger', logger)
    item = module.IncomeReconciler(Mock(), SimpleNamespace(use_unified_account=False), {})
    item._ensure_state_table = AsyncMock()
    item._fetch_income_records = AsyncMock()
    item._update_reconciled_ts = AsyncMock()
    await item.run_once()
    gate.assert_awaited_once()
    item._ensure_state_table.assert_not_awaited()
    item._fetch_income_records.assert_not_awaited()
    item._update_reconciled_ts.assert_not_awaited()
    if expected:
        logger.error.assert_not_called()
        assert logger.warning.call_args.kwargs['status'] == 'BLOCKED_DATA'
    else:
        logger.error.assert_called_once()
        logger.warning.assert_not_called()
