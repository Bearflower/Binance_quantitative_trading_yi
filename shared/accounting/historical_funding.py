"""Recover funding ownership by reversing complete fills from a proven position."""
from datetime import timedelta
from decimal import Decimal
from itertools import groupby
import re

from .derived_attribution import derive_income
from .economic_ledger import coverage_gaps
from .position_replay import replay_position


def recover(event, anchor, fills, owners, cursors):
    account, symbol, stamp = event['account_id'], event['symbol'], event['event_time_utc']
    stop = anchor['flat_at']
    if (anchor['account_id'], anchor['symbol']) != (account, symbol) or anchor['exchange_qty'] != 0 or stop <= stamp:
        return None, 'invalid_flat_anchor'
    return _recover(event, stop, Decimal(0), fills, owners, cursors)


def recover_from_checkpoint(event, stop, quantity, proof, fills, owners, cursors):
    """Recover from a signed one-way position with proof of no post-cutoff fills."""
    cutoff = int(stop.timestamp() * 1000)
    if (stop <= event["event_time_utc"] or not quantity.is_finite() or not quantity
            or proof.get("cutoff_ms") != cutoff or proof.get("position_side") != "BOTH"
            or proof.get("gap_trade_count") != 0
            or not isinstance(proof.get("snapshot_observed_ms"), int)
            or proof["snapshot_observed_ms"] <= cutoff
            or not re.fullmatch(r"[a-f0-9]{64}", str(proof.get("position_payload_sha256")))):
        return None, "invalid_signed_position_checkpoint"
    return _recover(event, stop, quantity, fills, owners, cursors, proof=proof)


def _recover(event, stop, final_qty, fills, owners, cursors, *, proof=None):
    account, symbol, stamp = event["account_id"], event["symbol"], event["event_time_utc"]
    scoped = sorted((f for f in fills if (f['account_id'], f['symbol']) == (account, symbol)
                     and f['trade_time_utc'] < stop), key=lambda f: f['trade_time_utc'], reverse=True)
    qty = final_qty
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
            baseline = dict(account_id=account, symbol=symbol, flat_at=start, exchange_qty=Decimal(0))
            replay = replay_position(baseline, window, owners, stop, final_qty)
            row, reason = derive_income(event, {}, owners, replay)
            if row:
                row['evidence']['fill_orders'] = [[str(f['trade_id']), str(f['order_id'])] for f in window]
                if proof is None:
                    row['evidence'].update(flat_anchor=stop.isoformat(), reconstruction='reverse_from_flat')
                else:
                    row['evidence'].update(reconstruction='reverse_from_signed_position', checkpoint=proof)
            return row, reason
    return None, 'earlier_flat_boundary_missing'
