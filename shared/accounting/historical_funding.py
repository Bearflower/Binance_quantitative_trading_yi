"""Recover funding ownership inside a fully covered interval ending flat."""
from datetime import timedelta
from decimal import Decimal
from itertools import groupby

from .derived_attribution import derive_income
from .economic_ledger import coverage_gaps
from .position_replay import replay_position


def recover(event, anchor, fills, owners, cursors):
    account, symbol, stamp = event['account_id'], event['symbol'], event['event_time_utc']
    stop = anchor['flat_at']
    if (anchor['account_id'], anchor['symbol']) != (account, symbol) or anchor['exchange_qty'] != 0 or stop <= stamp:
        return None, 'invalid_flat_anchor'
    scoped = sorted((f for f in fills if (f['account_id'], f['symbol']) == (account, symbol)
                     and f['trade_time_utc'] < stop), key=lambda f: f['trade_time_utc'], reverse=True)
    qty = Decimal(0)
    window = []
    for time, group in groupby(scoped, key=lambda f: f['trade_time_utc']):
        batch = list(group)
        for f in batch:
            if f['side'] not in ('BUY', 'SELL') or f['qty'] <= 0:
                return None, 'invalid_fill'
            qty -= f['qty'] if f['side'] == 'BUY' else -f['qty']
        window.extend(batch)
        if time == stamp:
            return None, 'funding_fill_time_collision'
        if time < stamp and qty == 0:
            # Exchange timestamps have millisecond resolution.
            start = time - timedelta(milliseconds=1)
            gaps = coverage_gaps(cursors, account, int(time.timestamp()*1000),
                                 int(stop.timestamp()*1000), {symbol})
            if 'fills:' + symbol in gaps:
                return None, 'fill_coverage_incomplete'
            if any(str(f['order_id']) not in owners for f in window):
                return None, 'order_owner_missing'
            replay = replay_position(dict(anchor, flat_at=start), window, owners, stop, 0)
            row, reason = derive_income(event, {}, owners, replay)
            if row:
                row['evidence'].update(flat_anchor=stop.isoformat(), reconstruction='reverse_from_flat',
                                       fill_orders=[[str(f['trade_id']), str(f['order_id'])] for f in window])
            return row, reason
    return None, 'earlier_flat_boundary_missing'
