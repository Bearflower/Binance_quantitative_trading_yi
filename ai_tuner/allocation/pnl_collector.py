"""Allocation inputs share the immutable published exchange-income ledger.

A complete net-income report alone cannot prove historical strategy capital or
owned-fill activity. Until those evidence contracts exist, collection is explicitly
ineligible. No local trade-record PnL, future snapshot, or open-margin fallback is
allowed to fabricate rankable returns.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List

from shared.accounting.published_report import REPORT_VERSION, read_published_report
from shared.entry_risk_policy import account_id

_CST = timezone(timedelta(hours=8))


class AllocationInputUnavailable(RuntimeError):
    """Diagnostics only; this exception never represents rankable allocation data."""

    def __init__(self, *reasons, report_id=None, verified_net_pnl=None):
        self.reasons = tuple(reasons)
        self.report_id = report_id
        self.verified_net_pnl = verified_net_pnl
        super().__init__(', '.join(self.reasons))


class PnLCollector:
    def __init__(self, db_manager, strategies: List[Dict[str, Any]],
                 binance_client=None, strict_hrs_income=False):
        # Retain the public constructor; legacy arguments never select another source.
        self.db_manager = db_manager
        self.strategies = strategies

    async def collect_all_realized_pnl(self, month_start: datetime,
                                       month_end: datetime) -> Dict[str, Dict[str, Any]]:
        start_ms, end_ms = self._completed_month_window(month_start, month_end)
        account = account_id()
        window_key = f'monthly:{start_ms}'
        try:
            report = await read_published_report(self.db_manager, account, start_ms,
                                                 end_ms, window_key, current=False)
        except Exception as exc:
            raise AllocationInputUnavailable('published_report_unavailable') from exc
        if not isinstance(report, dict):
            raise AllocationInputUnavailable('invalid_report')
        expected = dict(account_id=account, start_ms=start_ms, end_ms=end_ms,
                        requested_end_ms=end_ms, window_key=window_key,
                        report_version=REPORT_VERSION)
        if any(report.get(key) != value for key, value in expected.items()):
            raise AllocationInputUnavailable('report_identity_mismatch')
        report_id = report.get('report_id')
        if not isinstance(report_id, str) or not report_id.strip():
            raise AllocationInputUnavailable('missing_report_id')
        if (report.get('publication_status') != 'published'
                or report.get('refresh_error') is not None
                or report.get('account_coverage_complete') is not True
                or report.get('strategy_attribution_complete') is not True):
            raise AllocationInputUnavailable('report_not_verified', report_id=report_id)
        nets = {}
        try:
            for strategy in self.strategies:
                strategy_id = strategy['strategy_id']
                if not isinstance(strategy_id, str) or not strategy_id or strategy_id in nets:
                    raise ValueError('missing or duplicate strategy id')
                net = Decimal(str(report['by_strategy'][strategy_id]['net_usdt']))
                if not net.is_finite():
                    raise ValueError('non-finite strategy net')
                nets[strategy_id] = net
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise AllocationInputUnavailable('strategy_net_unavailable', report_id=report_id) from exc
        # Current mutable allocations and hourly snapshots cannot prove historical
        # capital constancy. Income rows also do not prove owned fill activity.
        # Never turn either missing proof into 0%, zero trades, or a partial ranking.
        raise AllocationInputUnavailable('missing_capital_evidence', 'missing_activity_evidence',
                                         report_id=report_id, verified_net_pnl=nets)

    @staticmethod
    def _completed_month_window(month_start, month_end):
        if (not isinstance(month_start, datetime) or not isinstance(month_end, datetime)
                or month_start.utcoffset() is None or month_end.utcoffset() is None):
            raise AllocationInputUnavailable('invalid_month_window')
        left, right = month_start.astimezone(_CST), month_end.astimezone(_CST)
        next_month = (left.replace(day=28) + timedelta(days=4)).replace(day=1)
        if (left.day != 1 or any((left.hour, left.minute, left.second, left.microsecond))
                or right != next_month or right > datetime.now(_CST)):
            raise AllocationInputUnavailable('invalid_month_window')
        return int(left.timestamp()*1000), int(right.timestamp()*1000)
