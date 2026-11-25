"""Durable moving windows; historic price indices never change identity."""
import hashlib
import json
import time
from dataclasses import asdict
from decimal import Decimal

from .trailing_plan import TrailingPolicy, price_at


class TrailingStore:
    def __init__(self, store):
        self.store = store
        self.conn = store.conn
        self.conn.execute('''CREATE TABLE IF NOT EXISTS grid_trailing (
            epoch_id INTEGER PRIMARY KEY REFERENCES grid_epoch(id),
            policy_json TEXT NOT NULL, market_state TEXT NOT NULL,
            lower_index INTEGER NOT NULL, upper_index INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0)''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS grid_price_level (
            epoch_id INTEGER NOT NULL REFERENCES grid_epoch(id), level_index INTEGER NOT NULL,
            price TEXT NOT NULL, PRIMARY KEY(epoch_id,level_index))''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS grid_move (
            move_id TEXT PRIMARY KEY, epoch_id INTEGER NOT NULL REFERENCES grid_epoch(id),
            revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
            cancel_client_id TEXT, add_client_id TEXT NOT NULL,
            stage TEXT NOT NULL, created_at_ms INTEGER NOT NULL, last_error TEXT,
            UNIQUE(epoch_id,revision))''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS grid_retired_obligation (
            client_order_id TEXT PRIMARY KEY REFERENCES order_intent(client_order_id),
            epoch_id INTEGER NOT NULL, revision INTEGER NOT NULL)''')

    def window(self, epoch):
        row = self.conn.execute('SELECT * FROM grid_trailing WHERE epoch_id=?', (epoch,)).fetchone()
        return dict(row) if row else None

    def policy(self, epoch):
        raw = json.loads(self.window(epoch)['policy_json'])
        return TrailingPolicy(**{k: v if k in ('grid_count', 'mode') or v is None else Decimal(v)
                                  for k, v in raw.items()})

    def ensure(self, epoch, filters):
        if self.window(epoch):
            policy = self.policy(epoch)
            if (policy.tick_size != filters.tick_size or policy.step_size != filters.step_size
                    or policy.min_qty != filters.min_qty or policy.min_notional != filters.min_notional):
                raise ValueError('trailing exchange filters changed')
            return policy
        context = self.store.epoch(epoch)
        row = self.conn.execute('SELECT payload_json FROM decisions WHERE decision_id=?',
                                (context['decision_id'],)).fetchone()
        if row is None:
            raise ValueError('trailing epoch lacks original signal')
        decision = json.loads(row[0]); params = decision['grid_params']
        levels = tuple(map(Decimal, json.loads(context['levels_json'])))
        quantities = {Decimal(o['quantity']) for o in self.store.all_orders()
                      if o['epoch_id'] == epoch and o['role'] == 'ENTRY'}
        if len(quantities) != 1 or len(levels) != params['grid_count'] + 1:
            raise ValueError('trailing epoch initial grid evidence mismatch')
        quantity = quantities.pop()
        optional = lambda key: Decimal(str(params[key])) if params.get(key) is not None else None
        policy = TrailingPolicy(
            raw_lower=Decimal(str(params['lower_boundary'])), raw_upper=Decimal(str(params['upper_boundary'])),
            grid_count=params['grid_count'], mode=params['grid_mode'],
            tick_size=filters.tick_size, step_size=filters.step_size,
            min_qty=filters.min_qty, min_notional=filters.min_notional,
            stop_low=Decimal(context['stop_loss_low']), stop_high=Decimal(context['stop_loss_high']),
            move_up=optional('stop_move_up_price'), move_down=optional('stop_move_down_price'),
            quote_per_order=quantity * sum(levels, Decimal(0)) / len(levels))
        if any(price_at(policy, index) != price for index, price in enumerate(levels)):
            raise ValueError('trailing initial price lattice mismatch')
        encoded = json.dumps(asdict(policy), default=str, sort_keys=True)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            self.conn.execute('INSERT INTO grid_trailing VALUES(?,?,?,?,?,0)',
                              (epoch, encoded, decision['state'], 0, len(levels) - 1))
            self.conn.executemany('INSERT INTO grid_price_level VALUES(?,?,?)',
                                 [(epoch, i, str(p)) for i, p in enumerate(levels)])
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK'); raise
        return policy

    def level(self, epoch, index):
        price = price_at(self.policy(epoch), index)
        row = self.conn.execute('SELECT price FROM grid_price_level WHERE epoch_id=? AND level_index=?',
                                (epoch, index)).fetchone()
        if row and Decimal(row[0]) != price:
            raise ValueError('immutable trailing level changed')
        self.conn.execute('INSERT OR IGNORE INTO grid_price_level VALUES(?,?,?)', (epoch, index, str(price)))
        return price

    def pending(self, epoch):
        rows = self.conn.execute("SELECT * FROM grid_move WHERE epoch_id=? AND stage!='DONE' ORDER BY revision",
                                 (epoch,)).fetchall()
        if len(rows) > 1:
            raise ValueError('multiple pending grid moves')
        return dict(rows[0]) if rows else None

    def prepare(self, epoch, shift, cancel_client_id):
        revision = self.window(epoch)['revision'] + 1
        identity = hashlib.sha256(('%s:%s' % (epoch, revision)).encode()).hexdigest()[:24]
        move_id = 'move-' + identity; client_id = 'eg-m-' + identity
        payload = asdict(shift)
        context = self.store.epoch(epoch)
        payload.update(previous_stop_low=context['stop_loss_low'], previous_stop_high=context['stop_loss_high'])
        self.conn.execute('INSERT INTO grid_move VALUES(?,?,?,?,?,?,?,?,NULL)',
                          (move_id, epoch, revision, json.dumps(payload, default=str),
                           cancel_client_id, client_id, 'PLANNED', int(time.time() * 1000)))
        return self.pending(epoch)

    def stage(self, move_id, stage, error=None):
        self.conn.execute('UPDATE grid_move SET stage=?,last_error=? WHERE move_id=?',
                          (stage, str(error) if error else None, move_id))

    def finish(self, move):
        shift = json.loads(move['payload_json'])
        aborted = shift.get('outcome') == 'ABORTED'
        window = self.window(move['epoch_id'])
        lower = window['lower_index'] if aborted else shift['lower_index']
        upper = window['upper_index'] if aborted else shift['upper_index']
        low = shift['previous_stop_low'] if aborted else shift['stop_low']
        high = shift['previous_stop_high'] if aborted else shift['stop_high']
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute('UPDATE grid_trailing SET lower_index=?,upper_index=?,revision=? '
                                    'WHERE epoch_id=? AND revision=?',
                                    (lower, upper, move['revision'],
                                     move['epoch_id'], move['revision'] - 1))
            if row.rowcount != 1:
                raise ValueError('trailing window revision conflict')
            self.conn.execute('UPDATE grid_epoch SET stop_loss_low=?,stop_loss_high=? WHERE id=?',
                              (low, high, move['epoch_id']))
            for order in self.store.all_orders():
                target = order['level_index'] + (1 if order['side'] == 'BUY' else -1)
                if (order['epoch_id'] == move['epoch_id'] and order['role'] == 'REVERSE'
                        and not lower <= target <= upper):
                    self.conn.execute('INSERT OR IGNORE INTO grid_retired_obligation VALUES(?,?,?)',
                                      (order['client_order_id'], move['epoch_id'], move['revision']))
            self.stage(move['move_id'], 'DONE')
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK'); raise
