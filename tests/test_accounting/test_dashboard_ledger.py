from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
from dashboard.backend.services.exchange_ledger import ExchangeLedgerDashboardMixin, period_windows

class Service(ExchangeLedgerDashboardMixin):
    _STRATEGY_NAME_MAP = {'hrs': 'HRS'}
    _STRATEGY_EMOJI_MAP = {}
    _ensure_initialized = AsyncMock()
    get_open_positions_summary = AsyncMock(return_value={})
    _query_unrealized_pnl = AsyncMock(return_value=42)
    def _get_date_range(self, kind):
        return datetime(2026, 10, 1), datetime(2026, 11, 1)


def report(complete=True):
    return dict(account_components=dict(net_usdt='5.80965338', realized_pnl_usdt='8.28318681', commission_usdt='-5.89624705', funding_usdt='3.42271362'),
                account_usdt_total='5.80965338', unattributed_usdt_total='5.80965338', by_strategy={}, events=[],
                account_coverage_complete=complete, strategy_attribution_complete=False, report_id='a', report_version='exchange-income-v1',
                start_ms=1000, end_ms=2000, requested_end_ms=2000, coverage_gaps=[], other_asset_totals={}, unattributed=[])

@pytest.mark.asyncio
async def test_overview_keeps_account_money_but_unknown_strategy_and_trade_metrics():
    svc=Service(); svc._ledger_report=AsyncMock(return_value=report())
    result=await svc.get_overview('monthly')
    assert result['total_pnl']=='5.80965338'
    assert result['total_funding']=='3.42271362'
    assert result['strategies'][0]['total_pnl'] is None
    assert result['win_rate'] is None
    assert result['unattributed_pnl']=='5.80965338'

@pytest.mark.asyncio
async def test_partial_account_is_unknown_with_observed_amount_retained():
    svc=Service(); svc._ledger_report=AsyncMock(return_value=report(False))
    result=await svc.get_overview()
    assert result['total_pnl'] is None
    assert result['observed_pnl']=='5.80965338'

@pytest.mark.asyncio
async def test_reader_failure_propagates_without_legacy_fallback():
    svc=Service(); svc._ledger_report=AsyncMock(side_effect=RuntimeError('database unavailable'))
    with pytest.raises(RuntimeError): await svc.get_overview()


def test_month_windows_are_contiguous_and_current_end_is_now():
    now=datetime(2026,11,4,8,tzinfo=timezone(timedelta(hours=8)))
    windows=period_windows('monthly',3,now)
    assert windows[0][0].day==1
    assert windows[0][1]==windows[1][0]
    assert windows[-1][1]==now


def test_api_schema_preserves_unknowns_and_coverage_metadata():
    from dashboard.backend.models.schemas import OverviewData
    data=OverviewData(total_pnl=None,total_orders=None,total_closed=None,total_wins=None,
                      win_rate=None,strategies=[],report_type='daily',updated_at='now',
                      account_coverage_complete=False,unattributed_pnl='5',report_id='a')
    assert data.total_pnl is None
    assert data.report_id=='a'
    assert data.unattributed_pnl=='5'


def test_frontend_does_not_add_live_unrealized_or_zero_unknowns():
    from pathlib import Path
    root=Path.cwd()
    main=(root/'dashboard/frontend/js/main.js').read_text()
    charts=(root/'dashboard/frontend/js/charts.js').read_text()
    assert 'const pnlValue = realizedValue + unrealizedValue' not in main
    assert "if (isNaN(num)) return '0'" not in charts
    assert 'parseFloat(t.total_pnl) || 0' not in charts
