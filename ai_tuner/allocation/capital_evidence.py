"""Audited, duration-weighted allocated capital for the default PM account.

Only history observations establish coverage. Legacy ``created_at`` values never
backdate it. All intervals are half open; month activation uses Asia/Shanghai.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


_BEIJING = ZoneInfo('Asia/Shanghai')
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _observed_us(value: Any) -> int:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('History requires an explicit UTC offset')
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _array(value: Any) -> list:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError('Expected JSON array')
    return value


def _month_boundaries(start_us: int, end_us: int) -> list:
    local = datetime.fromtimestamp(start_us / 1_000_000, _BEIJING)
    result = []
    year, month = local.year, local.month
    while True:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        boundary = _observed_us(datetime(year, month, 1, tzinfo=_BEIJING))
        if boundary >= end_us:
            return result
        result.append(boundary)


def _snapshot_capital(rows: list, at_us: int, strategy_ids: Iterable[str]) -> tuple:
    """Return the latest eligible active month's amounts and per-strategy gaps."""
    month = datetime.fromtimestamp(at_us / 1_000_000, _BEIJING).strftime('%Y-%m')
    active = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError('Invalid allocation row')
        if row.get('status') != 'active':
            continue
        raw_month = row.get('month')
        if not isinstance(raw_month, str) or len(raw_month) != 7:
            raise ValueError('Invalid allocation month')
        if datetime.strptime(raw_month, '%Y-%m').strftime('%Y-%m') != raw_month:
            raise ValueError('Invalid allocation month')
        if raw_month in active:
            return {}, {sid: 'duplicate_active_month' for sid in strategy_ids}
        active[raw_month] = row
    eligible = [key for key in active if key <= month]
    if not eligible:
        return {}, {sid: 'no_active_allocation' for sid in strategy_ids}
    entries = _array(active[max(eligible)].get('entries'))
    amounts, gaps = {}, {}
    for sid in strategy_ids:
        matches = [entry for entry in entries if isinstance(entry, Mapping) and entry.get('strategy_id') == sid]
        if not matches:
            gaps[sid] = 'missing_strategy'
            continue
        if len(matches) != 1:
            gaps[sid] = 'duplicate_strategy_allocation'
            continue
        raw = matches[0].get('allocated_amount')
        try:
            if raw is None or isinstance(raw, bool):
                raise ValueError('Missing/boolean amount')
            value = Decimal(str(raw))
            if not value.is_finite() or value <= 0:
                raise ValueError('Nonpositive/nonfinite amount')
            amounts[sid] = value
        except (ValueError, TypeError, InvalidOperation):
            gaps[sid] = 'invalid_allocated_amount'
    return amounts, gaps


def project_capital_evidence(
    history: Iterable[Mapping[str, Any]], account_id: str, start_ms: int,
    end_ms: int, strategy_ids: Iterable[str], *, audit_healthy: bool = True,
) -> dict:
    """Project immutable full-table snapshots into per-strategy capital evidence.

    Unknown capital is ``None``, never zero. A single uncovered positive-duration
    slice invalidates that strategy's complete report window. Returns Decimal
    amounts, which callers can serialize as strings without precision loss.
    """
    strategy_ids = tuple(dict.fromkeys(strategy_ids))
    reasons = {sid: [] for sid in strategy_ids}
    totals = {sid: Decimal(0) for sid in strategy_ids}

    def reject(reason, ids=strategy_ids):
        for sid in ids:
            if reason not in reasons[sid]:
                reasons[sid].append(reason)

    events = []
    valid_window = (isinstance(start_ms, int) and not isinstance(start_ms, bool)
                    and isinstance(end_ms, int) and not isinstance(end_ms, bool)
                    and 0 <= start_ms < end_ms)
    if not valid_window:
        reject('invalid_capital_window')
    if account_id != 'default':
        reject('unsupported_account')
    if not audit_healthy:
        reject('capital_audit_unhealthy')
    if valid_window and account_id == 'default' and audit_healthy:
        try:
            seen = set()
            for item in history:
                if not isinstance(item, Mapping):
                    raise ValueError('Invalid history event')
                if item.get('account_id') != account_id:
                    reject('capital_account_mismatch')
                    continue
                event_id = item['id']
                if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0 or event_id in seen:
                    raise ValueError('Invalid/duplicate event id')
                seen.add(event_id)
                observed = _observed_us(item['observed_at'])
                if item.get('operation') not in ('BASELINE', 'INSERT', 'UPDATE', 'DELETE'):
                    raise ValueError('Invalid history operation')
                if observed < end_ms * 1000:
                    events.append((observed, event_id, _array(item['allocations'])))
            events.sort(key=lambda item: (item[0], item[1]))
            before = [entry for entry in events if entry[0] <= start_ms * 1000]
            after = [entry for entry in events if entry[0] > start_ms * 1000]
            events = (before[-1:] if before else []) + after
            if not before:
                reject('missing_start_coverage')
            if before and not any(reasons.values()):
                points = sorted({start_ms * 1000, end_ms * 1000,
                                 *[entry[0] for entry in after],
                                 *_month_boundaries(start_ms * 1000, end_ms * 1000)})
                cursor = 0
                for left, right in zip(points, points[1:]):
                    while cursor + 1 < len(events) and events[cursor + 1][0] <= left:
                        cursor += 1
                    amounts, gaps = _snapshot_capital(events[cursor][2], left, strategy_ids)
                    for sid, reason in gaps.items():
                        reject(reason, (sid,))
                    for sid, amount in amounts.items():
                        totals[sid] += amount * (right - left)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            reject('invalid_history_event')
    payload = json.dumps([account_id, start_ms, end_ms, events], sort_keys=True,
                         separators=(',', ':'), default=str).encode()
    ids = ','.join(str(entry[1]) for entry in events) or 'none'
    source_id = f'capital-history:{ids}:sha256:{hashlib.sha256(payload).hexdigest()}'
    return {sid: dict(account_id=account_id, start_ms=start_ms, end_ms=end_ms,
                      average_capital_usdt=(totals[sid] / ((end_ms - start_ms) * 1000)
                                            if valid_window and not reasons[sid] else None),
                      source_id=source_id, coverage_complete=not reasons[sid], reasons=reasons[sid])
            for sid in strategy_ids}


# One statement gives the catalog guard and events the same PostgreSQL snapshot.
# Trigger functions run as the installing owner; application callers only SELECT.
_READ_HISTORY = """
WITH audit AS (
    SELECT count(*) = 4
           AND EXISTS (SELECT FROM trading.capital_allocation_history
                       WHERE account_id = $1 AND operation = 'BASELINE') AS audit_healthy
    FROM pg_catalog.pg_trigger AS t
    JOIN pg_catalog.pg_proc AS p ON p.oid = t.tgfoid
    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
    WHERE NOT t.tgisinternal AND t.tgenabled = 'A' AND n.nspname = 'trading'
      AND (t.tgrelid, t.tgname, t.tgtype::int, p.proname) IN (
        (to_regclass('public.capital_allocation'), 'capital_history_serialize', 30, 'serialize_capital_allocation'),
        (to_regclass('public.capital_allocation'), 'capital_history_capture', 28, 'capture_capital_allocation'),
        (to_regclass('public.capital_allocation'), 'capital_history_no_truncate', 34, 'reject_capital_history_mutation'),
        (to_regclass('trading.capital_allocation_history'), 'capital_history_immutable', 58, 'reject_capital_history_mutation')
      )
), relevant AS (
    SELECT id, account_id, observed_at, operation, allocations
    FROM trading.capital_allocation_history
    WHERE account_id = $1
      AND observed_at < to_timestamp($3::double precision / 1000)
      AND (observed_at > to_timestamp($2::double precision / 1000)
           OR id = (SELECT id FROM trading.capital_allocation_history
                    WHERE account_id = $1
                      AND observed_at <= to_timestamp($2::double precision / 1000)
                    ORDER BY observed_at DESC, id DESC LIMIT 1))
)
SELECT audit.audit_healthy, relevant.* FROM audit LEFT JOIN relevant ON true
ORDER BY relevant.observed_at, relevant.id
"""


async def read_capital_evidence(db, account_id, start_ms, end_ms, strategy_ids):
    """Read audited history through a ``fetch_all`` database wrapper, fail closed."""
    strategy_ids = tuple(strategy_ids)
    if account_id != 'default':
        return project_capital_evidence([], account_id, start_ms, end_ms, strategy_ids)
    try:
        rows = await db.fetch_all(_READ_HISTORY, account_id, start_ms, end_ms)
        healthy = bool(rows) and all(row.get('audit_healthy') is True for row in rows)
        events = [row for row in rows if row.get('id') is not None]
        return project_capital_evidence(events, account_id, start_ms, end_ms,
                                        strategy_ids, audit_healthy=healthy)
    except Exception:
        result = project_capital_evidence([], account_id, start_ms, end_ms, strategy_ids)
        for evidence in result.values():
            evidence['reasons'] = ['capital_history_unavailable']
        return result
