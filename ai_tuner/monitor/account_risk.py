"""Read-only PM account risk observation; no order or policy mutation capability.

The durable account baseline is independent of calendar jobs and budget renewal.
Returns use actualEquity (USD economic equity without collateral haircuts),
preserving accountEquity separately. They are exact *sampled* unit NAV returns.
Missing event-time cashflow equity
anchors, missing coverage or missing samples never become zero return.

Budget: account_id, account_identity (operator-configured stable binding, not an
exchange-attested UID), budget_id, max_drawdown_ratio, decision_at_ms; optional
max_sample_gap_ms (300000), max_snapshot_age_ms (120000). Both clients must be
ReadOnlyBinanceClient instances using the same credentials. A credential change
requires an explicit identity migration; this observer cannot rebase an account.
After initialization, pass expected_baseline_time_ms from an independently
durable, create-only baseline pin to detect loss of *all* PostgreSQL facts.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Mapping

from shared.binance_account_mode import verify_trading_mode
from shared.readonly_client import ReadOnlyBinanceClient


TRANSFER_TYPES = ('MAIN_PORTFOLIO_MARGIN', 'PORTFOLIO_MARGIN_MAIN')
EQUITY_SOURCE = 'signed:GET /papi/v1/account'
TRANSFER_SOURCE = 'signed:GET /sapi/v1/asset/transfer'


def _number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _ms(value: Any) -> bool:
    return type(value) is int and value > 0


def _budget_gaps(budget: Mapping) -> list[str]:
    gaps = []
    for key in ('account_id', 'account_identity', 'budget_id'):
        if not isinstance(budget.get(key), str) or not budget[key].strip():
            gaps.append(f'budget_{key}_missing')
    limit = _number(budget.get('max_drawdown_ratio'))
    if limit is None or not 0 < limit < 1:
        gaps.append('budget_limit_invalid')
    if not _ms(budget.get('decision_at_ms')):
        gaps.append('budget_decision_at_invalid')
    for key, default in (('max_sample_gap_ms', 300_000), ('max_snapshot_age_ms', 120_000)):
        if not _ms(budget.get(key, default)):
            gaps.append(f'budget_{key}_invalid')
    if 'expected_baseline_time_ms' in budget and not _ms(budget['expected_baseline_time_ms']):
        gaps.append('budget_expected_baseline_time_ms_invalid')
    return gaps


def assess_account_risk(evidence: Mapping, budget: Mapping) -> dict:
    """Assess immutable facts without IO. Gaps suppress all drawdown numbers.

    Cashflows require identity, complete transfer history, event-time pre/post
    equity and a valuation identity. An account with such an unresolved event
    stays BLOCKED_DATA on every later run, until the actual facts are supplied.
    """
    gaps = list(evidence.get('source_gaps') or []) + _budget_gaps(budget)
    account, identity = budget.get('account_id'), budget.get('account_identity')
    limit = _number(budget.get('max_drawdown_ratio'))
    baseline = evidence.get('baseline')
    result = {'status': 'BLOCKED_DATA', 'account_id': account,
              'budget_id': budget.get('budget_id'),
              'limit_ratio': str(limit) if limit is not None else None,
              'current_drawdown_ratio': None, 'max_drawdown_ratio': None,
              'current_equity_usd': None,
              'unit_nav': None, 'high_water_unit_nav': None,
              'as_of_ms': evidence.get('as_of_ms'), 'baseline': baseline,
              'activation_gap': None, 'method': 'cashflow_adjusted_sampled_unit_nav',
              'known_breach': None, 'confirmed_drawdown_lower_bound_ratio': None,
              'source_gaps': [], 'reasons': []}
    if _budget_gaps(budget):
        result['source_gaps'] = result['reasons'] = sorted(set(gaps))
        return result

    def finish_gaps():
        result['source_gaps'] = sorted(set(gaps))
        result['reasons'] = list(result['source_gaps'])
        for breach in evidence.get('confirmed_breaches', []):
            if not isinstance(breach, Mapping):
                continue
            maximum = _number(breach.get('max_drawdown_ratio'))
            if (breach.get('account_id') == account and breach.get('account_identity') == identity
                    and breach.get('source') == 'persisted:account_risk_verified_breach'
                    and maximum is not None and maximum >= limit):
                prior = _number(result['confirmed_drawdown_lower_bound_ratio'])
                if prior is None or maximum > prior:
                    result.update(status='SAFETY_REVIEW', known_breach=dict(breach),
                                  confirmed_drawdown_lower_bound_ratio=str(maximum))
        if result['known_breach']:
            result['reasons'].append('previously_verified_account_drawdown_breach')
        return result

    def bound(row):
        return row.get('account_id') == account and row.get('account_identity') == identity

    if not bound(evidence):
        gaps.append('account_identity_mismatch')
    if not isinstance(baseline, Mapping):
        gaps.append('baseline_missing')
        baseline = {}
    elif not bound(baseline):
        gaps.append('account_identity_mismatch:baseline')
    start = baseline.get('exchange_time_ms')
    if 'expected_baseline_time_ms' in budget:
        if not start:
            gaps.append('baseline_pin_missing')
        elif start != budget['expected_baseline_time_ms']:
            gaps.append('baseline_pin_mismatch')
    end = evidence.get('as_of_ms')
    observed = evidence.get('observed_at_ms')
    points = evidence.get('equity_points')
    events = evidence.get('cashflows')
    coverage = evidence.get('coverage')
    if not isinstance(points, (list, tuple)) or not points:
        gaps.append('equity_points_missing')
        points = []
    if not isinstance(events, (list, tuple)):
        gaps.append('cashflow_facts_missing')
        events = []
    if not isinstance(coverage, (list, tuple)):
        gaps.append('cashflow_coverage_gap')
        coverage = []
    valid_period = _ms(start) and _ms(end) and end >= start
    if not valid_period:
        gaps.append('baseline_or_as_of_invalid')
    elif _ms(baseline.get('decision_at_ms', budget.get('decision_at_ms'))):
        activation = baseline.get('decision_at_ms', budget['decision_at_ms'])
        if start < activation:
            gaps.append('baseline_precedes_activation')
        result['activation_gap'] = {'start_ms': activation, 'end_ms': start,
                                    'reason': 'pre_activation_equity_unobserved'}
    if not _ms(observed) or not _ms(end):
        gaps.append('equity_snapshot_time_missing')
    elif observed < end:
        gaps.append('equity_snapshot_future')
    elif observed - end > budget.get('max_snapshot_age_ms', 120_000):
        gaps.append('equity_snapshot_stale')

    if any(not isinstance(point, Mapping) for point in points):
        gaps.append('equity_point_invalid')
        return finish_gaps()
    if _number(baseline.get('equity_usd')) is None or _number(baseline.get('equity_usd')) <= 0:
        gaps.append('baseline_equity_not_positive')
    previous = None
    for point in points:
        if not isinstance(point, Mapping):
            gaps.append('equity_point_invalid')
            continue
        if not bound(point):
            gaps.append('account_identity_mismatch:equity')
        stamp, amount = point.get('exchange_time_ms'), _number(point.get('equity_usd'))
        if point.get('source') != EQUITY_SOURCE or amount is None:
            gaps.append('equity_signed_anchor_unverified')
        if not _ms(stamp) or (previous is not None and stamp <= previous):
            gaps.append('equity_order_invalid')
        if _ms(stamp) and previous is not None:
            if stamp - previous > budget.get('max_sample_gap_ms', 300_000) or stamp < previous:
                gaps.append(f'equity_sampling_gap:{previous}:{stamp}')
        if _ms(stamp):
            previous = stamp
    if points and valid_period:
        if (points[0].get('exchange_time_ms') != start
                or _number(points[0].get('equity_usd')) != _number(baseline.get('equity_usd'))
                or baseline.get('source') != EQUITY_SOURCE):
            gaps.append('baseline_anchor_missing')
        if points[-1].get('exchange_time_ms') != end:
            gaps.append('current_equity_anchor_missing')

    seen = set()
    event_previous = None
    for event in events:
        if not isinstance(event, Mapping):
            gaps.append('cashflow_event_invalid')
            continue
        eid, typ = event.get('event_id'), event.get('transfer_type')
        key = (typ, str(eid))
        if not bound(event):
            gaps.append('account_identity_mismatch:cashflow')
        if eid is None or str(eid) == '' or key in seen or typ not in TRANSFER_TYPES:
            gaps.append('cashflow_identity_unverified')
        seen.add(key)
        stamp = event.get('exchange_time_ms')
        if (not _ms(stamp) or (valid_period and not start < stamp <= end)
                or (event_previous is not None and stamp <= event_previous)):
            gaps.append(f'cashflow_order_invalid:{eid}')
        if _ms(stamp):
            event_previous = stamp
            if any(p.get('request_start_ms', p.get('exchange_time_ms', 0)) <= stamp
                   <= p.get('request_end_ms', p.get('exchange_time_ms', 0))
                   for p in points if isinstance(p, Mapping)):
                gaps.append(f'cashflow_snapshot_time_ambiguous:{eid}')
        if event.get('status') != 'CONFIRMED' or event.get('source') != TRANSFER_SOURCE:
            gaps.append(f'cashflow_status_unverified:{eid}')
        amount = _number(event.get('amount'))
        value = _number(event.get('value_usd'))
        if event.get('asset') == 'USDT' and value is None:
            value = amount
        if (amount is None or amount <= 0 or value is None or value <= 0
                or not event.get('valuation_id')):
            gaps.append(f'cashflow_value_unverified:{eid}')
        pre, post = _number(event.get('pre_equity_usd')), _number(event.get('post_equity_usd'))
        if (pre is None or pre <= 0 or post is None or post <= 0
                or event.get('pre_valuation_time_ms') != stamp
                or event.get('post_valuation_time_ms') != stamp):
            gaps.append(f'cashflow_event_equity_unverified:{eid}')
        elif value is not None:
            signed = value if typ == TRANSFER_TYPES[0] else -value
            if post - pre != signed:
                gaps.append(f'cashflow_equity_bridge_mismatch:{eid}')

    if valid_period:
        for typ in TRANSFER_TYPES:
            cursor = start + 1
            windows = []
            for window in coverage:
                if not isinstance(window, Mapping) or window.get('transfer_type') != typ:
                    continue
                if not bound(window):
                    gaps.append('account_identity_mismatch:coverage')
                    continue
                a, b = window.get('start_ms'), window.get('end_ms')
                if (window.get('complete') is not True or not _ms(a) or not _ms(b) or b < a):
                    continue
                if b <= start:
                    # These may be raw initialization attempts before the first
                    # valid baseline. They remain audit facts, outside returns.
                    continue
                expected = {str(e['event_id']) for e in events if isinstance(e, Mapping)
                            and e.get('transfer_type') == typ and _ms(e.get('exchange_time_ms'))
                            and a <= e['exchange_time_ms'] <= b}
                ids = window.get('event_ids')
                if (not isinstance(ids, list) or len(ids) != len(set(ids))
                        or type(window.get('total')) is not int or window['total'] != len(ids)
                        or set(ids) != expected):
                    gaps.append(f'coverage_event_identity_mismatch:{typ}:{a}:{b}')
                    continue
                windows.append((a, b))
            for a, b in sorted(windows):
                if a <= cursor <= b:
                    cursor = b + 1
            if cursor <= end:
                gaps.append(f'cashflow_coverage_gap:{typ}:{cursor}:{end}')

    if gaps:
        return finish_gaps()

    # Exact rational arithmetic prevents an exact 5% threshold from becoming
    # 4.999999999999... after several rounded Decimal divisions.
    nav = high = Fraction(1)
    maximum = current = Fraction(0)
    units = Fraction(_number(baseline['equity_usd']))
    event_index = 0

    def mark(value):
        nonlocal nav, high, maximum, current
        nav = value
        high = max(high, nav)
        current = (high - nav) / high
        maximum = max(maximum, current)

    for point in points[1:]:
        while event_index < len(events) and events[event_index]['exchange_time_ms'] <= point['exchange_time_ms']:
            event = events[event_index]
            mark(Fraction(_number(event['pre_equity_usd'])) / units)
            units = Fraction(_number(event['post_equity_usd'])) / nav
            event_index += 1
        mark(Fraction(_number(point['equity_usd'])) / units)
    def display(value):
        return str(Decimal(value.numerator) / Decimal(value.denominator))
    reached = maximum >= Fraction(limit)
    result.update(status='SAFETY_REVIEW' if reached else 'VERIFIED',
                  current_drawdown_ratio=display(current), max_drawdown_ratio=display(maximum),
                  current_equity_usd=str(_number(points[-1]['equity_usd'])),
                  unit_nav=display(nav), high_water_unit_nav=display(high))
    # Attach durable historical breach evidence on complete assessments as well
    # as on incomplete ones. Cache consumers must see the very first confirmed
    # breach, before a subsequent API failure or restart happens.
    finish_gaps()
    known_maximum = _number(result['confirmed_drawdown_lower_bound_ratio'])
    if known_maximum is not None and maximum < Fraction(known_maximum):
        gaps.append('verified_breach_exceeds_reconstructed_history')
        result.update(current_drawdown_ratio=None, max_drawdown_ratio=None,
                      current_equity_usd=None, unit_nav=None, high_water_unit_nav=None)
        return finish_gaps()
    if reached:
        result['reasons'] = ['account_drawdown_limit_reached']
        if result['known_breach'] is None:
            # The pure function can prove a crossing, but cannot attest that it
            # has been committed. collect_account_risk replaces this with the
            # durable fact after the successful database transaction.
            result['known_breach'] = {
                'account_id': account, 'account_identity': identity,
                'source': 'assessed:account_risk_verified_breach',
                'baseline_time_ms': start, 'as_of_ms': end,
                'max_drawdown_ratio': display(maximum),
                'budget_id': budget['budget_id'],
            }
        result['confirmed_drawdown_lower_bound_ratio'] = display(maximum)
    return result


def classic_budget_block(account_id: str) -> dict:
    """经典预算须独立批准，固定切点和期初 USDT 权益，并验证划转覆盖与事件前后权益。"""
    reasons = ['classic_drawdown_budget_not_approved',
               'classic_usdt_baseline_and_cashflow_coverage_required']
    return {'status': 'BLOCKED_DATA', 'account_id': account_id, 'account_mode': 'classic',
            'budget_id': None, 'limit_ratio': None, 'baseline': None, 'as_of_ms': None,
            'current_equity_usd': None, 'current_drawdown_ratio': None,
            'max_drawdown_ratio': None, 'known_breach': None,
            'source_gaps': reasons, 'reasons': reasons}


async def observe_classic_equity(client: ReadOnlyBinanceClient) -> dict:
    """独立记录单资产钱包的签名快照；这是 USDT 计价采样，不含回撤预算。"""
    if (not isinstance(client, ReadOnlyBinanceClient) or client.use_unified_account
            or client.base_url.rstrip('/') != 'https://fapi.binance.com' or client.testnet):
        raise ValueError('invalid classic production observation route')
    await verify_trading_mode(client)
    account = await client.get_account_info()
    margin = account['totalMarginBalance']
    wallet = account['totalWalletBalance']
    pnl = account['totalUnrealizedProfit']
    if wallet + pnl != margin:
        raise ValueError('classic wallet and margin balance disagree')
    return {'status': 'VERIFIED_POINT', 'source': 'signed:GET /fapi/v3/account',
            'credential_fingerprint': hashlib.sha256(client._api_key.encode()).hexdigest(),
            'margin_balance_usdt': str(margin), 'wallet_balance_usdt': str(wallet),
            'unrealized_pnl_usdt': str(pnl), 'available_balance_usdt': str(account['availableBalance'])}


async def collect_account_risk(db, pm_client, wallet_client, budget: Mapping) -> dict:
    """Collect GET-only exchange facts and atomically append them to PostgreSQL."""
    if isinstance(pm_client, ReadOnlyBinanceClient) and pm_client.use_unified_account is False:
        return classic_budget_block(budget.get('account_id'))
    evidence = {'account_id': budget.get('account_id'),
                'account_identity': budget.get('account_identity'),
                'baseline': None, 'equity_points': [], 'cashflows': [], 'coverage': [],
                'as_of_ms': None, 'observed_at_ms': int(time.time() * 1000),
                'source_gaps': _budget_gaps(budget)}
    if not isinstance(pm_client, ReadOnlyBinanceClient) or not isinstance(wallet_client, ReadOnlyBinanceClient):
        evidence['source_gaps'].append('readonly_client_required')
    elif (pm_client.base_url.rstrip('/') != 'https://papi.binance.com'
          or pm_client.use_unified_account is not True
          or wallet_client.base_url.rstrip('/') != 'https://api.binance.com'
          or wallet_client.use_unified_account is not False):
        evidence['source_gaps'].append('account_risk_client_route_invalid')
    elif (pm_client._api_key != wallet_client._api_key
          or pm_client._api_secret != wallet_client._api_secret):
        evidence['source_gaps'].append('account_risk_credentials_mismatch')
    if evidence['source_gaps']:
        return {**assess_account_risk(evidence, budget), 'evidence': evidence}

    fingerprint = hashlib.sha256(pm_client._api_key.encode()).hexdigest()
    committed_breaches = []
    try:
        if getattr(db, 'pool', None) is None and callable(getattr(db, 'connect', None)):
            await db.connect()
        if getattr(db, 'pool', None) is None:
            raise RuntimeError('transactional PostgreSQL pool required')
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
                                   'account-risk:' + budget['account_id'])
                await _load_facts(conn, evidence)
                committed_breaches = list(evidence['confirmed_breaches'])
                baseline = evidence['baseline']
                if ('expected_baseline_time_ms' in budget and baseline is None):
                    evidence['source_gaps'].append('baseline_pin_missing')
                elif (baseline and 'expected_baseline_time_ms' in budget
                      and baseline.get('exchange_time_ms') != budget['expected_baseline_time_ms']):
                    evidence['source_gaps'].append('baseline_pin_mismatch')
                elif baseline and (baseline.get('account_identity') != budget['account_identity']
                                 or baseline.get('credential_fingerprint') != fingerprint):
                    evidence['source_gaps'].append('account_risk_baseline_identity_mismatch')
                elif baseline is None and evidence['equity_points']:
                    evidence['source_gaps'].append('baseline_missing_with_existing_history')
                else:
                    await _collect_locked(conn, pm_client, wallet_client, budget, evidence, fingerprint)
                evidence['observed_at_ms'] = int(time.time() * 1000)
                assessed = assess_account_risk(evidence, budget)
                if assessed['status'] == 'SAFETY_REVIEW' and not assessed['source_gaps']:
                    breach = {'account_id': budget['account_id'],
                              'account_identity': budget['account_identity'],
                              'source': 'persisted:account_risk_verified_breach',
                              'baseline_time_ms': evidence['baseline']['exchange_time_ms'],
                              'as_of_ms': assessed['as_of_ms'],
                              'max_drawdown_ratio': assessed['max_drawdown_ratio'],
                              'budget_id': budget['budget_id'],
                              'credential_fingerprint': fingerprint}
                    breach_id = hashlib.sha256(_json(breach).encode()).hexdigest()
                    await conn.execute('INSERT INTO trading.account_risk_breach(account_id,breach_id,payload) '
                                       'VALUES($1,$2,$3::jsonb) ON CONFLICT DO NOTHING',
                                       budget['account_id'], breach_id, _json(breach))
                    evidence.setdefault('confirmed_breaches', []).append(breach)
    except Exception as exc:
        # Do not include raw exception messages: HTTP/database errors may include
        # credentials or response bodies. Callers get a concrete safe error class.
        evidence['source_gaps'].extend(['account_risk_storage_unavailable',
                                         f'account_risk_storage_error:{type(exc).__name__}'])
        # The transaction rolled back; never publish uncommitted in-memory facts.
        evidence.update(baseline=None, equity_points=[], cashflows=[], coverage=[], as_of_ms=None,
                        confirmed_breaches=committed_breaches)
    evidence['observed_at_ms'] = int(time.time() * 1000)
    return {**assess_account_risk(evidence, budget), 'evidence': evidence}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _payload(row):
    value = row['payload']
    return json.loads(value) if isinstance(value, str) else dict(value)


async def _load_facts(conn, evidence):
    account = evidence['account_id']
    baseline = await conn.fetchrow('SELECT payload FROM trading.account_risk_baseline WHERE account_id=$1', account)
    evidence['baseline'] = _payload(baseline) if baseline else None
    breaches = await conn.fetch('SELECT payload FROM trading.account_risk_breach WHERE account_id=$1', account)
    evidence['confirmed_breaches'] = [_payload(row) for row in breaches]
    for table, key, order in (
        ('equity_point', 'equity_points', 'sample_time_ms'),
        ('transfer', 'cashflows', 'exchange_time_ms, transfer_type, event_id'),
        ('coverage', 'coverage', 'start_ms, end_ms'),
    ):
        rows = await conn.fetch(f'SELECT payload FROM trading.account_risk_{table} WHERE account_id=$1 ORDER BY {order}', account)
        evidence[key] = [_payload(row) for row in rows]
    if evidence['equity_points']:
        evidence['as_of_ms'] = evidence['equity_points'][-1]['exchange_time_ms']
    if evidence['baseline']:
        start = evidence['baseline']['exchange_time_ms']
        evidence['cashflows'] = [event for event in evidence['cashflows'] if event['exchange_time_ms'] > start]


def _coverage_cursor(evidence, typ, start):
    """Use only windows whose raw event identities still exist in storage."""
    cursor = start
    for window in sorted(evidence['coverage'], key=lambda w: (w['start_ms'], w['end_ms'])):
        if (window.get('transfer_type') != typ or window.get('complete') is not True
                or window.get('account_identity') != evidence['account_identity']):
            continue
        ids = window.get('event_ids', [])
        expected = {event['event_id'] for event in evidence['cashflows']
                    if event['transfer_type'] == typ
                    and window['start_ms'] <= event['exchange_time_ms'] <= window['end_ms']}
        if len(ids) != len(set(ids)) or window.get('total') != len(ids) or set(ids) != expected:
            continue
        if window['start_ms'] <= cursor + 1 <= window['end_ms']:
            cursor = window['end_ms']
    return cursor


def _six_month_cutoff(now_ms):
    now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    month_index = now.year * 12 + now.month - 1 - 6
    year, month = divmod(month_index, 12)
    month += 1
    return int(now.replace(year=year, month=month,
                           day=min(now.day, calendar.monthrange(year, month)[1])).timestamp() * 1000)


async def _read_transfer_window(wallet, budget, typ, start, end):
    window = {'account_id': budget['account_id'], 'account_identity': budget['account_identity'],
              'source': TRANSFER_SOURCE, 'transfer_type': typ, 'start_ms': start,
              'end_ms': end, 'complete': False, 'total': None, 'event_ids': [],
              'raw_pages': [], 'source_gaps': []}
    events = []
    total, page, seen = None, 1, set()
    while True:
        params = {'type': typ, 'startTime': start, 'endTime': end, 'current': page, 'size': 100}
        request_start = int(time.time() * 1000)
        try:
            response = await wallet._request('GET', '/sapi/v1/asset/transfer', params, signed=True)
        except Exception as exc:
            window['source_gaps'].append(f'transfer_request_failed:{typ}:{page}:{type(exc).__name__}')
            break
        window['raw_pages'].append({'page': page, 'request_start_ms': request_start,
                                    'request_end_ms': int(time.time() * 1000), 'response': response})
        if not isinstance(response, Mapping) or type(response.get('total')) is not int or response['total'] < 0:
            window['source_gaps'].append(f'transfer_total_invalid:{typ}:{page}')
            break
        if total is None:
            total = window['total'] = response['total']
        elif total != response['total']:
            window['source_gaps'].append(f'transfer_total_changed:{typ}:{page}')
            break
        rows = response.get('rows', [] if total == 0 else None)
        expected_size = min(100, max(0, total - (page - 1) * 100))
        if not isinstance(rows, list) or len(rows) != expected_size:
            window['source_gaps'].append(f'transfer_page_incomplete:{typ}:{page}')
            break
        for row in rows:
            if not isinstance(row, Mapping):
                window['source_gaps'].append(f'transfer_row_invalid:{typ}:{page}')
                continue
            eid, stamp, amount = row.get('tranId'), row.get('timestamp'), _number(row.get('amount'))
            if (type(eid) not in (int, str) or str(eid) == '' or str(eid) in seen
                    or not _ms(stamp) or not start <= stamp <= end
                    or row.get('type') != typ or not isinstance(row.get('asset'), str)
                    or amount is None or amount <= 0):
                window['source_gaps'].append(f'transfer_identity_or_time_invalid:{typ}:{page}')
                continue
            seen.add(str(eid))
            events.append({'account_id': budget['account_id'], 'account_identity': budget['account_identity'],
                           'source': TRANSFER_SOURCE, 'transfer_type': typ, 'event_id': str(eid),
                           'exchange_time_ms': stamp, 'asset': row['asset'], 'amount': str(amount),
                           'status': row.get('status'), 'raw': dict(row)})
        if window['source_gaps'] or page * 100 >= total:
            break
        page += 1
        if page > 10_000:
            window['source_gaps'].append(f'transfer_page_limit:{typ}')
            break
    window['event_ids'] = sorted(seen)
    window['complete'] = not window['source_gaps'] and total == len(seen)
    return window, events


async def _collect_locked(conn, pm, wallet, budget, evidence, fingerprint):
    request_start = int(time.time() * 1000)
    try:
        raw = await pm._request('GET', '/papi/v1/account', signed=True)
    except Exception as exc:
        evidence['source_gaps'].append(f'equity_request_failed:{type(exc).__name__}')
        return
    request_end = int(time.time() * 1000)
    if not isinstance(raw, Mapping):
        evidence['source_gaps'].append('equity_response_invalid')
        return
    actual, margin = _number(raw.get('actualEquity')), _number(raw.get('accountEquity'))
    update = raw.get('updateTime')
    if actual is None or margin is None or not _ms(update):
        evidence['source_gaps'].append('equity_signed_fields_missing')
        return
    if not evidence['baseline'] and actual <= 0:
        evidence['source_gaps'].append('baseline_equity_not_positive')
        return
    if update > request_end + 5000 or request_end < request_start:
        evidence['source_gaps'].append('equity_source_time_invalid')
        return
    if request_end - request_start > budget.get('max_snapshot_age_ms', 120_000):
        evidence['source_gaps'].append('equity_request_stale')
        return
    if request_start < budget['decision_at_ms']:
        evidence['source_gaps'].append('equity_pre_activation')
        return
    point = {'account_id': budget['account_id'], 'account_identity': budget['account_identity'],
             'source': EQUITY_SOURCE, 'equity_field': 'actualEquity', 'equity_usd': str(actual),
             'account_equity_usd': str(margin), 'actual_equity_usd': str(actual),
             # Compatibility key: for equity this is signed-response observation
             # time, NOT the exchange's last-account-update timestamp.
             'exchange_time_ms': request_end, 'exchange_update_time_ms': update,
             'request_start_ms': request_start, 'request_end_ms': request_end,
             'identity_source': 'operator_configured_binding_same_api_credentials',
             'credential_fingerprint': fingerprint, 'raw': dict(raw)}
    old_point = next((p for p in evidence['equity_points'] if p['exchange_time_ms'] == request_end), None)
    if old_point and (old_point['equity_usd'] != point['equity_usd']
                      or old_point['account_identity'] != point['account_identity']):
        evidence['source_gaps'].append('equity_fact_conflict')
        return
    if evidence['as_of_ms'] is not None and request_end < evidence['as_of_ms']:
        evidence['source_gaps'].append('equity_collection_time_reversed')
        return

    baseline = evidence['baseline']
    start = baseline['exchange_time_ms'] if baseline else request_start - 1
    cutoff = _six_month_cutoff(request_end)
    new_windows, new_events = [], []
    for typ in TRANSFER_TYPES:
        cursor = _coverage_cursor(evidence, typ, start)
        if cursor + 1 < cutoff:
            evidence['source_gaps'].append(f'transfer_history_retention_gap:{typ}:{cursor + 1}:{cutoff - 1}')
            cursor = cutoff - 1
        while cursor < request_end:
            window_end = min(request_end, cursor + 7 * 86_400_000)
            window, events = await _read_transfer_window(wallet, budget, typ, cursor + 1, window_end)
            new_windows.append(window)
            new_events.extend(events)
            evidence['source_gaps'].extend(window['source_gaps'])
            if not window['complete']:
                break
            cursor = window_end

    if not baseline:
        # A failed initial transfer request cannot prove that no cashflow crossed
        # the signed equity request. Keep the raw attempt but defer the baseline
        # until the first unambiguous, fully covered signed observation.
        candidate = {**evidence, 'cashflows': evidence['cashflows'] + new_events,
                     'coverage': evidence['coverage'] + new_windows}
        initial_complete = all(_coverage_cursor(candidate, typ, request_start - 1) >= request_end
                               for typ in TRANSFER_TYPES)
        ambiguous = [event for event in candidate['cashflows']
                     if request_start <= event['exchange_time_ms'] <= request_end]
        if not initial_complete:
            evidence['source_gaps'].append('initial_equity_cashflow_coverage_unverified')
        if ambiguous:
            evidence['source_gaps'].append('initial_equity_cashflow_time_ambiguous')
        if initial_complete and not ambiguous:
            baseline = {**point, 'budget_id': budget['budget_id'], 'decision_at_ms': budget['decision_at_ms']}
            await conn.execute('INSERT INTO trading.account_risk_baseline(account_id,payload) '
                               'VALUES($1,$2::jsonb) ON CONFLICT DO NOTHING', budget['account_id'], _json(baseline))
        else:
            for window in new_windows:
                window['unanchored_equity_observation'] = point
    if baseline:
        await conn.execute('INSERT INTO trading.account_risk_equity_point(account_id,sample_time_ms,payload) '
                           'VALUES($1,$2,$3::jsonb) ON CONFLICT DO NOTHING',
                           budget['account_id'], request_end, _json(point))
    for event in new_events:
        existing = await conn.fetchrow('SELECT payload FROM trading.account_risk_transfer '
                                      'WHERE account_id=$1 AND transfer_type=$2 AND event_id=$3',
                                      budget['account_id'], event['transfer_type'], event['event_id'])
        if existing and _payload(existing) != event:
            evidence['source_gaps'].append(f'transfer_fact_conflict:{event["transfer_type"]}:{event["event_id"]}')
            for window in new_windows:
                if window['transfer_type'] == event['transfer_type']:
                    window['complete'] = False
        await conn.execute('INSERT INTO trading.account_risk_transfer '
                           '(account_id,transfer_type,event_id,exchange_time_ms,payload) '
                           'VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING',
                           budget['account_id'], event['transfer_type'], event['event_id'],
                           event['exchange_time_ms'], _json(event))
    for window in new_windows:
        coverage_id = hashlib.sha256(_json(window).encode()).hexdigest()
        await conn.execute('INSERT INTO trading.account_risk_coverage '
                           '(account_id,coverage_id,transfer_type,start_ms,end_ms,payload) '
                           'VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT DO NOTHING',
                           budget['account_id'], coverage_id, window['transfer_type'],
                           window['start_ms'], window['end_ms'], _json(window))
    await _load_facts(conn, evidence)
