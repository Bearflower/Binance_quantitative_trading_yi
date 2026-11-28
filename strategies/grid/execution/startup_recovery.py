"""Operator-run recovery of an absent, never-acknowledged startup intent.

Not called by the trading loop. Hold ExecutionLock with the executor stopped.
Absence is recorded as ABSENT, never invented as a Binance rejection/cancel.
"""
import hashlib
import json
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path

from shared.binance_api import BinanceAPIError
from .position_recovery import PositionLockRecovery, RecoveryEvidenceError
from .store import DecisionStore


def snapshot(store):
    bound = store.conn.execute("SELECT value FROM schema_meta WHERE key='account_identity'").fetchone()
    return dict(account_identity=bound[0] if bound else None,
                state=store.engine_state(), epoch=store.latest_epoch(),
                orders=store.all_orders(), fills=store.all_fills(),
                protections=store.active_protections(), cursor=store.cursor())


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def candidate(store, now_ms):
    saved = snapshot(store)
    state, epoch = saved['state'], saved['epoch']
    if (state['state'] != 'LOCKED' or state['lock_reason'] != 'cancel result unknown during exit'
            or not epoch or epoch['status'] != 'ACTIVE' or store.active_epoch_id() != epoch['id']
            or now_ms - epoch['created_at_ms'] < 300_000 or saved['protections']):
        raise RecoveryEvidenceError('not an aged locked startup epoch')
    active = store.active_orders()
    if len(active) != 1:
        raise RecoveryEvidenceError('expected one unacknowledged intent')
    missing = active[0]
    if (missing['epoch_id'] != epoch['id'] or missing['role'] != 'ENTRY'
            or missing['status'] != 'PREPARED' or missing['exchange_order_id']
            or Decimal(missing['executed_qty']) != 0 or store.fills(missing['client_order_id'])):
        raise RecoveryEvidenceError('intent has acknowledgement or execution evidence')
    peers = [o for o in saved['orders'] if o['epoch_id'] == epoch['id'] and o != missing]
    if not peers or any(o['role'] != 'ENTRY' or o['status'] != 'CANCELED'
                        or not o['exchange_order_id'] or Decimal(o['executed_qty']) != 0 for o in peers):
        raise RecoveryEvidenceError('startup peers are not canceled without fills')
    return saved, missing


async def capture(store, exchange, now_ms):
    """Require signed no-such-order plus complete flat/order/trade evidence."""
    saved, missing = candidate(store, now_ms)
    identity = await exchange.journal_identity()
    bound = store.conn.execute("SELECT value FROM schema_meta WHERE key='account_identity'").fetchone()
    if not bound or bound[0] != identity or not identity.startswith('classic:'):
        raise RecoveryEvidenceError('signed account differs from journal')
    try:
        await exchange.query_order(missing['client_order_id'])
    except BinanceAPIError as exc:
        if exc.code != -2013:
            raise
    else:
        raise RecoveryEvidenceError('unacknowledged order exists or absence is unproven')
    history = {}
    class EvidenceExchange:
        def __getattr__(self, name):
            return getattr(exchange, name)
        async def trade_history(self, start, end):
            history['trades'] = await exchange.trade_history(start, end)
            return history['trades']
        async def order_history(self, start, end):
            history['orders'] = await exchange.order_history(start, end)
            return history['orders']
    # Prove the proposed settlement on a private copy before touching live data.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/'proof.sqlite3'
        raw = sqlite3.connect(path)
        try:
            store.conn.backup(raw)
        finally:
            raw.close()
        clone = DecisionStore(path)
        try:
            clone.conn.execute("UPDATE order_intent SET status='ABSENT' WHERE client_order_id=?",
                               (missing['client_order_id'],))
            clone.close_epoch(saved['epoch']['id'])
            await PositionLockRecovery(clone, EvidenceExchange())._verify_flat(saved['epoch'], now_ms)
        finally:
            clone.close()
    if fingerprint(snapshot(store)) != fingerprint(saved):
        raise RecoveryEvidenceError('journal changed during proof')
    return dict(checked_at_ms=now_ms, identity=identity, journal_sha256=fingerprint(saved),
                epoch_id=saved['epoch']['id'], client_order_id=missing['client_order_id'],
                query_code=-2013, history=history, history_sha256=fingerprint(history))


def commit(store, first, second, recovered_at_ms):
    """Commit two matching independent samples and a fresh-signal barrier."""
    keys = ('identity', 'journal_sha256', 'epoch_id', 'client_order_id', 'query_code', 'history_sha256')
    if (any(first[k] != second[k] for k in keys)
            or not 5000 <= second['checked_at_ms']-first['checked_at_ms'] <= 60_000
            or not 0 <= recovered_at_ms-second['checked_at_ms'] <= 30_000
            or any(s['query_code'] != -2013 or fingerprint(s['history']) != s['history_sha256']
                   for s in (first, second))):
        raise RecoveryEvidenceError('two fresh matching samples required')
    store.conn.execute('BEGIN IMMEDIATE')
    try:
        saved, missing = candidate(store, recovered_at_ms)
        if (fingerprint(saved) != first['journal_sha256'] or missing['client_order_id'] != first['client_order_id']
                or saved['epoch']['id'] != first['epoch_id']):
            raise RecoveryEvidenceError('journal changed before recovery commit')
        store.conn.execute("UPDATE order_intent SET status='ABSENT' WHERE client_order_id=?",
                           (missing['client_order_id'],))
        store.close_epoch(first['epoch_id'])
        store.conn.execute("UPDATE engine_state SET state='FLAT',lock_reason=NULL,updated_at_ms=? WHERE id=1",
                           (recovered_at_ms,))
        store.conn.executemany('INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)', [
            ('position_recovery_cutoff_ms', str(recovered_at_ms)),
            ('startup_absence_recovery:'+str(first['epoch_id']), json.dumps(dict(
                first=first, second=second, recovered_at_ms=recovered_at_ms), sort_keys=True)),
        ])
        store.conn.execute('COMMIT')
    except Exception:
        store.conn.execute('ROLLBACK')
        raise
