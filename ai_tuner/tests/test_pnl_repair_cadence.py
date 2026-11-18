from unittest.mock import AsyncMock, patch
import pytest
from ai_tuner.main import StratTuneAI


@pytest.mark.asyncio
async def test_every_scheduled_reconcile_repairs_before_reading_income():
    app = StratTuneAI.__new__(StratTuneAI)
    events = []
    app.reconciler = AsyncMock()
    app.reconciler.run_once.side_effect = lambda: events.append('reconcile')
    async def repair(rec):
        assert rec is app.reconciler
        events.append('repair')
    with patch('ai_tuner.main.nightly_exit_repair', side_effect=repair):
        await app._scheduled_pnl_reconcile()
        await app._scheduled_pnl_reconcile()
    assert events == ['repair', 'reconcile', 'repair', 'reconcile']


@pytest.mark.asyncio
async def test_repair_query_failure_does_not_run_reconcile():
    app = StratTuneAI.__new__(StratTuneAI)
    app.reconciler = AsyncMock()
    with patch('ai_tuner.main.nightly_exit_repair', side_effect=RuntimeError('database unavailable')):
        with pytest.raises(RuntimeError, match='database unavailable'):
            await app._scheduled_pnl_reconcile()
    app.reconciler.run_once.assert_not_awaited()
