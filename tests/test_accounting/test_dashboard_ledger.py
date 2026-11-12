from datetime import datetime, timedelta, timezone
from decimal import Decimal
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


def test_api_schema_preserves_realized_metric_provenance():
    from dashboard.backend.models.schemas import OverviewData, StrategySummary
    strategy = StrategySummary(id='hrs', name='HRS', perf_caliber='exchange_income_realized_daily_v1',
                               perf_reason='opening_capital_missing',
                               win_rate_reason='verified_sample_only', win_sample_partial=True)
    data = OverviewData(total_pnl='1', total_orders=None, total_closed=None, total_wins=None,
                        win_rate=None, strategies=[strategy], report_type='monthly', updated_at='now',
                        total_perf_caliber='exchange_income_realized_daily_v1',
                        total_perf_reason='zero_return_variance')
    payload = data.model_dump()
    assert payload['total_perf_reason'] == 'zero_return_variance'
    assert payload['strategies'][0]['perf_reason'] == 'opening_capital_missing'
    assert payload['strategies'][0]['win_sample_partial'] is True


def test_frontend_does_not_add_live_unrealized_or_zero_unknowns():
    from pathlib import Path
    root=Path.cwd()
    main=(root/'dashboard/frontend/js/main.js').read_text()
    charts=(root/'dashboard/frontend/js/charts.js').read_text()
    assert 'const pnlValue = realizedValue + unrealizedValue' not in main
    assert "if (isNaN(num)) return '0'" not in charts
    assert 'parseFloat(t.total_pnl) || 0' not in charts


@pytest.mark.asyncio
async def test_overview_attaches_exchange_income_performance_from_capital_snapshots():
    from datetime import date
    class DB:
        async def fetch_all(self, sql, *args):
            if 'public.equity_snapshot' in sql:
                return [{'snapshot_date': date(2026, 10, 31), 'total_equity': Decimal('100')},
                        {'snapshot_date': date(2026, 11, 1), 'total_equity': Decimal('100')},
                        {'snapshot_date': date(2026, 11, 2), 'total_equity': Decimal('100')}]
            if 'public.strategy_position_snapshot_history' in sql:
                return [{'strategy_id': 'hrs', 'snapshot_hour': datetime(2026, 11, day),
                         'allocated_amount': Decimal('50')} for day in (1, 2, 3)]
            raise AssertionError(sql)
    svc = Service()
    svc._db_manager = DB()
    svc._get_date_range = lambda _: (datetime(2026, 11, 1), datetime(2026, 11, 4))
    r = report()
    r.update(start_ms=1793462400000, end_ms=1793721600000,
             publication_status='published', strategy_attribution_complete=True,
             events=[{'asset': 'USDT', 'amount': amount, 'strategy': 'hrs',
                      'symbol': 'ETHUSDT', 'income_type': 'REALIZED_PNL',
                      'event_time_utc': datetime(2026, 11, day, 4, tzinfo=timezone.utc).isoformat()}
                     for day, amount in ((1, '5'), (2, '-10'), (3, '2'))])
    svc._ledger_report = AsyncMock(return_value=r)
    result = await svc.get_overview('monthly')
    assert result['total_perf_sample_count'] == 3
    assert result['total_max_drawdown'] is not None
    assert result['total_sharpe'] is not None
    assert result['strategies'][0]['perf_sample_count'] == 3
    assert result['strategies'][0]['max_drawdown'] is not None
    detail = await svc.get_strategy_detail('hrs', 'monthly')
    assert detail['sharpe'] == result['strategies'][0]['sharpe']
    assert detail['max_drawdown'] == result['strategies'][0]['max_drawdown']


@pytest.mark.asyncio
async def test_overview_counts_only_verified_post_baseline_closed_positions():
    from datetime import date
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    open_time = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
    close_time = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
    class DB:
        async def fetch_all(self, sql, *args):
            if 'public.equity_snapshot' in sql: return []
            if 'public.strategy_position_snapshot_history' in sql: return []
            if 'position_flat_baseline' in sql:
                return [{'account_id': 'default', 'symbol': 'ETHUSDT', 'flat_at': base,
                         'exchange_qty': Decimal(0)}]
            if 'execution_fact_fill' in sql:
                return [{'account_id': 'default', 'symbol': 'ETHUSDT', 'trade_id': tid,
                         'order_id': tid, 'side': side, 'qty': Decimal(1),
                         'realized_pnl': Decimal(gross), 'commission': Decimal('0.01'),
                         'commission_asset': 'USDT', 'trade_time_utc': stamp}
                        for tid, side, gross, stamp in ((1, 'BUY', '0', open_time),
                                                       (2, 'SELL', '2', close_time))]
            if 'order_strategy_evidence' in sql:
                return [{'account_id': 'default', 'symbol': 'ETHUSDT', 'order_id': tid,
                         'strategy': 'hrs'} for tid in (1, 2)]
            if 'execution_fact_income' in sql:
                return [{'symbol': 'ETHUSDT', 'trade_id': tid, 'tran_id': tid * 10 + (1 if kind == 'COMMISSION' else 2),
                         'income_type': kind, 'income': Decimal(amount)}
                        for tid, kind, amount in ((1, 'COMMISSION', '-0.01'),
                                                  (2, 'COMMISSION', '-0.01'),
                                                  (2, 'REALIZED_PNL', '2'))]
            if 'execution_fact_cursor' in sql:
                return [{'account_id': 'default', 'source': 'userTrades',
                         'income_type': None, 'symbol': 'ETHUSDT',
                         'window_start_ms': 1793462400000,
                         'covered_until_ms': 1793721600000,
                         'page_complete': True, 'request_status': 'ok'}]
            raise AssertionError(sql)
    svc = Service(); svc._db_manager = DB()
    r = report()
    r.update(start_ms=1793462400000, end_ms=1793721600000,
             account_id='default', publication_status='published',
             strategy_attribution_complete=True,
             events=[{'asset': 'USDT', 'symbol': 'ETHUSDT',
                      'tran_id': tid * 10 + (1 if kind == 'COMMISSION' else 2),
                      'income_type': kind, 'amount': amount, 'strategy': 'hrs'}
                     for tid, kind, amount in ((1, 'COMMISSION', '-0.01'),
                                               (2, 'COMMISSION', '-0.01'),
                                               (2, 'REALIZED_PNL', '2'))])
    svc._ledger_report = AsyncMock(return_value=r)
    result = await svc.get_overview('monthly')
    assert result['total_closed'] == 1
    assert result['total_orders'] == 2
    assert result['total_wins'] == 1
    assert result['win_rate'] == 100
    assert result['win_sample_partial'] is True
    assert result['strategies'][0]['closed_count'] == 1


@pytest.mark.asyncio
async def test_capital_query_failure_is_visible_without_hiding_exchange_pnl():
    class DB:
        async def fetch_all(self, *_):
            raise RuntimeError('database unavailable')
    svc = Service(); svc._db_manager = DB()
    r = report()
    r.update(start_ms=1793462400000, end_ms=1793721600000,
             publication_status='published', strategy_attribution_complete=True)
    svc._ledger_report = AsyncMock(return_value=r)
    result = await svc.get_overview('monthly')
    assert result['total_pnl'] == '5.80965338'
    assert result['total_perf_reason'] == 'capital_source_unavailable'
    assert result['win_rate_reason'] == 'lifecycle_source_unavailable'
