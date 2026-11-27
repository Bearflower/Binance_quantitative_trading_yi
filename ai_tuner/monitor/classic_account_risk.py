"""Classic USDT risk under an explicit, finite historical acceptance.

The denominator is the approved original classic wallet, never new deposits.
This is observed fixed-capital drawdown, not reconstructed PM unit NAV.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal

from shared.readonly_client import ReadOnlyBinanceClient
from shared.accounting.history_tolerance import known_equity_points
from shared.accounting.equity_bridge import signed_spot_uid_sha256
from shared.binance_account_mode import verify_trading_mode

METHOD = 'classic_fixed_capital_cashflow_adjusted_drawdown_v1'
READY = 'VERIFIED_WITH_ACCEPTED_HISTORY'
ECONOMIC_TYPES = frozenset(('REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE'))
TRANSFER_TYPES = frozenset(('TRANSFER', 'INTERNAL_TRANSFER'))
MAX_GAP_MS = 300000
MAX_AGE_MS = 120000


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite_classic_risk_value')
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def payload(row):
    if not row:
        return None
    value = row['payload']
    return json.loads(value) if isinstance(value, str) else dict(value)


def assess_classic_risk(policy, observation, previous=None, pm_breaches=()):
    """No missing past observation becomes a zero return or a new baseline."""
    o = observation
    base = number(policy.classic_open_equity)
    limit = number(policy.risk_limit_ratio)
    if base <= 0 or limit != Decimal('.05') or policy.payload.get('classic_risk_method') != METHOD:
        raise ValueError('classic_risk_policy_invalid')
    start = int(policy.classic_flat_at.timestamp() * 1000)
    accepted_end = int(policy.accepted_through.timestamp() * 1000)
    result = dict(status='BLOCKED_DATA', account_id=policy.account_id, account_mode='classic',
        budget_id=f'{policy.account_id}-classic-{policy.revision}', policy_revision=policy.revision,
        acceptance_sha256=policy.receipt_sha256, method=METHOD, limit_ratio=str(limit),
        baseline=dict(exchange_time_ms=start, equity_usd=str(base), uid_sha256=policy.exchange_uid_sha256),
        history_status='ACCEPTED_GAPS', full_historical_nav_verified=False,
        accepted_history_through_ms=accepted_end, as_of_ms=o.get('as_of_ms'),
        current_equity_usd=None, current_drawdown_ratio=None, max_drawdown_ratio=None,
        known_breach=None, reasons=[], source_gaps=[], financial_evidence_verified=False)
    gaps = []
    peak, maximum = base, Decimal(0)
    if previous:
        if (previous.get('account_id') != policy.account_id or previous.get('method') != METHOD
                or previous.get('baseline') != result['baseline']):
            raise ValueError('classic_risk_prior_identity_changed')
        result['known_breach'] = previous.get('known_breach')
    for breach in pm_breaches:
        if breach.get('account_id') == policy.account_id and number(breach.get('max_drawdown_ratio', 0)) >= limit:
            result['known_breach'] = dict(breach)
    for point in known_equity_points(policy.payload, start, accepted_end):
        stamp = point['as_of_ms']
        if (type(stamp) is not int or not start <= stamp <= accepted_end
                or len(point.get('source_sha256', '')) != 64):
            raise ValueError('classic_historical_point_invalid')
        adjusted = number(point['equity_usdt']) - number(point['net_transfer_usdt'])
        maximum = max(maximum, (peak-adjusted)/base)
        peak = max(peak, adjusted)
    if previous:
        peak = max(peak, number(previous['high_water_adjusted_equity_usdt']))
        maximum = max(maximum, number(previous['max_drawdown_ratio']))
    result['high_water_adjusted_equity_usdt'] = str(peak)
    result['max_drawdown_ratio'] = str(maximum)
    if maximum >= limit:
        result['known_breach'] = result['known_breach'] or dict(account_id=policy.account_id,
            source='reviewed:classic_historical_equity', as_of_ms=accepted_end,
            max_drawdown_ratio=str(maximum), method=METHOD)
    try:
        at, observed = o['as_of_ms'], o['observed_at_ms']
        if type(at) is not int or type(observed) is not int or not 0 <= observed-at <= MAX_AGE_MS:
            gaps.append('classic_snapshot_stale')
        if o.get('uid_sha256') != policy.exchange_uid_sha256:
            gaps.append('classic_account_identity_mismatch')
        if o.get('single_asset_usdt') is not True:
            gaps.append('classic_single_usdt_required')
        if (o.get('coverage_complete') is not True or o.get('coverage_start_ms') != start
                or o.get('coverage_end_ms') != at or o.get('native_source') != 'signed:GET /fapi/v1/income'):
            gaps.append('classic_income_coverage_incomplete')
        wallet, equity = number(o['wallet_balance_usdt']), number(o['margin_balance_usdt'])
        income, transfer = number(o['cumulative_income_usdt']), number(o['cumulative_net_transfer_usdt'])
        if wallet != base + income + transfer or equity != wallet + number(o['unrealized_pnl_usdt']):
            gaps.append('classic_wallet_conservation_failed')
        last = max(accepted_end, previous['as_of_ms'] if previous else accepted_end)
        if at < last or at-last > MAX_GAP_MS:
            gaps.append('classic_sampling_gap')
            result['unresolved_sampling_gap_through_ms'] = at
        unresolved = (previous or {}).get('unresolved_sampling_gap_through_ms')
        if unresolved and unresolved > accepted_end:
            gaps.append('classic_sampling_gap')
            result['unresolved_sampling_gap_through_ms'] = max(at, unresolved)
        result['financial_evidence_verified'] = not any(g != 'classic_sampling_gap' for g in gaps)
        if result['financial_evidence_verified']:
            adjusted = equity-transfer
            peak = max(peak, adjusted)
            drawdown = max(Decimal(0), (peak-adjusted)/base)
            maximum = max(maximum, drawdown)
            result.update(current_equity_usd=str(equity), adjusted_equity_usdt=str(adjusted),
                high_water_adjusted_equity_usdt=str(peak), current_drawdown_ratio=str(drawdown),
                max_drawdown_ratio=str(maximum), budget_loss_usdt=str(base*limit),
                cumulative_income_usdt=str(income), cumulative_net_transfer_usdt=str(transfer),
                wallet_balance_usdt=str(wallet), observation=o)
            if maximum >= limit:
                result['known_breach'] = result['known_breach'] or dict(account_id=policy.account_id,
                    source='assessed:classic_fixed_capital_drawdown', as_of_ms=at,
                    max_drawdown_ratio=str(maximum), method=METHOD)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        gaps.append('classic_observation_invalid')
    result['source_gaps'] = result['reasons'] = sorted(set(gaps))
    if result['known_breach']:
        result['status'] = 'SAFETY_REVIEW'
        result['reasons'] = [*result['reasons'], 'previously_verified_account_drawdown_breach']
    elif not gaps:
        result['status'] = READY
    return result


async def native_income_window(client, start, end):
    """Fixed windows and explicit pages; tranId is scoped by incomeType."""
    events, seen = [], {}
    cursor = start
    while cursor <= end:
        right = min(end, cursor + 7*86400000-1)
        for page in range(1, 101):
            rows = await client._request('GET', '/fapi/v1/income',
                {'startTime': cursor, 'endTime': right, 'page': page, 'limit': 1000}, signed=True)
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ValueError('classic_income_page_invalid')
            for row in rows:
                if (not isinstance(row, dict) or type(row.get('time')) is not int
                        or not cursor <= row['time'] <= right or row.get('tranId') is None
                        or not isinstance(row.get('incomeType'), str)):
                    raise ValueError('classic_income_identity_invalid')
                amount = number(row.get('income'))
                if amount and (row.get('asset') != 'USDT'
                               or row['incomeType'] not in ECONOMIC_TYPES | TRANSFER_TYPES):
                    raise ValueError('classic_cashflow_type_unclassified')
                key = (row['incomeType'], str(row['tranId']))
                if key in seen:
                    raise ValueError('classic_income_duplicate_or_pagination_overlap')
                seen[key] = row
                events.append(row)
            if len(rows) < 1000:
                break
        else:
            raise ValueError('classic_income_page_budget_exhausted')
        cursor = right+1
    return events


def risk_allows_allocation(risk, policy, now_ms):
    return bool(policy and policy.automatic_allocation is True and isinstance(risk, dict)
        and risk.get('status') == READY and risk.get('account_id') == policy.account_id
        and risk.get('acceptance_sha256') == policy.receipt_sha256
        and risk.get('policy_revision') == policy.revision and not risk.get('known_breach')
        and risk.get('financial_evidence_verified') is True
        and type(risk.get('as_of_ms')) is int and 0 <= now_ms-risk['as_of_ms'] <= MAX_AGE_MS
        and number(risk.get('max_drawdown_ratio')) < number(policy.risk_limit_ratio))


def apply_attribution_gate(result, attribution):
    """Persist ownership readiness alongside the financial state, before INSERT."""
    result['income_attribution'] = attribution
    end = attribution.get('coverage_end_ms') if isinstance(attribution, dict) else None
    if (not isinstance(attribution, dict) or attribution.get('ready') is not True
            or attribution.get('account_id') != result['account_id']
            or attribution.get('receipt_sha256') != result['acceptance_sha256']
            or type(end) is not int or type(result.get('as_of_ms')) is not int
            or not 0 <= result['as_of_ms'] - end <= MAX_AGE_MS):
        result['status'] = 'SAFETY_REVIEW' if result.get('known_breach') else 'BLOCKED_DATA'
        result['reasons'] = [*result.get('reasons', []), 'current_income_attribution_pending']
    return result


async def collect_classic_risk(db, client, account='default', *, attribution=None):
    """Collect only GET facts; append risk state atomically, preserving PM storage."""
    from shared.accounting.history_tolerance import read_history_acceptance
    from ai_tuner.monitor.account_risk import classic_budget_block
    policy = await read_history_acceptance(db, account)
    if policy is None:
        return classic_budget_block(account)
    if (not isinstance(client, ReadOnlyBinanceClient) or client.use_unified_account or client.testnet
            or client.base_url.rstrip('/') != 'https://fapi.binance.com'):
        raise ValueError('classic_readonly_production_client_required')
    await verify_trading_mode(client)
    uid = await signed_spot_uid_sha256(client, require_classic_trading=True)
    if uid != policy.exchange_uid_sha256:
        raise ValueError('classic_account_identity_mismatch')
    async with db.pool.acquire() as conn:
        # Lock precedes repeatable source reads, serializing cumulative state writers.
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout='5s'")
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', 'classic-account-risk-v1:'+account)
            prior = payload(await conn.fetchrow('SELECT payload FROM trading.classic_account_risk_point WHERE account_id=$1 ORDER BY sample_time_ms DESC LIMIT 1', account))
            breaches = [payload(row) for row in await conn.fetch('SELECT payload FROM trading.account_risk_breach WHERE account_id=$1', account)]
            now_policy = await read_history_acceptance(conn, account)
            if now_policy is None or now_policy.receipt_sha256 != policy.receipt_sha256:
                raise ValueError('classic_policy_changed_during_capture')
            start_ms = int(policy.classic_flat_at.timestamp()*1000)
            cursor = prior['as_of_ms'] if prior else start_ms
            if int(time.time()*1000)-cursor > 89*86400000:
                raise ValueError('classic_income_retention_gap')
            started = int(time.time()*1000)
            first = await client.get_account_info()
            point_time = int(time.time()*1000)
            events = await native_income_window(client, cursor+1, point_time)
            second = await client.get_account_info()
            as_of = int(time.time()*1000)
            gap = await native_income_window(client, point_time+1, as_of) if as_of > point_time else []
            if gap or number(first['totalWalletBalance']) != number(second['totalWalletBalance']):
                raise ValueError('classic_snapshot_income_race_retry')
            if as_of-started > MAX_AGE_MS:
                raise ValueError('classic_capture_stale')
            assets = second.get('assets')
            single = isinstance(assets, list) and all(row.get('asset') == 'USDT' or
                (number(row.get('walletBalance', 0)) == 0 and number(row.get('unrealizedProfit', 0)) == 0)
                for row in assets)
            income = number((prior or {}).get('cumulative_income_usdt', 0)) + sum(
                (number(r['income']) for r in events if r['incomeType'] in ECONOMIC_TYPES), Decimal(0))
            flows = number((prior or {}).get('cumulative_net_transfer_usdt', 0)) + sum(
                (number(r['income']) for r in events if r['incomeType'] in TRANSFER_TYPES), Decimal(0))
            observation = dict(as_of_ms=as_of, observed_at_ms=int(time.time()*1000), uid_sha256=uid,
                wallet_balance_usdt=str(second['totalWalletBalance']), margin_balance_usdt=str(second['totalMarginBalance']),
                unrealized_pnl_usdt=str(second['totalUnrealizedProfit']), cumulative_income_usdt=str(income),
                cumulative_net_transfer_usdt=str(flows), coverage_start_ms=start_ms, coverage_end_ms=as_of,
                coverage_complete=True, native_source='signed:GET /fapi/v1/income', single_asset_usdt=single,
                native_interval_start_ms=cursor+1, native_events=events, raw_snapshot=second,
                raw_snapshot_sha256=hashlib.sha256(canonical(second).encode()).hexdigest(),
                exchange_account_initial_margin_usd=str(second.get('totalInitialMargin', '0')))
            result = apply_attribution_gate(assess_classic_risk(policy, observation, prior, breaches), attribution)
            result['exchange_account_initial_margin_usd'] = observation['exchange_account_initial_margin_usd']
            if result['financial_evidence_verified']:
                await conn.execute('INSERT INTO trading.classic_account_risk_point (account_id,policy_revision,sample_time_ms,payload) VALUES ($1,$2,$3,$4::jsonb)', account, policy.revision, as_of, canonical(result))
            return result
