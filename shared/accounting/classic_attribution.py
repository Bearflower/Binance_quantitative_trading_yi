"""Append proven classic income owners without requiring unavailable PM NAV.

Historical acceptance supplies only the authenticated classic flat boundary.
Every income/fill interval used here still needs complete native collection.
Unknown owners remain unknown; no trading endpoint or policy mutation is used.
"""
from __future__ import annotations
from datetime import datetime, timezone
import os
import time

from shared.readonly_client import ReadOnlyBinanceClient
from shared.accounting.history_tolerance import read_history_acceptance
from shared.accounting.equity_bridge import signed_spot_uid_sha256
from shared.accounting.economic_ledger import coverage_gaps
from shared.accounting.income_integrity import require_income_integrity
from scripts.audit_algo_attribution import _fetch_links, _CLASSIC_CONDITIONS
from scripts.reconcile_income_attribution import (
    verified_fact_cutoff, verified_position_at_cutoff, build_replay_by_symbol,
    plan_reconciliation, apply_plan, _INCOME_SQL, _TRADE_SQL, _MANUAL_SQL,
    _EXISTING_SQL, _CURSOR_SQL,
)


class ClassicAttributionBlocked(ValueError):
    pass


def _symbols(data):
    return sorted({r['symbol'] for rows in (data['incomes'],data['fills'])
                   for r in rows if r.get('symbol')})


def classic_plan(acceptance, end, data, quantities, links):
    """Recompute all ownership from the transaction's current immutable facts."""
    account, start = acceptance.account_id, acceptance.classic_flat_at
    symbols = _symbols(data)
    if set(quantities) != set(symbols):
        raise ClassicAttributionBlocked('classic_position_scope_changed')
    gaps = coverage_gaps(data['cursors'],account,int(start.timestamp()*1000),
                         int(end.timestamp()*1000),set(symbols))
    if gaps:
        raise ClassicAttributionBlocked('classic_native_coverage_incomplete')
    if any(not start < f['trade_time_utc'] < end for f in data['fills']):
        raise ClassicAttributionBlocked('classic_flat_boundary_collision')
    baselines={symbol:dict(account_id=account,symbol=symbol,flat_at=start,exchange_qty=0)
               for symbol in symbols}
    replay=build_replay_by_symbol(account,baselines,data['fills'],data['trades'],links,
        data['conditions'],quantities,end,{symbol:True for symbol in symbols})
    plan=plan_reconciliation(data['incomes'],data['fills'],data['trades'],links,
        data['conditions'],data['manual'],data['existing'],replay)
    if plan['conflicts']:
        raise ClassicAttributionBlocked('classic_income_owner_conflict')
    for (_, symbol), result in replay.items():
        if not result.verified and not any(r.get('symbol')==symbol for r in plan['rejected']):
            plan['rejected'].append({'account_id':account,'symbol':symbol,
                                     'reason':result.reason,'scope':'position_replay'})
    # A fresh independent order proof may add source IDs to an existing proof.
    # Preserve the original immutable proof when its identity and owner agree.
    owners={(r['account_id'],r['symbol'],str(r['order_id'])):r for r in data['owners']}
    new=[]
    for row in plan['order_rows']:
        old=owners.get((row['account_id'],row['symbol'],str(row['order_id'])))
        if old and old['strategy'] != row['strategy']:
            raise ClassicAttributionBlocked('classic_order_owner_conflict')
        if not old:new.append(row)
    plan['order_rows']=new
    return plan


async def _facts(connection, account, start, end):
    incomes=[dict(r) for r in await connection.fetch(_INCOME_SQL,account,start,end)]
    fills=[dict(r) for r in await connection.fetch('''SELECT account_id,symbol,trade_id,
        order_id,side,qty,commission,commission_asset,realized_pnl,trade_time_utc
        FROM trading.execution_fact_fill WHERE account_id=$1
        AND trade_time_utc >= $2 AND trade_time_utc < $3''',account,start,end)]
    cursors=[dict(r) for r in await connection.fetch(_CURSOR_SQL,account,
        int(start.timestamp()*1000),int(end.timestamp()*1000))]
    return dict(incomes=incomes,fills=fills,cursors=cursors)


async def reconcile_classic_attribution(db, client, acceptance=None):
    """Verify external positions, then re-read/replay/write in SERIALIZABLE.

    ``acceptance`` may pin a caller's revision; it is never trusted instead of
    rereading the administrator receipt. Any API failure aborts without writes.
    """
    if (not isinstance(client,ReadOnlyBinanceClient) or client.use_unified_account
            or client.testnet or client.base_url.rstrip('/')!='https://fapi.binance.com'):
        raise ClassicAttributionBlocked('classic_readonly_production_client_required')
    account=os.environ.get('TRADE_ACCOUNT_ID','default')
    uid=await signed_spot_uid_sha256(client,require_classic_trading=True)
    policy=await read_history_acceptance(db,account,expected_uid=uid)
    if policy is None:
        return dict(status='NOT_ENABLED',ready=False,pending=['classic_history_acceptance_missing'],reason='classic_history_acceptance_missing')
    if acceptance is not None and acceptance.receipt_sha256!=policy.receipt_sha256:
        raise ClassicAttributionBlocked('classic_acceptance_changed')
    start=policy.classic_flat_at
    target_ms=int(time.time()*1000)-1000
    target=datetime.fromtimestamp(target_ms/1000,timezone.utc)
    async with db.pool.acquire() as connection:
        async with connection.transaction(isolation='repeatable_read',readonly=True):
            await require_income_integrity(connection,account)
            preliminary=await _facts(connection,account,start,target)
    symbols=_symbols(preliminary)
    end_ms=verified_fact_cutoff(preliminary['cursors'],account,
        int(start.timestamp()*1000),target_ms,symbols)
    if end_ms<=int(start.timestamp()*1000) or target_ms-end_ms>120000:
        return dict(status='BLOCKED_DATA',ready=False,pending=['classic_native_watermark_stale'],reason='classic_native_watermark_stale',end_ms=end_ms,coverage_end_ms=end_ms)
    end=datetime.fromtimestamp(end_ms/1000,timezone.utc)
    # Restrict symbols to those actually present before the common cutoff.
    scoped={key:[r for r in preliminary[key] if r['event_time_utc' if key=='incomes' else 'trade_time_utc']<end]
            for key in ('incomes','fills')}
    symbols=_symbols(scoped)
    quantities={}; proofs={}
    for symbol in symbols:
        quantity,reason,proof=await verified_position_at_cutoff(client,symbol,end_ms)
        if reason:
            return dict(status='BLOCKED_DATA',ready=False,pending=[reason],reason=reason,symbol=symbol,end_ms=end_ms,coverage_end_ms=end_ms)
        quantities[symbol]=quantity;proofs[symbol]=proof
    links=await _fetch_links(client,symbols,int(start.timestamp()*1000),end_ms) if symbols else []
    links=[dict(row,account_id=account) for row in links]
    async with db.pool.acquire() as connection:
        async with connection.transaction(isolation='serializable'):
            await require_income_integrity(connection,account)
            current=await read_history_acceptance(connection,account,expected_uid=uid)
            if current is None or current.receipt_sha256!=policy.receipt_sha256:
                raise ClassicAttributionBlocked('classic_acceptance_changed')
            data=await _facts(connection,account,start,end)
            if _symbols(data)!=symbols:
                raise ClassicAttributionBlocked('classic_fact_scope_changed')
            data['trades']=[dict(r) for r in await connection.fetch(_TRADE_SQL,account,symbols)]
            data['conditions']=[dict(r) for r in await connection.fetch(_CLASSIC_CONDITIONS,account,symbols,
                policy.payload['classic_cutover_ms'])]
            data['manual']=[dict(r) for r in await connection.fetch(_MANUAL_SQL,account,start,end)]
            data['existing']=[dict(r) for r in await connection.fetch(_EXISTING_SQL,account,start,end)]
            data['owners']=[dict(r) for r in await connection.fetch('SELECT * FROM trading.order_strategy_evidence WHERE account_id=$1 AND symbol=ANY($2::text[])',account,symbols)]
            plan=classic_plan(policy,end,data,quantities,links)
            for row in plan['derived_rows']:
                if row['method']=='verified_position_interval':
                    row['evidence']['classic_history_acceptance']={
                        'receipt_sha256':policy.receipt_sha256,
                        'flat_evidence_sha256':policy.classic_flat_evidence_sha256,
                        'position_at_cutoff':proofs[row['symbol']]}
            result=await apply_plan(connection,plan,coverage_complete=True)
    return dict(status='PARTIAL' if plan['rejected'] else 'VERIFIED',account_id=account,
        ready=not plan['rejected'],pending=plan['rejected'],coverage_end_ms=end_ms,
        end_ms=end_ms,receipt_sha256=policy.receipt_sha256,write_result=result,
        rejected=plan['rejected'],position_proofs=proofs,raw_facts_mutated=False)
