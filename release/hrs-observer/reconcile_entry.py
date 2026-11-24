"""Append an HRS exchange entry receipt using authenticated GET and persisted ownership."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from urllib.parse import urlsplit

import asyncpg

from shared.binance_api import BinanceClient
from strategies.hrs.decision_capture import canonical_payload

KIND = 'exchange_entry_reconciliation'


async def reconcile(symbol, order_id, *, write):
    # Credentials stay in the existing HRS container. Exchange requests are GET only.
    api = BinanceClient(os.environ['BINANCE_API_KEY'], os.environ['BINANCE_API_SECRET'],
                        testnet=os.getenv('BINANCE_TESTNET', 'false').lower() == 'true')
    try:
        order = await api.get_order(symbol, order_id)
        observed = datetime.now(timezone.utc)
    finally:
        await api.close()
    if not order or order.get('symbol') != symbol or order.get('orderId') != order_id:
        raise ValueError('exchange_order_identity_unproven')
    client_id = order['clientOrderId']
    executed_at = datetime.fromtimestamp(order['updateTime'] / 1000, timezone.utc)
    account = os.environ.get('TRADE_ACCOUNT_ID', 'default')
    dsn = urlsplit(os.environ['DATABASE_URL'])._replace(query='').geturl()
    db = await asyncpg.connect(dsn, timeout=8, command_timeout=15)
    try:
        async with db.transaction(readonly=True, isolation='repeatable_read'):
            binding = await db.fetchrow("""
                SELECT r.created_at, s.id, s.entry_mode, s.direction, s.resolution, s.triggered_at
                FROM trading.entry_risk_reservation r
                JOIN hrs.hrs_signals s ON s.id::text=r.geometry->>'signal_id' AND s.symbol=r.symbol
                WHERE r.account_id=$1 AND r.strategy_id='hrs' AND r.symbol=$2 AND r.client_order_id=$3
            """, account, symbol, client_id)
            if not binding or binding['entry_mode'] not in ('standard', 'emm', 'semi_emm', 'lv_rm'):
                raise ValueError('reservation_signal_binding_unproven')
            expected_id = 'risk_' + hashlib.sha256(f"{account}:hrs:{binding['id']}".encode()).hexdigest()[:30]
            if (expected_id != client_id or binding['resolution'] == 'shadow'
                    or not binding['triggered_at'] <= binding['created_at'] <= executed_at <= observed):
                raise ValueError('reservation_signal_identity_unproven')
            payload = dict(version='hrs_exchange_entry_v1', account_id=account, signal_id=binding['id'],
                           order={key: order[key] for key in ('orderId', 'clientOrderId', 'symbol', 'side',
                                                             'status', 'reduceOnly', 'executedQty', 'updateTime')})
            quantity = Decimal(str(order['executedQty']))
            if (binding['direction'] not in ('long', 'short')
                    or order['side'] != ('BUY' if binding['direction'] == 'long' else 'SELL')
                    or order['status'] not in ('FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH')
                    or order['reduceOnly'] not in (False, 'false')
                    or not quantity.is_finite() or quantity <= 0):
                raise ValueError('exchange_order_is_not_a_verified_entry')
            owners = await db.fetch("""
                SELECT DISTINCT strategy FROM trading.trade_records
                WHERE account_id=$1 AND symbol=$2 AND order_id=$3
            """, account, symbol, str(order_id))
            if {row['strategy'] for row in owners} != {'hrs'}:
                raise ValueError('entry_order_ownership_unproven')
            fills = await db.fetch("""
                SELECT side,qty,trade_time_utc FROM trading.execution_fact_fill
                WHERE account_id=$1 AND symbol=$2 AND order_id=$3
            """, account, symbol, order_id)
            if (not fills or any(f['side'] != order['side'] or f['qty'] is None or f['qty'] <= 0
                                 or not binding['created_at'] <= f['trade_time_utc'] <= executed_at for f in fills)
                    or sum(f['qty'] for f in fills) != quantity):
                raise ValueError('exchange_fill_quantity_unproven')
            # Attach to the original scan so this receipt cannot introduce a new scan slot.
            budget = await db.fetch("""
                SELECT run_id, payload, payload_sha256 FROM hrs.hrs_decision_evidence
                WHERE kind='entry_budget_reserved' AND symbol=$1
                  AND payload->'decision'->>'client_order_id'=$2 AND event_time <= $3
            """, symbol, client_id, executed_at)
            if len(budget) != 1:
                raise ValueError('original_scan_identity_unproven')
            original = json.loads(budget[0]['payload'])
            if (canonical_payload(original)[1] != budget[0]['payload_sha256']
                    or original.get('entry_mode') != binding['entry_mode']
                    or original.get('decision', {}).get('allowed') is not True):
                raise ValueError('original_scan_identity_unproven')
            run_id = budget[0]['run_id']
    finally:
        await db.close()

    encoded, fingerprint = canonical_payload(payload)
    evidence_id = hashlib.sha256(f'{KIND}:{account}:{symbol}:{order_id}'.encode()).hexdigest()
    if write:
        writer = await asyncpg.connect(host=os.environ['DATABASE_HOST'], port=int(os.environ.get('DATABASE_PORT', '5432')),
                                       database=os.environ['DATABASE_NAME'], user=os.environ['AI_TUNER_SNAPSHOT_DB_USER'],
                                       password=os.environ['AI_TUNER_SNAPSHOT_DB_PASSWORD'], timeout=8, command_timeout=15)
        try:
            if await writer.fetchval('SELECT current_user') != 'ai_tuner_snapshot_writer':
                raise ValueError('unexpected_evidence_writer_role')
            async with writer.transaction():
                await writer.execute("""
                    INSERT INTO hrs.hrs_decision_evidence
                    (evidence_id,run_id,kind,symbol,event_time,available_at,payload_sha256,payload)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT (evidence_id) DO NOTHING
                """, evidence_id, run_id, KIND, symbol, executed_at, observed, fingerprint, encoded)
                stored = await writer.fetchrow("""
                    SELECT run_id,kind,symbol,event_time,available_at,recorded_at,payload_sha256,payload
                    FROM hrs.hrs_decision_evidence WHERE evidence_id=$1
                """, evidence_id)
                if not stored or (stored['run_id'], stored['kind'], stored['symbol'], stored['event_time'],
                                  stored['payload_sha256']) != (run_id, KIND, symbol, executed_at, fingerprint):
                    raise ValueError('existing_entry_receipt_conflict')
                if canonical_payload(json.loads(stored['payload']))[1] != fingerprint:
                    raise ValueError('existing_entry_receipt_hash_mismatch')
        finally:
            await writer.close()
    return dict(evidence_id=evidence_id, payload_sha256=fingerprint, entry_mode=binding['entry_mode'],
                run_id=run_id, fill_count=len(fills), persisted=write)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('symbol')
    parser.add_argument('order_id', type=int)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(reconcile(args.symbol, args.order_id, write=args.write))))
