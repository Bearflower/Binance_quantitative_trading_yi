"""A data denial and an unexpected cycle exception have distinct diagnostics."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest


def loop_with_exception(monkeypatch, error):
    import strategies.hrs.strategy as module
    item = module.HRSStrategy.__new__(module.HRSStrategy)
    item._paused = False
    item.retry_delay = 0
    item.check_interval = 1
    item._recover_timeout_entries = AsyncMock()
    item._align_to_hour = AsyncMock()
    item._wait_with_timeout_recovery = AsyncMock()
    item._is_sleep_on_empty = lambda: False
    attempts = []
    async def execute():
        attempts.append(True)
        if len(attempts) == 1:
            raise error
        item._running = False
    item._execute_cycle = execute
    logger = Mock()
    monkeypatch.setattr(module, 'logger', logger)
    return item, logger


@pytest.mark.asyncio
async def test_unexpected_empty_error_retains_type_and_frames_and_next_cycle_runs(monkeypatch):
    item, logger = loop_with_exception(monkeypatch, TimeoutError())
    await item.run()
    detail = logger.error.call_args.kwargs
    assert detail['error'] == 'TimeoutError'
    assert detail['error_type'] == 'TimeoutError'
    assert any(frame['function'] == 'execute' for frame in detail['exception_frames'])
    item._wait_with_timeout_recovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_expected_unready_scan_is_reported_with_real_source_reason(monkeypatch):
    from strategies.hrs.decision_context import ScanContextUnavailable
    error = ScanContextUnavailable('scan_boundary_source_denied', phase='equity_refresh',
                                   reason_codes=['source_collection_incomplete'], elapsed_seconds=2)
    item, logger = loop_with_exception(monkeypatch, error)
    await item.run()
    logger.error.assert_not_called()
    warning = logger.warning.call_args.kwargs
    assert warning['reason_codes'] == ['source_collection_incomplete']
    assert warning['phase'] == 'equity_refresh'
    assert warning['error'] == 'scan_boundary_source_denied'
    item._wait_with_timeout_recovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_cycle_cancellation_still_propagates_without_error_or_retry(monkeypatch):
    item, logger = loop_with_exception(monkeypatch, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await item.run()
    logger.error.assert_not_called()
    logger.warning.assert_not_called()
    item._wait_with_timeout_recovery.assert_not_awaited()


def test_diagnostics_do_not_persist_signed_urls_or_cause_objects():
    import json
    import aiohttp
    from types import SimpleNamespace
    from strategies.hrs.decision_context import cycle_failure_diagnostics
    try:
        raise aiohttp.ContentTypeError(SimpleNamespace(real_url='https://papi.binance.com/?signature=SENSITIVE'),
                                       (), status=502, message='Unexpected mimetype')
    except aiohttp.ContentTypeError as error:
        detail = cycle_failure_diagnostics(error)
    assert 'SENSITIVE' not in json.dumps(detail)
    assert detail['error_type'] == 'ContentTypeError'
    assert detail['exception_frames']


@pytest.mark.asyncio
async def test_real_collector_programming_failure_denial_still_emits_error(monkeypatch):
    from types import SimpleNamespace
    from strategies.hrs.equity_service import HrsEquityService
    from strategies.hrs import decision_context
    db = SimpleNamespace(pool=object(), fetch_one=AsyncMock(return_value=None))
    service = HrsEquityService(db, None, None, None)
    service._capture = AsyncMock(side_effect=TypeError('synthetic collector defect'))
    async def persisted_denial(reason):
        return dict(ready=False, allowed=False, policy_revision=0, reasons=[reason])
    service._deny = AsyncMock(side_effect=persisted_denial)
    with pytest.raises(decision_context.ScanContextUnavailable) as error:
        await decision_context.capture_scan_financial_context(SimpleNamespace(equity_service=service))
    service._deny.assert_awaited_once_with('TypeError')
    item, logger = loop_with_exception(monkeypatch, error.value)
    await item.run()
    logger.warning.assert_not_called()
    logger.error.assert_called_once()
    assert logger.error.call_args.kwargs['reason_codes'] == ['TypeError']
