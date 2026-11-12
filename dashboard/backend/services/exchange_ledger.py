"""Dashboard projections of the canonical exchange-income ledger.

No inferred symbol ownership, lifecycle win rate or legacy PNL fallback.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os

from shared.accounting.published_report import read_published_report
from shared.accounting.economic_ledger import INCOME_TYPES
from shared.accounting.dashboard_performance import (
    realized_income_metrics, unavailable_lifecycle_stats, unavailable_realized_metrics,
    verified_lifecycle_stats,
)

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
    async def _verified_lifecycles(self, report, strategy_ids):
        if report.get('publication_status') != 'published' or not report.get('strategy_attribution_complete'):
            return verified_lifecycle_stats(report, [], [], [], [], set(), strategy_ids)
        account = report.get('account_id', 'default')
        start_ms, end_ms = report['start_ms'], report['end_ms']
        start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
        end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
        try:
            baselines = [dict(row) for row in await self._db_manager.fetch_all(
                'SELECT account_id,symbol,flat_at,exchange_qty '
                'FROM trading.position_flat_baseline WHERE account_id=$1 '
                'AND flat_at >= $2 AND flat_at < $3 ORDER BY symbol,flat_at',
                account, start, end)]
            symbols = sorted({row['symbol'] for row in baselines})
            if not symbols:
                return verified_lifecycle_stats(report, [], [], [], [], set(), strategy_ids)
            fills = [dict(row) for row in await self._db_manager.fetch_all(
                'SELECT account_id,symbol,trade_id,order_id,side,qty,commission,commission_asset,'
                'realized_pnl,trade_time_utc FROM trading.execution_fact_fill '
                'WHERE account_id=$1 AND symbol=ANY($2::text[]) '
                'AND trade_time_utc > $3 AND trade_time_utc < $4',
                account, symbols, min(row['flat_at'] for row in baselines), end)]
            order_ids = sorted({int(row['order_id']) for row in fills if row.get('order_id') is not None})
            owners = [dict(row) for row in await self._db_manager.fetch_all(
                'SELECT account_id,symbol,order_id,strategy FROM trading.order_strategy_evidence '
                'WHERE account_id=$1 AND symbol=ANY($2::text[]) AND order_id=ANY($3::bigint[])',
                account, symbols, order_ids)]
            trade_ids = sorted({int(row['trade_id']) for row in fills})
            incomes = [dict(row) for row in await self._db_manager.fetch_all(
                'SELECT symbol,trade_id,tran_id,income_type,income FROM trading.execution_fact_income '
                'WHERE account_id=$1 AND symbol=ANY($2::text[]) '
                'AND trade_id=ANY($3::bigint[]) AND income_type=ANY($4::text[])',
                account, symbols, trade_ids, ['REALIZED_PNL', 'COMMISSION'])]
            cursors = [dict(row) for row in await self._db_manager.fetch_all(
                'SELECT source,account_id,income_type,symbol,window_start_ms,covered_until_ms,'
                'page_complete,request_status FROM trading.execution_fact_cursor '
                'WHERE account_id=$1 AND window_start_ms < $3 AND covered_until_ms >= $2',
                account, start_ms, end_ms)]
            covered = set()
            for symbol in symbols:
                intervals = sorted((int(row['window_start_ms']), int(row['covered_until_ms']))
                                   for row in cursors if row.get('source') == 'userTrades'
                                   and row.get('symbol') == symbol and row.get('page_complete') is True
                                   and row.get('request_status') == 'ok'
                                   and row.get('covered_until_ms') is not None)
                cursor = start_ms
                for left, right in intervals:
                    if left <= cursor:
                        cursor = max(cursor, right)
                if cursor >= end_ms:
                    covered.add(symbol)
            return verified_lifecycle_stats(report, baselines, fills, owners, incomes,
                                            covered, strategy_ids)
        except Exception:
            return unavailable_lifecycle_stats('lifecycle_source_unavailable', strategy_ids)

    async def _realized_performance(self, report, strategy_ids):
        """Attach only capital denominators to the immutable income publication."""
        start = datetime.fromtimestamp(report['start_ms'] / 1000, BEIJING).date()
        end = datetime.fromtimestamp(report['end_ms'] / 1000, BEIJING).date()
        account_capital = {}
        strategy_capital = {}
        if (report.get('publication_status') == 'published'
                and report.get('account_coverage_complete') and start < end):
            try:
                equity_rows = await self._db_manager.fetch_all(
                    'SELECT snapshot_date, total_equity FROM public.equity_snapshot '
                    'WHERE snapshot_date >= $1 AND snapshot_date < $2 ORDER BY snapshot_date',
                    start - timedelta(days=1), end - timedelta(days=1))
                account_capital = {row['snapshot_date'] + timedelta(days=1): row['total_equity']
                                   for row in equity_rows}
                if report.get('strategy_attribution_complete'):
                    strategy_rows = await self._db_manager.fetch_all(
                        'SELECT strategy_id, snapshot_hour, allocated_amount '
                        'FROM public.strategy_position_snapshot_history '
                        'WHERE snapshot_hour >= $1 AND snapshot_hour < $2 '
                        'AND EXTRACT(HOUR FROM snapshot_hour) = 0 '
                        'AND EXTRACT(MINUTE FROM snapshot_hour) = 0 '
                        'ORDER BY strategy_id, snapshot_hour',
                        datetime.combine(start, datetime.min.time()),
                        datetime.combine(end, datetime.min.time()))
                    for row in strategy_rows:
                        strategy_capital.setdefault(row['strategy_id'], {})[
                            row['snapshot_hour'].date()] = row['allocated_amount']
            except Exception:
                return unavailable_realized_metrics('capital_source_unavailable', report, strategy_ids)
        return realized_income_metrics(report, account_capital, strategy_capital, strategy_ids)

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
        performance = await self._realized_performance(report, strategy_ids)
        lifecycles = await self._verified_lifecycles(report, strategy_ids)
        for strategy in strategies:
            position = positions.get(strategy['id'], {})
            strategy.update(open_position_count=position.get('open_position_count', 0),
                            open_margin=str(position.get('open_margin', 0)))
            metric = performance['strategies'][strategy['id']]
            strategy.update(sharpe=metric['sharpe'], max_drawdown=metric['max_drawdown'],
                            perf_sample_count=metric['sample_count'],
                            perf_window_start=metric['window_start'], perf_window_end=metric['window_end'],
                            perf_reason=metric['reason'], perf_caliber=metric['caliber'])
            lifecycle = lifecycles['strategies'][strategy['id']]
            strategy.update(order_count=lifecycle['order_count'], fill_count=lifecycle['fill_count'],
                            closed_count=lifecycle['closed_count'], win_count=lifecycle['win_count'],
                            loss_count=lifecycle['loss_count'], win_rate=lifecycle['win_rate'],
                            win_sample_start=lifecycle['sample_start'],
                            win_sample_partial=lifecycle['sample_partial'],
                            win_rate_reason=lifecycle['reason'], win_rate_caliber=lifecycle['caliber'])
        account_metric = performance['account']
        account_lifecycle = lifecycles['account']
        return dict(total_pnl=components['net_usdt'] if complete else None,
                    total_gross_pnl=components['realized_pnl_usdt'] if complete else None,
                    total_commission=components['commission_usdt'] if complete else None,
                    total_funding=components['funding_usdt'] if complete else None,
                    total_unrealized_pnl=str(await self._query_unrealized_pnl()),
                    observed_pnl=report['account_usdt_total'],
                    unattributed_pnl=report['unattributed_usdt_total'],
                    unattributed_count=report.get('unattributed_count'),
                    unattributed_absolute=report.get('unattributed_usdt_absolute'),
                    total_orders=account_lifecycle['order_count'], total_closed=account_lifecycle['closed_count'],
                    total_wins=account_lifecycle['win_count'], win_rate=account_lifecycle['win_rate'],
                    win_sample_start=account_lifecycle['sample_start'],
                    win_sample_partial=account_lifecycle['sample_partial'],
                    win_rate_reason=account_lifecycle['reason'],
                    win_rate_caliber=account_lifecycle['caliber'],
                    total_sharpe=account_metric['sharpe'], total_max_drawdown=account_metric['max_drawdown'],
                    total_perf_sample_count=account_metric['sample_count'],
                    total_perf_window_start=account_metric['window_start'],
                    total_perf_window_end=account_metric['window_end'],
                    total_perf_reason=account_metric['reason'], total_perf_caliber=account_metric['caliber'],
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
        detail = self._strategy_projection(report, strategy_id)
        metric = (await self._realized_performance(report, [strategy_id]))['strategies'][strategy_id]
        lifecycle = (await self._verified_lifecycles(report, [strategy_id]))['strategies'][strategy_id]
        detail.update(sharpe=metric['sharpe'], max_drawdown=metric['max_drawdown'],
                      perf_sample_count=metric['sample_count'],
                      perf_window_start=metric['window_start'], perf_window_end=metric['window_end'],
                      perf_reason=metric['reason'], perf_caliber=metric['caliber'])
        detail.update(order_count=lifecycle['order_count'], fill_count=lifecycle['fill_count'],
                      closed_count=lifecycle['closed_count'], win_count=lifecycle['win_count'],
                      loss_count=lifecycle['loss_count'], win_rate=lifecycle['win_rate'],
                      win_sample_start=lifecycle['sample_start'],
                      win_sample_partial=lifecycle['sample_partial'],
                      win_rate_reason=lifecycle['reason'], win_rate_caliber=lifecycle['caliber'])
        return dict(**detail,
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
        # get_overview uses the published exchange-income report. Legacy mutable
        # performance_metric_snapshot rows must never overwrite that caliber.
        return None
