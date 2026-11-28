"""Restore ordinary entries after a recoverable stream or handoff pause."""
import hashlib
import json
import logging
import time
from decimal import Decimal

from .planner import PlannedOrder, PlanLimitExceeded
from .policy import grid_signal_usable

logger = logging.getLogger(__name__)
STREAM_PAUSE = 'user stream disconnected'
HANDOFF_PAUSE = 'flat grid handoff'
RECOVERABLE_PAUSES = (STREAM_PAUSE, HANDOFF_PAUSE)


class StreamRecovery:
    def __init__(self, trailing):
        self.trailing = trailing
        self.engine = trailing.engine
        self.store = trailing.store
        self.conn = self.store.conn
        self.conn.execute('''CREATE TABLE IF NOT EXISTS grid_stream_restore (
            source_id TEXT PRIMARY KEY REFERENCES order_intent(client_order_id),
            epoch_id INTEGER NOT NULL, replacement_id TEXT, outcome TEXT)''')

    def capture(self, epoch, *, reason=STREAM_PAUSE):
        # Fence submissions and persist the pre-cancel set in the same commit.
        # A second disconnect can interrupt a partially restored book.
        move = self.trailing.pending(epoch)
        sources = {o['client_order_id'] for o in self.store.active_orders()
                   if o['epoch_id'] == epoch and o['role'] == 'ENTRY'}
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if move:
                # Abort the unfinished move back to its committed window.
                # Its own rollback will restore protection. Queue only the old
                # edge (or the residual of its already-submitted replacement).
                raw = json.loads(move['payload_json'])
                replacement = self.store.get_order(move['add_client_id'])
                if not (raw.get('restore') and replacement):
                    sources.discard(move['add_client_id'])
                    if move['cancel_client_id']:
                        sources.add(move['cancel_client_id'])
                raw.update(outcome='ABORTED', restore_abandoned=True)
                self.conn.execute("UPDATE grid_move SET payload_json=? WHERE move_id=?",
                                  (json.dumps(raw), move['move_id']))
            self.conn.executemany('INSERT OR IGNORE INTO grid_stream_restore(source_id,epoch_id) VALUES(?,?)',
                                  [(source, epoch) for source in sorted(sources)])
            self.store.set_engine_state('PAUSED', reason)
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK')
            raise

    def signals_allow_resume(self, epoch, pending):
        window = self.trailing.state.window(epoch)
        if not window:
            return False
        if not pending:
            latest = self.conn.execute('SELECT payload_json FROM decisions ORDER BY rowid DESC LIMIT 1').fetchone()
            pending = [json.loads(latest[0])] if latest else []
        now = int(time.time() * 1000)
        return bool(pending) and all(grid_signal_usable(row, now) for row in pending)

    async def resume(self, epoch, filters):
        # May have crashed midway through the original cancel sweep.
        reason = self.store.engine_state()['lock_reason']
        context = self.store.epoch(epoch)
        if self.store.active_orders() and not await self.engine.pause(
                epoch, stop_loss_low=Decimal(context['stop_loss_low']),
                stop_loss_high=Decimal(context['stop_loss_high']), reason=reason):
            return False
        if (not await self.engine.reconcile() or self.store.stopped_epoch(epoch)
                or self.store.engine_state()['state'] != 'PAUSED'):
            return False
        self.trailing.ensure_epoch(epoch, filters)
        move = self.trailing.pending(epoch)
        if move:
            self.trailing.state.stage(move['move_id'], 'ABORTING')
        if not self.signals_allow_resume(epoch, self.store.pending_decisions()):
            return False
        self.store.set_engine_state('ACTIVE', reason)
        logger.info('grid resuming after stream reconciliation: epoch=%s reason=%s', epoch, reason)
        return True

    async def restore(self, epoch, filters):
        if self.trailing.pending(epoch) or not self.trailing.can_submit(epoch):
            return
        await self.engine.recover_obligations(epoch, filters)
        if not self.trailing.can_submit(epoch):
            return
        rows = self.conn.execute("""SELECT s.source_id,o.* FROM grid_stream_restore s
            JOIN order_intent o ON s.source_id=o.client_order_id
            WHERE s.epoch_id=? AND s.outcome IS NULL ORDER BY o.level_index""", (epoch,)).fetchall()
        if not rows:
            self.store.set_engine_state('ACTIVE')
            logger.info('grid stream recovery complete: epoch=%s', epoch)
            return
        if not await self.engine.reconcile() or not self.trailing.can_submit(epoch):
            return
        window = self.trailing.state.window(epoch)
        mark = await self.engine.exchange.mark_price()
        if not await self.trailing.guard(epoch, mark):
            return
        occupied = {o['level_index'] for o in self.store.active_orders()}
        for row in rows:
            source = row['source_id']
            if row['status'] not in ('FILLED', 'CANCELED', 'EXPIRED'):
                raise ValueError('stream recovery source is not terminal')
            price = Decimal(row['price'])
            qty = Decimal(row['quantity']) - Decimal(row['executed_qty'])
            index, side = row['level_index'], row['side']
            if index in occupied or not window['lower_index'] <= index <= window['upper_index']:
                outcome = 'LEVEL_REASSIGNED'
            elif qty < filters.min_qty or price * qty < filters.min_notional:
                outcome = 'NO_TRADABLE_REMAINDER'
            elif (price >= mark if side == 'BUY' else price <= mark):
                continue  # Retry at the original price when it is passive again.
            else:
                order = PlannedOrder(index, side, price, qty)
                try:
                    await self.trailing._budget(order)
                except PlanLimitExceeded:
                    continue
                if self.store.next_decision() or not self.trailing.can_submit(epoch):
                    return
                client_id = 'eg-s-' + hashlib.sha256(source.encode()).hexdigest()[:24]
                self.conn.execute('BEGIN IMMEDIATE')
                try:
                    self.store.prepare_order(epoch, index, 'ENTRY', side, price, qty, client_id)
                    self.conn.execute("UPDATE grid_stream_restore SET replacement_id=?,outcome='SUBMITTED' WHERE source_id=?",
                                      (client_id, source))
                    self.conn.execute('COMMIT')
                except Exception:
                    self.conn.execute('ROLLBACK')
                    raise
                # One POST per durable intent. After restart reconciliation
                # queries the exact ID even when the response was lost.
                result = await self.engine.exchange.place_limit(side, qty, price, client_id)
                self.trailing._record_order(client_id, result)
                logger.info(
                    "grid entry restored: epoch=%s source_client_order_id=%s client_order_id=%s exchange_order_id=%s "
                    "side=%s price=%s quantity=%s status=%s",
                    epoch, source, client_id, result["orderId"], side, price, qty, result["status"],
                )
                if result['status'] in ('CANCELED', 'EXPIRED'):
                    self.conn.execute('INSERT OR IGNORE INTO grid_stream_restore(source_id,epoch_id) VALUES(?,?)',
                                      (client_id, epoch))
                if not await self.engine.reconcile():
                    return
                return  # Recheck live risk, signals and stops before the next entry.
            self.conn.execute('UPDATE grid_stream_restore SET outcome=? WHERE source_id=?', (outcome, source))
