"""Immutable exchange report publication with an atomically activated pointer.

Facts and coverage are read in one repeatable-read transaction. A failed or
incomplete refresh retains the last verified report, explicitly marked stale.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from .income_integrity import income_account_lock, require_income_integrity
from .economic_ledger import INCOME_TYPES
from .report_reader import read_monthly_economic_report, _CURSOR_SQL

REPORT_VERSION = 'exchange-income-v1'
_ACTIVE_SQL = '''SELECT r.report, r.created_at FROM trading.ledger_report_active a
JOIN trading.ledger_report_run r ON r.report_id = a.report_id
WHERE a.account_id = $1 AND a.asset = 'USDT' AND a.window_key = $2
AND a.report_version = $3'''
_RUN_SQL = '''INSERT INTO trading.ledger_report_run
(report_id, account_id, asset, window_key, report_version, start_ms, end_ms, report)
VALUES ($1,$2,'USDT',$3,$4,$5,$6,$7::jsonb) ON CONFLICT (report_id) DO NOTHING'''
_ACTIVATE_SQL = '''INSERT INTO trading.ledger_report_active
(account_id, asset, window_key, report_version, report_id, end_ms)
VALUES ($1,'USDT',$2,$3,$4,$5)
ON CONFLICT (account_id, asset, window_key, report_version)
DO UPDATE SET report_id=EXCLUDED.report_id, end_ms=EXCLUDED.end_ms
WHERE trading.ledger_report_active.end_ms <= EXCLUDED.end_ms'''


def coverage_cutoff(rows, account_id, start, end):
    cutoffs = []
    for kind in INCOME_TYPES:
        cursor = start
        intervals = sorted((r['window_start_ms'], r['covered_until_ms']) for r in rows
                           if r['account_id'] == account_id and r['source'] == 'um/income'
                           and r['income_type'] == kind and r.get('symbol') is None
                           and r['page_complete'] is True and r['request_status'] == 'ok'
                           and r.get('covered_until_ms') is not None)
        for left, right in intervals:
            if left <= cursor: cursor = max(cursor, right)
        cutoffs.append(min(cursor, end))
    return min(cutoffs)


class _SnapshotDB:
    def __init__(self, connection): self.connection = connection
    async def fetch_all(self, query, *args): return await self.connection.fetch(query, *args)


def _served(report, requested_end, status, error=None):
    result = deepcopy(report)
    # Delivery metadata is outside the immutable report body identified by report_id.
    result.update(publication_status=status, requested_end_ms=requested_end,
                  stale_seconds=max(0, (requested_end-report['end_ms'])/1000)
                  if report.get('as_of') else None, refresh_error=error)
    return result


async def read_published_report(db, account_id, start_ms, end_ms, window_key,
                                *, current=False, refresh_seconds=60, _retry=0):
    """Read or refresh a durable verified run for a fixed calendar window key.

    ``current`` explicitly permits shortening an open period to its contiguous
    collection watermark. Historical periods require coverage to their full end.
    DB errors never fall back to trade_records or legacy metric snapshots.
    """
    if start_ms >= end_ms: raise ValueError('Invalid report window')
    previous = None
    try:
        async with db.pool.acquire() as connection:
            async with income_account_lock(connection, account_id):
                async with connection.transaction(isolation='repeatable_read'):
                    prior = await connection.fetchrow(_ACTIVE_SQL, account_id, window_key, REPORT_VERSION)
                    if prior:
                        previous = prior['report']
                        if isinstance(previous, str): previous = json.loads(previous)
                        # Reject corrupted/mismatched cached bodies rather than treating them as evidence.
                        if (previous.get('account_id') != account_id or previous.get('start_ms') != start_ms
                                or previous.get('report_version') != REPORT_VERSION
                                or not previous.get('account_coverage_complete')
                                or previous.get('end_ms', end_ms+1) > end_ms
                                or (not current and previous.get('end_ms') != end_ms)):
                            previous = None
                    # Check the durable latch even for a newly cached verified run.
                    await require_income_integrity(connection, account_id)
                    if previous and (datetime.now(timezone.utc)-prior['created_at']).total_seconds() < refresh_seconds:
                        return _served(previous, end_ms, 'published')
                    adapter = _SnapshotDB(connection)
                    cursors = await adapter.fetch_all(_CURSOR_SQL, account_id, start_ms, end_ms)
                    cutoff = coverage_cutoff(cursors, account_id, start_ms, end_ms)
                    effective_end = cutoff if current and cutoff > start_ms else end_ms
                    report = await read_monthly_economic_report(adapter, account_id, start_ms, effective_end,
                                                               include_reviewed_overrides=True)
                    report.update(window_key=window_key, requested_end_ms=end_ms,
                                  as_of=datetime.fromtimestamp(effective_end/1000,timezone.utc).isoformat()
                                  if report['account_coverage_complete'] else None)
                    report.pop('report_id', None)
                    report['report_id'] = hashlib.sha256(json.dumps(report,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    if not report['account_coverage_complete']:
                        if previous: return _served(previous, end_ms, 'stale', 'income_coverage_incomplete')
                        return _served(report, end_ms, 'unverified', 'income_coverage_incomplete')
                    await connection.execute(_RUN_SQL, report['report_id'], account_id, window_key,
                                             REPORT_VERSION, start_ms, effective_end,
                                             json.dumps(report,sort_keys=True,separators=(',',':')))
                    activation = await connection.execute(_ACTIVATE_SQL, account_id, window_key,
                                                           REPORT_VERSION, report['report_id'], effective_end)
                    if activation == 'INSERT 0 0':
                        # A newer watermark already won. Never serve the losing older run.
                        winner = await connection.fetchrow(_ACTIVE_SQL, account_id, window_key, REPORT_VERSION)
                        body = winner['report']
                        if isinstance(body,str): body=json.loads(body)
                        return _served(body,end_ms,'published')
                    return _served(report, end_ms, 'published')
    except Exception as exc:
        if getattr(exc, 'sqlstate', None) == '40001' and _retry < 2:
            return await read_published_report(db, account_id, start_ms, end_ms, window_key,
                                               current=current, refresh_seconds=refresh_seconds,
                                               _retry=_retry+1)
        if previous:
            return _served(previous, end_ms, 'stale', type(exc).__name__)
        raise
