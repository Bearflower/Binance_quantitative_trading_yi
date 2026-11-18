from unittest.mock import AsyncMock, patch
import pytest
from ai_tuner.reconciler.income_reconciler import IncomeReconciler, ExecutionFactsPending


def reconciler():
    return IncomeReconciler(AsyncMock(), AsyncMock(), {})


def test_recent_empty_evidence_is_pending_and_overdue_is_error():
    rec = reconciler()
    with patch('ai_tuner.reconciler.income_reconciler._utc_now_ms', return_value=6_000_000):
        with pytest.raises(ExecutionFactsPending):
            rec._require_arrived_facts([], 'BTCUSDT', 'tranId=1', 5_000_000)
        with pytest.raises(ValueError) as error:
            rec._require_arrived_facts([], 'BTCUSDT', 'tranId=1', 100_000)
        assert not isinstance(error.value, ExecutionFactsPending)


@pytest.mark.asyncio
async def test_pending_does_not_advance_cursor_and_next_run_recovers():
    rec = reconciler()
    rec._ensure_state_table = AsyncMock()
    rec._get_last_reconciled_ts = AsyncMock(return_value=1)
    rec._fetch_income_records = AsyncMock(return_value=[{'time': 2}])
    rec._match_and_update = AsyncMock(side_effect=[ExecutionFactsPending('not arrived'), (1, 0)])
    rec._update_reconciled_ts = AsyncMock()
    with patch('ai_tuner.reconciler.income_reconciler.logger') as log:
        await rec.run_once()
        log.warning.assert_called_once()
        log.error.assert_not_called()
        rec._update_reconciled_ts.assert_not_awaited()
        await rec.run_once()
        assert rec._match_and_update.await_count == 2


@pytest.mark.asyncio
async def test_existing_mismatched_income_is_error_without_grace():
    rec = reconciler()
    rec.db.fetch_all.return_value = [{'symbol': 'BTCUSDT', 'trade_id': 123, 'income': '9'}]
    event = {'symbol': 'BTCUSDT', 'time': 100, 'tradeId': 123, 'tranId': 456, 'income': '1'}
    with pytest.raises(ValueError, match='不一致') as error:
        await rec._match_and_update([event])
    assert not isinstance(error.value, ExecutionFactsPending)
    rec.db.execute_transaction.assert_not_awaited()


@pytest.mark.parametrize('age', [90 * 60000, 91 * 60000, -1])
def test_deadline_and_future_event_are_not_pending(age):
    rec = reconciler()
    with patch('ai_tuner.reconciler.income_reconciler._utc_now_ms', return_value=10_000_000):
        with pytest.raises(ValueError) as error:
            rec._require_arrived_facts([], 'DOGEUSDT', 'tradeId=2', 10_000_000-age)
        assert not isinstance(error.value, ExecutionFactsPending)


@pytest.mark.asyncio
async def test_recent_missing_fact_does_not_hide_later_symbol_conflict():
    rec = reconciler()
    rec.db.fetch_all.side_effect = [
        [],
        [{'symbol': 'BTCUSDT', 'trade_id': 222, 'income': '1'}],
    ]
    first = {'symbol': 'BTCUSDT', 'time': 100, 'tradeId': 123, 'tranId': 456, 'income': '1'}
    second = dict(first, symbol='DOGEUSDT', tradeId=222, tranId=789)
    with patch('ai_tuner.reconciler.income_reconciler._utc_now_ms', return_value=101):
        with pytest.raises(ValueError, match='DOGEUSDT tranId=789.*不一致') as error:
            await rec._match_and_update([first, second], completed_through=101)
    assert not isinstance(error.value, ExecutionFactsPending)
    rec.db.execute_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_income_before_cursor_is_rechecked():
    rec = reconciler()
    now = 10_000_000
    last_ts = now - 30 * 60_000
    late_event = {'time': last_ts - 60_000}
    rec._ensure_state_table = AsyncMock()
    rec._get_last_reconciled_ts = AsyncMock(return_value=last_ts)
    rec._match_and_update = AsyncMock(return_value=(1, 0))

    async def read_income(start_time, end_time, **_):
        return [late_event] if start_time <= late_event['time'] <= end_time else []

    rec.binance.get_income_history = AsyncMock(side_effect=read_income)
    with patch('ai_tuner.reconciler.income_reconciler._utc_now_ms', return_value=now):
        await rec.run_once()
    rec._match_and_update.assert_awaited_once_with([late_event], completed_through=now)


@pytest.mark.asyncio
async def test_full_page_on_two_adjacent_milliseconds_splits_without_overlap():
    rec = reconciler()
    rec.batch_size = 2
    records = [{'tranId': 1, 'time': 100}, {'tranId': 2, 'time': 101}]

    async def read_income(start_time, end_time, **_):
        return [row for row in records if start_time <= row['time'] <= end_time]

    rec.binance.get_income_history = AsyncMock(side_effect=read_income)
    assert await rec._fetch_income_records(100, 101) == records


def test_conflicting_duplicate_transaction_is_rejected():
    record = {'tranId': 1, 'time': 100, 'income': '1'}
    with pytest.raises(ValueError, match='tranId=1.*冲突'):
        reconciler()._dedupe_income([record, dict(record, income='2')])
