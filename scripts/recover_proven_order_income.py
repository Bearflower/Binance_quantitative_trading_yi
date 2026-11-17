"""Recover income from authenticated orders and verified closed positions.

Dry-run by default. This maintenance command rechecks the legacy account proof,
optionally adds ownership supported by an authenticated order export, then uses
the existing insert-only writer for ownership and derived income.
Closed-position mode requires a fixed signed flat anchor and complete fills.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import csv
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from itertools import groupby
from zoneinfo import ZoneInfo

from scripts.reconcile_income_attribution import (
    _identity_matches, _key, _same_derivation, apply_plan,
)
from scripts.report_economic_ledger import month_window_ms
from shared.accounting.derived_attribution import derive_income
from shared.accounting.economic_ledger import coverage_gaps
from shared.accounting.position_replay import owner_at, replay_position
from shared.accounting.report_reader import read_monthly_economic_report


def json_value(value):
    if isinstance(value, (datetime, Decimal)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    raise TypeError(type(value).__name__)


def verified_owners(account, fills, owners, trades):
    fills_by_order = defaultdict(list)
    trades_by_order = defaultdict(list)
    for fill in fills:
        fills_by_order[(fill['symbol'], str(fill['order_id']))].append(fill)
    for trade in trades:
        trades_by_order[(trade['symbol'], str(trade.get('order_id')))].append(trade)
    accepted = {}
    for owner in owners:
        identity = (owner['symbol'], str(owner['order_id']))
        if owner['account_id'] != account or owner['source_kind'] != 'trade_record':
            continue
        if identity not in fills_by_order:
            continue
        proof = owner['evidence']
        if isinstance(proof, str):
            proof = json.loads(proof)
        if (proof.get('source_kind') != 'trade_record'
                or str(proof.get('order_id')) != identity[1]):
            continue
        source_ids = set(proof.get('source_ids', []))
        related = trades_by_order[identity]
        if any(row['strategy'] != owner['strategy'] or row['account_id'] not in (None, account)
               for row in related):
            continue
        sources = [row for row in related if str(row['id']) in source_ids]
        if not source_ids or len(sources) != len(source_ids):
            continue
        verified = True
        for source in sources:
            if source['account_id'] == account:
                continue
            recorded = source['executed_at']
            if recorded.tzinfo is not None:
                verified = False
                break
            recorded = recorded.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
            account_proof = proof.get('account_proof')
            if account_proof == 'authenticated_exchange_order_symbol_side_creation_time':
                created = datetime.fromisoformat(proof['order_created_at'])
                verified = (created.tzinfo is not None
                            and len(proof.get('order_archive_sha256', '')) == 64
                            and bool(proof.get('download_id'))
                            and str(source['side']).upper() == proof.get('side')
                            and abs((recorded - created).total_seconds()) <= 10)
            elif account_proof == 'authenticated_exchange_order_symbol_side_time':
                verified = any(str(fill['side']).upper() == str(source['side']).upper()
                               and abs((recorded - fill['trade_time_utc']).total_seconds()) <= 10
                               for fill in fills_by_order[identity])
            else:
                verified = False
            if not verified:
                verified = False
                break
        if verified:
            accepted[identity] = dict(owner, evidence=proof)
    return accepted


def archive_order_plan(account, archive, archive_sha256, download_id, fills, trades, owners):
    """Recheck an authenticated order export before proposing new ownership rows."""
    from datetime import timezone

    known = {(r['symbol'], str(r['order_id'])) for r in owners}
    exported = {}
    if len({r['Uid'] for r in archive}) != 1:
        raise ValueError('archive must contain one authenticated account')
    for row in archive:
        key = (row['Symbol'], row['Order No'])
        if key in exported:
            raise ValueError('duplicate archive order identity')
        exported[key] = row
    fills_by_order = defaultdict(list)
    trades_by_order = defaultdict(list)
    for fill in fills:
        if fill['account_id'] == account:
            fills_by_order[(fill['symbol'], str(fill['order_id']))].append(fill)
    for trade in trades:
        trades_by_order[(trade['symbol'], str(trade.get('order_id')))].append(trade)
    proposed = []
    for key, row in exported.items():
        if key in known or key not in fills_by_order:
            continue
        scoped_fills = fills_by_order[key]
        executed = Decimal(row['Executed Amount'])
        if executed <= 0 or sum((Decimal(str(f['qty'])) for f in scoped_fills), Decimal(0)) != executed:
            continue
        if any(str(f['side']).upper() != row['Side'].upper() for f in scoped_fills):
            continue
        related = trades_by_order[key]
        strategies = {t['strategy'] for t in related}
        if len(strategies) != 1 or any(t['account_id'] not in (None, account) for t in related):
            continue
        created = datetime.strptime(row['Time(UTC)'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        sources = [t for t in related if str(t['side']).upper() == row['Side'].upper()
                   and t['executed_at'].tzinfo is None
                   and abs((t['executed_at'].replace(tzinfo=ZoneInfo('Asia/Shanghai')) - created).total_seconds()) <= 10]
        if not sources:
            continue
        source_ids = sorted(str(t['id']) for t in sources)
        evidence = dict(source_kind='trade_record', source_ids=source_ids, order_id=key[1],
                        account_proof='authenticated_exchange_order_symbol_side_creation_time',
                        order_created_at=created.isoformat(), side=row['Side'].upper(),
                        order_archive_sha256=archive_sha256, download_id=download_id)
        proposed.append(dict(account_id=account, symbol=key[0], order_id=int(key[1]),
                             strategy=next(iter(strategies)), source_kind='trade_record',
                             source_id=source_ids[0], evidence=evidence))
    return proposed


def recovery_plan(account, incomes, fills, owners, trades, reviewed, existing):
    accepted = verified_owners(account, fills, owners, trades)
    by_trade = {(f['symbol'], str(f['trade_id'])): f for f in fills}
    manual = {_key(r): r for r in reviewed}
    prior = {_key(r): r for r in existing}
    plan = {'order_rows': [], 'derived_rows': [], 'conflicts': []}
    candidates = []
    for income in incomes:
        if income['income_type'] not in {'COMMISSION', 'REALIZED_PNL'}:
            continue
        symbol = income['symbol']
        fill = by_trade.get((symbol, str(income.get('trade_id'))))
        if fill is None:
            continue
        owner = accepted.get((symbol, str(fill['order_id'])))
        if owner is None:
            continue
        row, reason = derive_income(income, {str(fill['trade_id']): fill},
                                    {str(fill['order_id']): owner}, None)
        if reason:
            continue
        candidates.append(row)
        key = _key(row)
        if key in manual:
            if not _identity_matches(manual[key], row) or manual[key]['strategy'] != row['strategy']:
                plan['conflicts'].append({'key': key, 'reason': 'manual_owner_conflict'})
        elif key in prior:
            if (not _identity_matches(prior[key], row)
                    or prior[key]['strategy'] != row['strategy']
                    or not _same_derivation(prior[key], row)):
                plan['conflicts'].append({'key': key, 'reason': 'derived_evidence_conflict'})
        else:
            plan['derived_rows'].append(row)
    owner_fields = ('account_id', 'symbol', 'order_id', 'strategy',
                    'source_kind', 'source_id', 'evidence')
    stable_owners = [{field: owner.get(field) for field in owner_fields}
                     for _, owner in sorted(accepted.items())]
    stable = json.dumps({'income': sorted(candidates, key=_key), 'owners': stable_owners}, sort_keys=True,
                        separators=(',', ':'), default=json_value)
    return plan, hashlib.sha256(stable.encode()).hexdigest(), len(accepted)


class Reader:
    def __init__(self, connection):
        self.connection = connection

    async def fetch_all(self, sql, *args):
        return await self.connection.fetch(sql, *args)


def closed_lot_plan(account, incomes, fills, owners, anchors, cursors, reviewed, existing):
    """Attribute only fully verified closed lots ending before a signed flat anchor."""
    manual = {_key(row): row for row in reviewed}
    prior = {_key(row): row for row in existing}
    plan = {'order_rows': [], 'derived_rows': [], 'conflicts': []}
    candidates = []
    rejected = []
    for anchor in anchors:
        symbol = anchor['symbol']
        stop = anchor['flat_at']
        if anchor['account_id'] != account or Decimal(str(anchor['exchange_qty'])) != 0 or not anchor['evidence']:
            raise ValueError('unverified flat anchor')
        scoped = [dict(f) for f in fills if f['account_id'] == account and f['symbol'] == symbol]
        if any(f['trade_time_utc'] == stop for f in scoped):
            raise ValueError('flat anchor collides with a fill')
        scoped = sorted((f for f in scoped if f['trade_time_utc'] < stop),
                        key=lambda f: (f['trade_time_utc'], int(f['trade_id'])), reverse=True)
        owner_map = {str(row['order_id']): row for row in owners
                     if row['account_id'] == account and row['symbol'] == symbol}
        qty = Decimal(0)
        window = []
        suffix = []
        right = stop
        for stamp, batch in groupby(scoped, key=lambda f: f['trade_time_utc']):
            group = list(batch)
            window.extend(group)
            suffix.extend(group)
            for fill in group:
                amount = Decimal(str(fill['qty']))
                if fill['side'] not in ('BUY', 'SELL') or not amount.is_finite() or amount <= 0:
                    raise ValueError('invalid authenticated fill quantity/side')
                qty -= amount if fill['side'] == 'BUY' else -amount
            if qty != 0:
                continue
            left = stamp - timedelta(milliseconds=1)
            events = [i for i in incomes if i['account_id'] == account and i['symbol'] == symbol
                      and left < i['event_time_utc'] < right]
            if events:
                gaps = coverage_gaps(cursors, account, int(left.timestamp() * 1000),
                                     int(stop.timestamp() * 1000), {symbol})
                replay = replay_position(dict(anchor, flat_at=left), window, owner_map, right, 0,
                                         coverage_complete='fills:' + symbol not in gaps)
                if not replay.verified:
                    rejected.append({'symbol': symbol, 'start': left, 'end': right,
                                     'reason': replay.reason, 'income_count': len(events)})
                else:
                    reducing = {}
                    position = Decimal(0)
                    active_strategy = None
                    ordered = sorted(window, key=lambda f: (f['trade_time_utc'], int(f['trade_id'])))
                    for fill in ordered:
                        amount = Decimal(str(fill['qty']))
                        delta = amount if fill['side'] == 'BUY' else -amount
                        order_owner = owner_map.get(str(fill['order_id']))
                        if position == 0:
                            active_strategy = order_owner['strategy']
                        elif position * delta < 0:
                            if abs(delta) <= abs(position):
                                reducing[str(fill['trade_id'])] = (active_strategy, fill)
                            else:
                                active_strategy = order_owner['strategy']
                        position += delta
                        if position == 0:
                            active_strategy = None
                    suffix_hash = hashlib.sha256(json.dumps(suffix, sort_keys=True,
                        default=json_value, separators=(',', ':')).encode()).hexdigest()
                    opening_proofs = [dict(owner_map[order_id]) for order_id in sorted(
                        {str(f['order_id']) for f in window} & set(owner_map))]
                    for income in events:
                        if income['asset'] != 'USDT':
                            continue
                        kind = income['income_type']
                        proof = {'source_kind': 'closed_position_replay', 'flat_before': left,
                                 'verification_end': right, 'signed_flat_anchor': dict(anchor),
                                 'suffix_fill_sha256': suffix_hash,
                                 'lot_trade_ids': [str(f['trade_id']) for f in ordered],
                                 'order_evidence': opening_proofs}
                        if kind == 'FUNDING_FEE':
                            strategy, reason = owner_at(replay, income['event_time_utc'])
                            if reason:
                                continue
                        elif kind in ('REALIZED_PNL', 'COMMISSION'):
                            closing = reducing.get(str(income.get('trade_id')))
                            if closing is None:
                                continue
                            strategy, fill = closing
                            if kind == 'COMMISSION' and fill['commission_asset'] != 'USDT':
                                continue
                            expected = (Decimal(str(fill['realized_pnl'])) if kind == 'REALIZED_PNL'
                                        else -Decimal(str(fill['commission'])))
                            if Decimal(str(income['income'])) != expected:
                                continue
                            proof.update(trade_id=str(fill['trade_id']), order_id=str(fill['order_id']))
                        else:
                            continue
                        # Store proof JSON in the same primitive types as the existing writer.
                        proof = json.loads(json.dumps(proof, default=json_value))
                        row = {field: income[field] for field in ('account_id', 'income_type',
                            'tran_id', 'symbol', 'income', 'asset', 'event_time_utc')}
                        row.update(strategy=strategy, method='verified_position_interval',
                                   evidence=proof, algorithm_version=1)
                        candidates.append(row)
                        old = manual.get(_key(row)) or prior.get(_key(row))
                        if old:
                            if not _identity_matches(old, row) or old['strategy'] != strategy:
                                plan['conflicts'].append({'key': _key(row), 'reason': 'existing_owner_conflict'})
                        else:
                            plan['derived_rows'].append(row)
            window = []
            right = left
    stable = json.dumps(sorted(candidates, key=_key), sort_keys=True,
                        separators=(',', ':'), default=json_value)
    return plan, hashlib.sha256(stable.encode()).hexdigest(), rejected


async def run(args):
    import asyncpg
    from datetime import timezone

    def env(key):
        return os.environ.get('DATABASE_' + key) or os.environ.get('DB_' + key)

    start_ms, end_ms = month_window_ms(args.month)
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    account = args.account_id
    if os.environ.get('TRADE_ACCOUNT_ID') != account:
        raise ValueError('credential account binding mismatch')
    connection = await asyncpg.connect(host=env('HOST'), port=int(env('PORT') or 5432),
                                      database=env('NAME'), user=env('USER'), password=env('PASSWORD'))
    try:
        async with connection.transaction(isolation='serializable', readonly=not args.apply):
            from shared.accounting.income_integrity import require_income_integrity
            await require_income_integrity(connection, account)
            reader = Reader(connection)
            before = await read_monthly_economic_report(reader, account, start_ms, end_ms,
                include_reviewed_overrides=True, include_derived_attribution=True)
            incomes = await connection.fetch('SELECT * FROM trading.execution_fact_income WHERE account_id=$1 AND event_time_utc >= $2 AND event_time_utc < $3', account, start, end)
            fills = await connection.fetch('SELECT * FROM trading.execution_fact_fill WHERE account_id=$1 AND trade_time_utc >= $2 AND trade_time_utc < $3', account, start, end)
            owners = await connection.fetch('SELECT * FROM trading.order_strategy_evidence WHERE account_id=$1', account)
            trades = await connection.fetch('SELECT * FROM trading.trade_records')
            reviewed = await connection.fetch('SELECT * FROM trading.reviewed_income_attribution WHERE account_id=$1', account)
            existing = await connection.fetch('SELECT * FROM trading.derived_income_attribution WHERE account_id=$1', account)
            new_owners = []
            if args.order_archive:
                data = Path(args.order_archive).read_bytes()
                if hashlib.sha256(data).hexdigest() != args.archive_sha256 or not args.download_id:
                    raise ValueError('authenticated archive identity/checksum required')
                with Path(args.order_archive).open(encoding='utf-8-sig', newline='') as stream:
                    archive = list(csv.DictReader(stream))
                new_owners = archive_order_plan(account, archive, args.archive_sha256,
                    args.download_id, fills, trades, owners)
                owners = [*owners, *new_owners]
            plan, digest, owner_count = recovery_plan(account, incomes, fills, owners, trades, reviewed, existing)
            plan['order_rows'] = new_owners
            lot_rejections = []
            if args.closed_lots:
                if args.order_archive or not args.anchor_at:
                    raise ValueError('closed-lot recovery requires a fixed anchor and separate order-evidence recovery')
                symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
                anchor_at = datetime.fromisoformat(args.anchor_at)
                anchors = await connection.fetch('SELECT * FROM trading.position_flat_baseline WHERE account_id=$1 AND symbol=ANY($2::text[]) AND flat_at=$3', account, symbols, anchor_at)
                if len(anchors) != len(symbols):
                    raise ValueError('reviewed flat anchors missing')
                lot_fills = await connection.fetch('SELECT * FROM trading.execution_fact_fill WHERE account_id=$1 AND symbol=ANY($2::text[]) AND trade_time_utc <= $3 ORDER BY trade_time_utc,trade_id', account, symbols, anchor_at)
                cursors = await connection.fetch('SELECT * FROM trading.execution_fact_cursor WHERE account_id=$1 AND source=$2 AND symbol=ANY($3::text[])', account, 'userTrades', symbols)
                plan, digest, lot_rejections = closed_lot_plan(account, incomes, lot_fills, owners,
                    anchors, cursors, reviewed, existing)
            if args.apply:
                if args.expected_plan_sha256 != digest:
                    raise ValueError('reviewed plan checksum changed')
                if before['attribution_conflicts']:
                    raise ValueError('existing ledger attribution conflicts')
                await apply_plan(connection, plan, coverage_complete=(before['account_coverage_complete']
                    and not before['coverage_gaps'] and not before['missing_income_fills']))
            after = await read_monthly_economic_report(reader, account, start_ms, end_ms,
                include_reviewed_overrides=True, include_derived_attribution=True)
            for field in ('account_components', 'income_count', 'coverage_gaps', 'missing_income_fills'):
                if before[field] != after[field]:
                    raise ValueError('account facts changed: ' + field)
            if Decimal(after['attributed_usdt_total']) + Decimal(after['unattributed_usdt_total']) != Decimal(after['account_usdt_total']):
                raise ValueError('ledger conservation failed')
            result = {'account_id': account, 'month': args.month, 'mode': 'apply' if args.apply else 'preview',
                      'plan_sha256': digest, 'verified_orders': owner_count, 'plan': plan,
                      'lot_rejections': lot_rejections,
                      'before': before, 'after': after}
        return result
    finally:
        await connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--month', required=True)
    parser.add_argument('--account-id', default='default')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--order-archive')
    parser.add_argument('--archive-sha256')
    parser.add_argument('--download-id')
    parser.add_argument('--closed-lots', action='store_true')
    parser.add_argument('--anchor-at')
    print(json.dumps(asyncio.run(run(parser.parse_args())), default=json_value, sort_keys=True))
