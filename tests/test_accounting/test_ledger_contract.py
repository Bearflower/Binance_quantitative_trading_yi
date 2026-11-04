from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest
from ai_tuner.accounting.economic_ledger import build_economic_report
from ai_tuner.accounting.report_reader import read_monthly_economic_report, _INCOME_SQL


def income(kind, amount, tran=1):
    return dict(account_id='main', tran_id=tran, symbol='ETHUSDT', income_type=kind,
                income=Decimal(amount), asset='USDT', trade_id=None,
                event_time_utc=datetime(2026, 10, 1, tzinfo=timezone.utc))


def test_transaction_identity_includes_income_type_and_deduplicates():
    rows = [income('REALIZED_PNL', '8.28318681'), income('COMMISSION', '-5.89624705'),
            income('FUNDING_FEE', '3.42271362')]
    report = build_economic_report([], rows + rows, [])
    assert report['account_usdt_total'] == '5.80965338'
    assert report['account_components']['funding_usdt'] == '3.42271362'
    assert len(report['events']) == 3


@pytest.mark.asyncio
async def test_account_coverage_independent_from_unproven_strategy():
    cursors = [dict(account_id='main', source='um/income', income_type=kind, symbol=None,
                    window_start_ms=1000, covered_until_ms=2000, page_complete=True, request_status='ok')
               for kind in ('REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE')]
    db = AsyncMock()
    db.fetch_all.side_effect = [[income('FUNDING_FEE', '1')], [], [], cursors]
    report = await read_monthly_economic_report(db, 'main', 1000, 2000)
    assert report['account_coverage_complete'] is True
    assert report['strategy_attribution_complete'] is False
    assert report['report_id']
    assert 'event_time_utc' in _INCOME_SQL


@pytest.mark.asyncio
async def test_missing_coverage_does_not_assert_zero_is_verified():
    db = AsyncMock()
    db.fetch_all.return_value = []
    report = await read_monthly_economic_report(db, 'main', 1000, 2000)
    assert report['account_coverage_complete'] is False
    assert report['coverage_gaps']
