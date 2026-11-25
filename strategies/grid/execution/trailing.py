"""Serialize bounded edge moves without flattening or discarding old pairs."""
import json
import logging
import time
from decimal import Decimal

from shared.binance_api import BinanceAPIError
from .contracts import decode_grid_params
from .engine import log_cancel_result
from .exposure import projected_exposure, validate_exposure
from .fills import ProtectionCoordinator
from .planner import PlanLimitExceeded
from .policy import grid_candles_current, grid_signal_usable
from .trailing_plan import next_shift, price_at, termination
from .trailing_store import TrailingStore


logger = logging.getLogger(__name__)


class TrailingController:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.exchange = engine.exchange
        self.state = TrailingStore(self.store)
        self.submission_allowed = None

    def can_submit(self, epoch):
        if (self.store.engine_state()['state'] != 'ACTIVE' or self.store.active_epoch_id() != epoch
                or self.store.stopped_epoch(epoch)):
            return False
        if self.submission_allowed is not None and not self.submission_allowed():
            return False
        row = self.store.conn.execute('SELECT payload_json FROM decisions ORDER BY rowid DESC LIMIT 1').fetchone()
        if row is None:
            return False
        latest = json.loads(row[0])
        now = int(time.time() * 1000)
        return self.state.window(epoch) is not None and grid_signal_usable(latest, now)

    def ensure_epoch(self, epoch, filters):
        return self.state.ensure(epoch, filters)

    def pending(self, epoch):
        return self.state.pending(epoch)

    def keep_active(self, decision, epoch):
        window = self.state.window(epoch)
        return bool(window and self.store.engine_state()['state'] == 'ACTIVE'
                    and grid_signal_usable(decision, int(time.time() * 1000)))

    @staticmethod
    def _candidate_geometry(params):
        try:
            proposed = decode_grid_params(params or {})
        except ValueError:
            return None
        return {
            "grid_count": proposed.grid_count, "grid_mode": proposed.grid_mode.name,
            "lower_boundary": proposed.lower_boundary, "upper_boundary": proposed.upper_boundary,
            "stop_loss_low": proposed.stop_loss_low, "stop_loss_high": proposed.stop_loss_high,
            "stop_move_down_price": proposed.stop_move_down_price,
            "stop_move_up_price": proposed.stop_move_up_price,
        }

    @staticmethod
    def _material_change(before, after, interval):
        if before["grid_count"] != after["grid_count"] or before["grid_mode"] != after["grid_mode"]:
            return True
        for key in ("lower_boundary", "upper_boundary", "stop_loss_low", "stop_loss_high",
                    "stop_move_down_price", "stop_move_up_price"):
            old, new = before[key], after[key]
            if old is None or new is None:
                if (old is None) != (new is None):
                    return True
            elif abs(new - old) >= interval:
                return True
        return False

    def refresh_confirmed(self, decision, epoch):
        """Require a materially different geometry confirmed by consecutive closes."""
        now = int(time.time() * 1000)
        if not grid_signal_usable(decision, now) or not grid_candles_current(decision, now):
            return False
        latest = self.store.conn.execute("SELECT decision_id FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()
        if not latest or latest[0] != decision["decision_id"]:
            return False
        proposed = self._candidate_geometry(decision.get("grid_params"))
        if proposed is None:
            return False

        window = self.state.window(epoch)
        policy = self.state.policy(epoch)
        low = price_at(policy, window["lower_index"])
        high = price_at(policy, window["upper_index"])
        interval = (high - low) / policy.grid_count
        context = self.store.epoch(epoch)
        current = {
            "grid_count": policy.grid_count, "grid_mode": policy.mode,
            "lower_boundary": low, "upper_boundary": high,
            "stop_loss_low": Decimal(context["stop_loss_low"]), "stop_loss_high": Decimal(context["stop_loss_high"]),
            "stop_move_down_price": policy.move_down, "stop_move_up_price": policy.move_up,
        }
        if not self._material_change(current, proposed, interval):
            return False

        close_ms = decision["last_closed_kline_ms"]["1h"]
        history = self.store.conn.execute(
            "SELECT payload_json FROM decisions WHERE calculated_at_ms>=? ORDER BY rowid DESC",
            (context["created_at_ms"],),
        )
        for row in history:
            previous = json.loads(row[0])
            previous_close = previous["last_closed_kline_ms"]["1h"]
            if (previous["state"] != decision["state"] or previous.get("executable") is not True
                    or not (previous.get("feasibility") or {}).get("feasible")):
                return False
            if previous_close == close_ms:
                continue
            if previous_close != close_ms - 3_600_000:
                return False
            prior = self._candidate_geometry(previous.get("grid_params"))
            return (prior is not None and self._material_change(current, prior, interval)
                    and not self._material_change(prior, proposed, interval))
        return False

    async def guard(self, epoch, price=None):
        """Check whole-grid bounds even when there is no position to protect."""
        context = self.store.epoch(epoch)
        price = await self.exchange.mark_price() if price is None else price
        reason = termination(price, Decimal(context['stop_loss_low']), Decimal(context['stop_loss_high']))
        if reason:
            logger.warning('grid terminal boundary: epoch=%s side=%s price=%s', epoch, reason, price)
            self.store.settle_stop(epoch)
            await self.engine.exit_all(epoch, trigger=f"grid_boundary:{reason}")
            return False
        return True

    async def _budget(self, shift, cancel_id=None, *, adopt=False):
        # Existing facts are reconciled/validated before projecting the changed
        # set. This includes old reverse orders outside the sliding window.
        await self.engine.check_limits()
        orders = [o for o in self.store.active_orders() if o['client_order_id'] != cancel_id]
        extra = {'role': 'ENTRY', 'side': shift.side, 'quantity': str(shift.quantity),
                 'executed_qty': '0', 'price': str(shift.price)}
        positions = await self.exchange.position()
        mark = await self.exchange.mark_price()
        journal = sum((Decimal(f['quantity']) * (1 if f['side'] == 'BUY' else -1)
                       for f in self.store.all_fills()), Decimal(0))
        if self.engine.limits is not None:
            validate_exposure(projected_exposure(positions, orders + ([] if adopt else [extra]), mark, journal), self.engine.limits)
            if adopt:
                return
            free = Decimal(str((await self.exchange.account_info())['totalAvailableBalance']))
            position = Decimal(positions[0]['positionAmt'])
            addition = max(abs(position + (shift.quantity if shift.side == 'BUY' else -shift.quantity))
                           - abs(position), Decimal(0))
            if not free.is_finite() or free < addition * shift.price / self.engine.limits.leverage:
                raise PlanLimitExceeded('insufficient available margin for trailing edge')

    def _record_order(self, client_id, row):
        intent = self.store.get_order(client_id)
        if (row.get('symbol') != 'ETHUSDT' or row.get('clientOrderId') != client_id
                or row.get('side') != intent['side']
                or Decimal(row['origQty']) != Decimal(intent['quantity'])
                or Decimal(row['price']) != Decimal(intent['price'])
                or (intent['exchange_order_id'] and str(row['orderId']) != intent['exchange_order_id'])
                or Decimal(row['executedQty']) < Decimal(intent['executed_qty'])):
            raise ValueError('trailing order does not match durable intent')
        self.store.record_order(client_id, str(row['orderId']), row['status'], Decimal(row['executedQty']))
        return row['status'] in ('FILLED', 'CANCELED', 'EXPIRED')

    def _waiting(self, move, error):
        self.state.stage(move['move_id'], move['stage'], error)
        logger.warning('grid move awaiting confirmation: move=%s stage=%s error_type=%s',
                       move['move_id'], move['stage'], type(error).__name__)
        if int(time.time() * 1000) - move['created_at_ms'] >= 300_000:
            self.engine._lock('trailing order confirmation uncertain')
        return False

    async def _bounds(self, epoch, low, high):
        # Persist desired protection before network I/O so reconnect can finish
        # a replacement without guessing which trigger the move requested.
        self.store.conn.execute('UPDATE grid_epoch SET stop_loss_low=?,stop_loss_high=? WHERE id=?',
                                (str(low), str(high), epoch))
        positions = await self.exchange.position()
        if not await ProtectionCoordinator(self.store, self.exchange).ensure(
                epoch, Decimal(positions[0]['positionAmt']), low, high):
            return False
        # Full identity/type/trigger/close-all validation must precede the edge
        # POST; ensure's placement acknowledgement alone is insufficient.
        return (await self.engine.reconcile() and self.store.engine_state()['state'] == 'ACTIVE'
                and self.store.active_epoch_id() == epoch and not self.store.stopped_epoch(epoch))

    async def _abort_unsent(self, move, filters):
        """Cancel a superseded shift without inventing executions or inventory."""
        from .trailing_plan import Shift
        raw = json.loads(move['payload_json'])
        raw['outcome'] = 'ABORTED'
        move['payload_json'] = json.dumps(raw)
        move['stage'] = 'ABORTING'
        self.store.conn.execute("UPDATE grid_move SET payload_json=?,stage='ABORTING' WHERE move_id=?",
                                (move['payload_json'], move['move_id']))
        window = self.state.window(move['epoch_id'])
        source = self.store.get_order(move['cancel_client_id']) if move['cancel_client_id'] else None
        current = await self.exchange.mark_price()
        restore = None
        if source:
            remainder = Decimal(source['quantity']) - Decimal(source['executed_qty'])
            price = Decimal(source['price'])
            occupied = any(o['level_index'] == source['level_index'] for o in self.store.active_orders())
            passive = (source['side'] == 'BUY' and price < current) or (source['side'] == 'SELL' and price > current)
            if (not occupied and passive and remainder >= filters.min_qty
                    and remainder * price >= filters.min_notional):
                restore = Shift('RESTORE', window['lower_index'], window['upper_index'],
                                source['level_index'], source['level_index'], source['side'], price, remainder,
                                Decimal(raw['previous_stop_low']), Decimal(raw['previous_stop_high']))
                try:
                    await self._budget(restore)
                except PlanLimitExceeded:
                    return True
            raw['restore_remaining'] = str(remainder)
            raw['restore_reason'] = ('RESTORED' if restore else 'OCCUPIED' if occupied else
                                     'MARKETABLE' if not passive else 'BELOW_MINIMUM')
        raw['outcome'] = 'ABORTED'
        if restore is None:
            move['payload_json'] = json.dumps(raw)
            self.store.conn.execute('UPDATE grid_move SET payload_json=? WHERE move_id=?',
                                    (move['payload_json'], move['move_id']))
            if not await self._bounds(move['epoch_id'], Decimal(raw['previous_stop_low']), Decimal(raw['previous_stop_high'])):
                return False
            self.state.finish(move)
            logger.info('grid move aborted without new exposure: move=%s', move['move_id'])
            return True
        raw['restore'] = {k: str(v) if isinstance(v, Decimal) else v for k, v in restore.__dict__.items()}
        raw['abandoned_add_client_id'] = move['add_client_id']
        client_id = 'eg-mr-' + move['move_id'][5:]
        self.store.conn.execute('BEGIN IMMEDIATE')
        try:
            self.store.prepare_order(move['epoch_id'], restore.add_index, 'ENTRY', restore.side,
                                     restore.price, restore.quantity, client_id)
            self.store.conn.execute("UPDATE grid_move SET payload_json=?,add_client_id=?,stage='RESTORE_READY' WHERE move_id=?",
                                    (json.dumps(raw), client_id, move['move_id']))
            self.store.conn.execute('COMMIT')
        except Exception:
            self.store.conn.execute('ROLLBACK'); raise
        return await self.tick(move['epoch_id'], filters)

    async def tick(self, epoch, filters):
        from .trailing_plan import Shift
        self.ensure_epoch(epoch, filters)
        price = await self.exchange.mark_price()
        if not await self.guard(epoch, price):
            return False
        if self.store.engine_state()['state'] != 'ACTIVE':
            return False
        if not self.can_submit(epoch):
            return False
        move = self.state.pending(epoch)
        if move is None:
            if not await self.engine.reconcile():
                return False
            window = self.state.window(epoch)
            try:
                shift = next_shift(self.state.policy(epoch), window['lower_index'], window['upper_index'], price)
                if shift is None:
                    return True
                active = [o for o in self.store.active_orders() if o['epoch_id'] == epoch]
                occupant = next((o for o in active if o['level_index'] == shift.add_index), None)
                if occupant and (occupant['side'] != shift.side or Decimal(occupant['price']) != shift.price):
                    return True  # Never cancel an unfinished pair to free a slot.
                cancel = next((o['client_order_id'] for o in active
                               if o['level_index'] == shift.remove_index and o['role'] == 'ENTRY'), None)
                await self._budget(shift, cancel, adopt=bool(occupant))
            except PlanLimitExceeded as exc:
                logger.info('grid move deferred: epoch=%s reason=%s', epoch, exc)
                return True
            move = self.state.prepare(epoch, shift, cancel)
        raw = json.loads(move['payload_json'])
        fields = raw.get('restore', raw)
        shift = Shift(**{k: fields[k] if k in ('direction', 'side', 'lower_index', 'upper_index', 'remove_index', 'add_index')
                         else Decimal(fields[k]) for k in Shift.__dataclass_fields__})
        if move['stage'] == 'ABORTING':
            if raw.get('restore_abandoned'):
                if not await self._bounds(epoch, Decimal(raw['previous_stop_low']), Decimal(raw['previous_stop_high'])):
                    return False
                self.state.finish(move)
                return True
            return await self._abort_unsent(move, filters)
        if raw.get('outcome') == 'ABORTED' and move['stage'] == 'CANCELED':
            if not await self._bounds(epoch, Decimal(raw['previous_stop_low']), Decimal(raw['previous_stop_high'])):
                return False
            self.state.finish(move)
            return True
        if move['stage'] in ('PLANNED', 'CANCELING'):
            cancel_id = move['cancel_client_id']
            if cancel_id:
                try:
                    intent = self.store.get_order(cancel_id)
                    resolved_by = "cancel_response"
                    if move['stage'] == 'PLANNED':
                        self.state.stage(move['move_id'], 'CANCELING')
                        move['stage'] = 'CANCELING'
                        try:
                            row = await self.exchange.cancel_order(cancel_id)
                        except BinanceAPIError as exc:
                            if exc.code != -2011:
                                raise
                            resolved_by = "status_query"
                            row = await self.exchange.query_order(cancel_id)
                    else:
                        resolved_by = "status_query"
                        row = await self.exchange.query_order(cancel_id)
                        if not self._record_order(cancel_id, row):
                            # Unlike a new-order POST, a validated live DELETE
                            # is idempotent. Recover a crash before HTTP send.
                            resolved_by = "cancel_response"
                            row = await self.exchange.cancel_order(cancel_id)
                    if not self._record_order(cancel_id, row):
                        return self._waiting(move, RuntimeError('cancel is not terminal'))
                    log_cancel_result(intent, row, trigger=f"grid_move:{move['move_id']}", resolved_by=resolved_by)
                except Exception as exc:
                    return self._waiting(move, exc)
            self.state.stage(move['move_id'], 'CANCELED'); move['stage'] = 'CANCELED'
        if move['stage'] == 'CANCELED':
            if not await self.engine.reconcile():
                return False
            # A cancellation can reveal a partial fill. Reconcile protects it;
            # service old pair obligations before allowing another edge entry.
            await self.engine.recover_obligations(epoch, filters, allow_moving=True)
            if self.store.engine_state()['state'] != 'ACTIVE':
                return False
            current = await self.exchange.mark_price()
            if not await self.guard(epoch, current):
                return False
            if (shift.side == 'BUY' and shift.price >= current) or (shift.side == 'SELL' and shift.price <= current):
                return await self._abort_unsent(move, filters)
            occupant = next((o for o in self.store.active_orders() if o['level_index'] == shift.add_index), None)
            if occupant:
                if occupant['side'] != shift.side or Decimal(occupant['price']) != shift.price:
                    return True
                await self._budget(shift, adopt=True)
                if not await self._bounds(epoch, shift.stop_low, shift.stop_high):
                    return False
                raw['adopted_client_id'] = occupant['client_order_id']
                move['payload_json'] = json.dumps(raw)
                self.store.conn.execute('UPDATE grid_move SET payload_json=? WHERE move_id=?',
                                        (move['payload_json'], move['move_id']))
                self.state.finish(move)
                return True
            try:
                await self._budget(shift)
            except PlanLimitExceeded as exc:
                logger.info('grid move add deferred: move=%s reason=%s', move['move_id'], exc)
                return True
            self.store.conn.execute('BEGIN IMMEDIATE')
            try:
                self.store.prepare_order(epoch, shift.add_index, 'ENTRY', shift.side,
                                         shift.price, shift.quantity, move['add_client_id'])
                self.state.stage(move['move_id'], 'ADD_READY')
                self.store.conn.execute('COMMIT')
            except Exception:
                self.store.conn.execute('ROLLBACK'); raise
            move['stage'] = 'ADD_READY'
        if move['stage'] in ('ADD_READY', 'SENDING', 'RESTORE_READY', 'RESTORE_SENDING'):
            client_id = move['add_client_id']
            intent = self.store.get_order(client_id)
            try:
                ready = move['stage'] in ('ADD_READY', 'RESTORE_READY')
                if ready:
                    current = await self.exchange.mark_price()
                    if not await self.guard(epoch, current):
                        return False
                    if ((shift.side == 'BUY' and shift.price >= current)
                            or (shift.side == 'SELL' and shift.price <= current)):
                        was_restoring = raw.get('outcome') == 'ABORTED'
                        raw['outcome'] = 'ABORTED'
                        if was_restoring:
                            raw['restore_abandoned'] = True
                            raw['restore_reason'] = 'MARKETABLE'
                        payload = json.dumps(raw)
                        self.store.conn.execute('BEGIN IMMEDIATE')
                        try:
                            self.store.record_order(client_id, '', 'CANCELED', Decimal(0))
                            self.store.conn.execute("UPDATE grid_move SET stage='ABORTING',payload_json=? WHERE move_id=?",
                                                    (payload, move['move_id']))
                            self.store.conn.execute('COMMIT')
                        except Exception:
                            self.store.conn.execute('ROLLBACK'); raise
                        move['stage'] = 'ABORTING'
                        move['payload_json'] = payload
                        if was_restoring:
                            if not await self._bounds(epoch, Decimal(raw['previous_stop_low']), Decimal(raw['previous_stop_high'])):
                                return False
                            self.state.finish(move)
                            return True
                        return await self._abort_unsent(move, filters)
                    try:
                        await self._budget(shift, client_id)
                    except PlanLimitExceeded as exc:
                        logger.info('grid move send deferred: move=%s reason=%s', move['move_id'], exc)
                        return True
                    if not await self._bounds(epoch, shift.stop_low, shift.stop_high):
                        return False
                    if not self.can_submit(epoch):
                        return False
                    # PREPARED is known not sent. Persist SENT atomically with
                    # the move stage before making exactly one HTTP submission.
                    self.store.conn.execute('BEGIN IMMEDIATE')
                    try:
                        self.store.conn.execute("UPDATE order_intent SET status='SENT' WHERE client_order_id=?", (client_id,))
                        sending_stage = 'RESTORE_SENDING' if raw.get('outcome') == 'ABORTED' else 'SENDING'
                        self.state.stage(move['move_id'], sending_stage)
                        self.store.conn.execute('COMMIT')
                    except Exception:
                        self.store.conn.execute('ROLLBACK'); raise
                    move['stage'] = sending_stage
                    row = await self.exchange.place_limit(shift.side, shift.quantity, shift.price, client_id)
                elif intent['status'] in ('NEW', 'PARTIALLY_FILLED', 'FILLED'):
                    row = await self.exchange.query_order(client_id)
                else:
                    row = await self.exchange.query_order(client_id)
                self._record_order(client_id, row)
                if row['status'] not in ('NEW', 'PARTIALLY_FILLED', 'FILLED'):
                    return self._waiting(move, RuntimeError('new edge order not accepted'))
            except Exception as exc:
                return self._waiting(move, exc)
            # Protect any immediate REST-discovered fills before recording the
            # completed window or releasing the service replenishment fence.
            if not await self.engine.reconcile():
                return False
            self.state.finish(move)
            logger.info('grid moved: epoch=%s move=%s direction=%s lower=%s upper=%s', epoch, move['move_id'],
                        shift.direction, shift.lower_index, shift.upper_index)
        return True
