"""Dashboard projections of the canonical exchange-income ledger.

No inferred symbol ownership, lifecycle win rate or legacy PNL fallback.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os

from shared.accounting.published_report import read_published_report
from shared.accounting.economic_ledger import INCOME_TYPES

BEIJING = timezone(timedelta(hours=8))


def period_windows(kind, count, now):
    """Calendar-aligned, half-open periods; the last ends at one captured now."""
    current = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == 'weekly': current -= timedelta(days=current.weekday())
    elif kind == 'monthly': current = current.replace(day=1)
    elif kind == 'yearly': current = current.replace(month=1, day=1)
    bounds = [current]
    for _ in range(count - 1):
        previous = bounds[0]
        if kind == 'yearly': previous = previous.replace(year=previous.year - 1)
        elif kind == 'monthly': previous = (previous - timedelta(days=1)).replace(day=1)
        else: previous -= timedelta(days=7 if kind == 'weekly' else 1)
        bounds.insert(0, previous)
    return list(zip(bounds, bounds[1:] + [now]))


def _ms(value):
    if value.tzinfo is None: value = value.replace(tzinfo=BEIJING)
    return int(value.timestamp() * 1000)


class ExchangeLedgerDashboardMixin:
    async def _ledger_report(self, start, end, *, report_type='daily', current=True):
        await self._ensure_initialized()
        account = os.environ.get('TRADE_ACCOUNT_ID', 'default')
        # A stable window key lets overview, cards and details share the active run.
        window_key = f'{report_type}:{_ms(start)}'
        return await read_published_report(
            self._db_manager, account, _ms(start), _ms(end), window_key,
            current=current, refresh_seconds=int(os.environ.get('LEDGER_REFRESH_SECONDS', '60')))

    @staticmethod
    def _ledger_metadata(report):
        return {key: report.get(key) for key in (
            'report_id', 'report_version', 'account_id', 'start_ms', 'end_ms',
            'requested_end_ms', 'as_of', 'stale_seconds', 'account_coverage_complete',
            'strategy_attribution_complete', 'coverage_gaps', 'other_asset_totals',
            'publication_status', 'refresh_error')}

    def _strategy_projection(self, report, strategy_id):
        complete = report['strategy_attribution_complete']
        bucket = report['by_strategy'].get(strategy_id, {})
        def amount(field): return bucket.get(field, '0') if complete else None
        return dict(id=strategy_id, name=self._STRATEGY_NAME_MAP.get(strategy_id, strategy_id),
                    emoji=self._STRATEGY_EMOJI_MAP.get(strategy_id, ''),
                    total_pnl=amount('net_usdt'), gross_pnl=amount('realized_pnl_usdt'),
                    commission=amount('commission_usdt'), funding=amount('funding_usdt'),
                    observed_pnl=bucket.get('net_usdt'), order_count=None, fill_count=None,
                    closed_count=None, win_count=None, loss_count=None, win_rate=None,
                    data_quality='complete' if complete else 'attribution_incomplete',
                    **self._ledger_metadata(report))

    async def get_overview(self, report_type='daily'):
        start, end = self._get_date_range(report_type)
        report = await self._ledger_report(start, end, report_type=report_type)
        components = report['account_components']
        complete = report['account_coverage_complete']
        positions = await self.get_open_positions_summary()
        strategy_ids = list(self._STRATEGY_NAME_MAP) + sorted(set(report['by_strategy']) - set(self._STRATEGY_NAME_MAP))
        strategies = [self._strategy_projection(report, sid) for sid in strategy_ids]
        for strategy in strategies:
            position = positions.get(strategy['id'], {})
            strategy.update(open_position_count=position.get('open_position_count', 0),
                            open_margin=str(position.get('open_margin', 0)))
        return dict(total_pnl=components['net_usdt'] if complete else None,
                    total_gross_pnl=components['realized_pnl_usdt'] if complete else None,
                    total_commission=components['commission_usdt'] if complete else None,
                    total_funding=components['funding_usdt'] if complete else None,
                    total_unrealized_pnl=str(await self._query_unrealized_pnl()),
                    observed_pnl=report['account_usdt_total'],
                    unattributed_pnl=report['unattributed_usdt_total'],
                    unattributed_count=report.get('unattributed_count'),
                    unattributed_absolute=report.get('unattributed_usdt_absolute'),
                    total_orders=None, total_closed=None, total_wins=None, win_rate=None,
                    strategies=strategies, report_type=report_type,
                    updated_at=datetime.now(BEIJING).isoformat(), **self._ledger_metadata(report))

    def _symbol_projections(self, report, strategy_id):
        buckets = {}
        for event in report['events']:
            if event['asset'] != 'USDT' or event['strategy'] != strategy_id: continue
            bucket = buckets.setdefault(event['symbol'], {kind: Decimal(0) for kind in INCOME_TYPES})
            bucket[event['income_type']] += Decimal(event['amount'])
        complete = report['strategy_attribution_complete']
        return [dict(symbol=symbol, total_pnl=str(sum(amounts.values())) if complete else None,
                     observed_pnl=str(sum(amounts.values())),
                     gross_pnl=str(amounts['REALIZED_PNL']) if complete else None,
                     commission=str(amounts['COMMISSION']) if complete else None,
                     funding=str(amounts['FUNDING_FEE']) if complete else None,
                     order_count=None, fill_count=None, wins=None, losses=None,
                     closed_count=None, win_rate=None,
                     data_quality='complete' if complete else 'attribution_incomplete',
                     **self._ledger_metadata(report)) for symbol, amounts in sorted(buckets.items())]

    async def get_strategy_symbols(self, strategy_id, report_type='daily'):
        report = await self._ledger_report(*self._get_date_range(report_type), report_type=report_type)
        return self._symbol_projections(report, strategy_id)

    async def get_strategy_detail(self, strategy_id, report_type='daily'):
        report = await self._ledger_report(*self._get_date_range(report_type), report_type=report_type)
        if strategy_id not in self._STRATEGY_NAME_MAP and strategy_id not in report['by_strategy']: return None
        return dict(**self._strategy_projection(report, strategy_id),
                    symbols=self._symbol_projections(report, strategy_id),
                    daily_counts={}, avg_daily_orders=None, data_source='exchange_income',
                    updated_at=datetime.now(BEIJING).isoformat())

    async def get_trend_data(self, report_type='daily', days=7):
        now = datetime.now(BEIJING)
        results = []
        for start, end in period_windows(report_type, days, now):
            report = await self._ledger_report(start, end, report_type=report_type, current=end == now)
            results.append(dict(date=start.strftime('%Y-%m-%d'),
                                total_pnl=report['account_usdt_total'] if report['account_coverage_complete'] else None,
                                observed_pnl=report['account_usdt_total'],
                                order_count=None, win_rate=None, **self._ledger_metadata(report)))
        return results

    async def compute_and_store_metrics(self):
        # Legacy mutable snapshots have no ledger version or coverage proof.
        return {'granularities': 0, 'rows': 0, 'source': 'exchange_income', 'legacy_snapshot_disabled': True}

    async def get_overview_from_metric(self, report_type='daily'): return None
    async def get_trend_from_metric(self, report_type, days): return None

    async def _attach_performance_metrics(self, overview_data, granularity):
        # Existing snapshots use a different economic basis and cannot be joined.
        for key in ('total_sharpe', 'total_max_drawdown', 'total_perf_sample_count',
                    'total_perf_window_start', 'total_perf_window_end'):
            overview_data[key] = None
        for strategy in overview_data.get('strategies', []):
            strategy.update(sharpe=None, max_drawdown=None, perf_sample_count=None)
