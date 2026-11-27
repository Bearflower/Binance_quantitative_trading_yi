"""Publish approved classic allocation atomically, without rewriting strategy configs.

The historical exception can override only the exact reviewed freeze revisions.
A later freeze, a revoked receipt, stale evidence, or a genuine risk breach wins.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from shared.accounting.history_tolerance import read_history_acceptance
from shared.accounting.allocation_provenance import entries_digest, validate_allocation_chain
from shared.accounting.income_integrity import require_income_integrity
from strategies.hrs.loss_gate import LossGateState
from ai_tuner.allocation.history_acceptance import amount
from ai_tuner.monitor.classic_account_risk import risk_allows_allocation

METHOD = 'classic_history_acceptance_v1'
CST = timezone(timedelta(hours=8))


def risk_policy_digest(row):
    return hashlib.sha256(json.dumps(dict(row), sort_keys=True, separators=(',', ':'),
        default=str, allow_nan=False).encode()).hexdigest()


def _entries(value):
    rows = json.loads(value) if isinstance(value, str) else value
    if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
        raise ValueError('allocation_entries_invalid')
    by_id = {r.get('strategy_id'):r for r in rows}
    if len(by_id) != len(rows) or None in by_id:
        raise ValueError('allocation_strategy_duplicate_or_missing')
    return rows, by_id


def publication_entries(result, previous):
    rows, by_id = _entries(previous['entries'])
    ids = [e.strategy_id for e in result.entries]
    if len(set(ids)) != len(ids) or set(ids) != set(by_id):
        raise ValueError('allocation_strategy_scope_changed')
    if type(previous['id']) is not int or previous['id'] <= 0:
        raise ValueError('previous_allocation_identity_invalid')
    if not isinstance(result.history_acceptance_sha256, str) or len(result.history_acceptance_sha256) != 64:
        raise ValueError('allocation_acceptance_missing')
    digest = entries_digest(rows)
    entries = []
    for entry in result.entries:
        old = by_id[entry.strategy_id]
        if 'holdings_floor_usdt' in old and old.get('publication_method') != METHOD:
            raise ValueError('allocation_previous_floor_method_invalid')
        floor = max(amount(old['allocated_amount']), amount(old.get('holdings_floor_usdt', 0)))
        amount(entry.allocated_amount)
        entries.append(dict(asdict(entry), publication_method=METHOD,
            holdings_floor_usdt=str(floor), previous_allocation_id=previous['id'],
            previous_entries_sha256=digest, history_acceptance_sha256=result.history_acceptance_sha256,
            caliber=result.caliber,allocation_constraint='fixed_hrs_allocation'))
    return entries


def _attribution_ready(risk):
    attribution = risk.get('income_attribution') or {}
    end = attribution.get('coverage_end_ms')
    return (attribution.get('ready') is True
        and attribution.get('account_id') == risk.get('account_id')
        and attribution.get('receipt_sha256') == risk.get('acceptance_sha256') and type(end) is int
        and type(risk.get('as_of_ms')) is int and 0 <= risk['as_of_ms']-end <= 120000)


async def _verify_hrs_loss_latches(conn, policy, checked_at):
    # Allocation is required to refresh the new-month entry projection. Reading
    # that projection here would deadlock rollover; preserve the persisted loss
    # state directly instead. This code never resets either loss latch.
    row = await conn.fetchrow('SELECT segment_id,version,gate_state FROM '
        'trading.hrs_loss_gate_state WHERE account_id=$1',policy.account_id)
    segment = policy.payload.get('hrs_segment_id')
    baseline = policy.payload.get('hrs_baseline_state_version')
    if (row is None or not isinstance(segment,str) or not segment
            or row['segment_id'] != segment or type(baseline) is not int or baseline <= 0
            or type(row['version']) is not int or row['version'] < baseline):
        raise ValueError('classic_allocation_hrs_state_identity_invalid')
    try:
        raw = json.loads(row['gate_state']) if isinstance(row['gate_state'],str) else row['gate_state']
        required = {'segment_id','day','day_origin_nav','daily_latched','rolling_latched','revision','as_of'}
        if not isinstance(raw,dict) or not required <= raw.keys():
            raise ValueError()
        gate = LossGateState.from_dict(raw)
        if (gate.segment_id != segment or type(gate.daily_latched) is not bool
                or type(gate.rolling_latched) is not bool or type(gate.revision) is not int
                or gate.revision < 0 or gate.as_of is None or gate.as_of.tzinfo is None
                or gate.as_of > checked_at or gate.day > checked_at.astimezone(CST).date()
                or (gate.day_origin_nav is not None and (not gate.day_origin_nav.is_finite() or gate.day_origin_nav <= 0))):
            raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError):
        raise ValueError('classic_allocation_hrs_gate_invalid') from None
    # Yesterday's daily stop remains recorded and still denies HRS entries until
    # its own next-day validation succeeds. It must not prevent publishing the
    # allocation that HRS needs to perform that validation. Rolling loss is not
    # day-scoped; neither it nor a current-day daily loss may pass publication.
    if gate.rolling_latched or (gate.daily_latched and gate.day == checked_at.astimezone(CST).date()):
        raise ValueError('classic_allocation_hrs_known_loss_latched')


async def publish_classic_allocation(db, result, policy, risk, reserve_ratio, pnl_month, *, now=None):
    """One current-month insert under risk/entry locks; duplicates return False.

    ``now`` is only an isolated-test clock. Production callers use the database
    clock inside the lock, so queued writers cannot publish on stale timestamps.
    """
    async with db.pool.acquire() as conn:
        async with conn.transaction(isolation='read_committed'):
            await conn.execute("SET LOCAL lock_timeout='5s'")
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                               'classic-account-risk-v1:'+policy.account_id)
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,7183))',policy.account_id)
            checked_at = now or await conn.fetchval('SELECT clock_timestamp()')
            current = checked_at.astimezone(CST)
            expected_pnl = (current.replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
            if result.month != current.strftime('%Y-%m') or pnl_month != expected_pnl:
                raise ValueError('classic_allocation_month_not_current_finished_month')
            live_policy = await read_history_acceptance(conn,policy.account_id,now=checked_at)
            if (live_policy is None or live_policy.receipt_sha256 != policy.receipt_sha256
                    or result.history_acceptance_sha256 != policy.receipt_sha256):
                raise ValueError('classic_allocation_policy_changed')
            point = await conn.fetchrow('SELECT payload FROM trading.classic_account_risk_point '
                'WHERE account_id=$1 ORDER BY sample_time_ms DESC LIMIT 1',policy.account_id)
            stored = (json.loads(point['payload']) if isinstance(point['payload'],str) else dict(point['payload'])) if point else None
            for candidate in (risk,stored):
                if (not risk_allows_allocation(candidate,live_policy,int(checked_at.timestamp()*1000))
                        or not _attribution_ready(candidate)):
                    raise ValueError('classic_allocation_current_risk_unverified')
            # The publisher consumes exactly the latest persisted assessment.
            for field in ('as_of_ms','current_equity_usd','max_drawdown_ratio','income_attribution'):
                if risk.get(field) != stored.get(field):
                    raise ValueError('classic_allocation_risk_changed')
            ceilings = live_policy.payload['allocation_ceilings_usdt']
            entry_ceilings = live_policy.payload.get('entry_risk_ceilings_usdt',{})
            overrides = live_policy.payload.get('allocation_freeze_overrides',{})
            if set(entry_ceilings) != set(ceilings):
                raise ValueError('classic_allocation_entry_caps_missing')
            await require_income_integrity(conn,policy.account_id)
            await _verify_hrs_loss_latches(conn,live_policy,checked_at)
            for sid in sorted(ceilings):
                entry_policy = await conn.fetchrow('''SELECT p.* FROM trading.strategy_risk_policy p
                    JOIN trading.strategy_risk_active a USING(account_id,strategy_id,revision)
                    WHERE p.account_id=$1 AND p.strategy_id=$2
                    AND p.effective_at <= clock_timestamp() AND p.expires_at > clock_timestamp()''',
                    policy.account_id,sid)
                if entry_policy is None:
                    raise ValueError('classic_allocation_entry_policy_missing_or_expired')
                if (entry_policy['mode'] not in ('reduced','capped') or entry_policy['allow_add'] is not False
                        or amount(entry_policy['entry_ceiling']) > amount(entry_ceilings[sid])):
                    raise ValueError('classic_allocation_entry_risk_changed')
                if entry_policy['freeze_allocation'] is True:
                    approval = overrides.get(sid,{})
                    if (approval.get('revision') != entry_policy['revision']
                            or approval.get('policy_sha256') != risk_policy_digest(entry_policy)):
                        raise ValueError('classic_allocation_new_or_unapproved_freeze')
            reserve = amount(reserve_ratio)
            if not Decimal('.15') <= reserve < 1:
                raise ValueError('classic_allocation_reserve_reduced')
            if (len(result.entries) != len(ceilings)
                    or {e.strategy_id for e in result.entries} != set(ceilings)):
                raise ValueError('classic_allocation_strategy_scope_changed')
            values = [amount(e.allocated_amount) for e in result.entries]
            if any(v.as_tuple().exponent < -8 or v > amount(ceilings[e.strategy_id])
                   for e,v in zip(result.entries,values)):
                raise ValueError('classic_allocation_amount_exceeds_cap_or_precision')
            total, equity = amount(result.total_capital), amount(stored['current_equity_usd'])
            spent = sum(values,Decimal(0))
            if total <= 0 or total > equity or spent > min(total,equity)*(1-reserve):
                raise ValueError('classic_allocation_equity_or_reserve_exceeded')
            if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM public.capital_allocation WHERE left(month::text,7)=$1)',result.month):
                return False
            previous = await conn.fetchrow("SELECT id,entries FROM public.capital_allocation WHERE status='active' AND month<$1 ORDER BY month DESC,id DESC LIMIT 1",
                                           datetime.strptime(result.month,'%Y-%m').date())
            if previous is None:
                raise ValueError('classic_allocation_previous_approval_missing')
            await validate_allocation_chain(conn,previous,account=policy.account_id,policy=live_policy)
            _, previous_entries = _entries(previous['entries'])
            previous_hrs = amount(previous_entries['hrs']['allocated_amount'])
            next_hrs = next(amount(e.allocated_amount) for e in result.entries if e.strategy_id=='hrs')
            if previous_hrs <= 0 or next_hrs != previous_hrs:
                raise ValueError('classic_hrs_allocation_changed')
            entries = publication_entries(result,previous)
            # A damaged prior metadata chain must never be silently propagated.
            for entry in entries:
                if amount(entry['holdings_floor_usdt']) > amount(ceilings[entry['strategy_id']]):
                    raise ValueError('classic_allocation_prior_floor_above_approval')
            inserted = await conn.fetchval('''INSERT INTO public.capital_allocation
                (month,total_capital,strategy_count,is_first_month,entries,status)
                VALUES ($1,$2,$3,$4,$5::jsonb,'active') ON CONFLICT(month) DO NOTHING RETURNING id''',
                datetime.strptime(result.month,'%Y-%m').date(),total,len(entries),result.is_first_month,
                json.dumps(entries,sort_keys=True,allow_nan=False))
            return inserted is not None
