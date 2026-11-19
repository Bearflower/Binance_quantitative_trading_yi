"""Account-bound, append-only evidence of real entry fills.

Collector configuration: use a ReadOnlyBinanceClient constructed from the selected
PM account's credentials and set ``entry_activity_account_id`` on that instance.
Only signed GET /papi/v1/um/order responses enter the observation stream. Nothing
here places orders or infers inactivity from missing orders.

Completeness source contract (not produced by today's collector): an administrator
may append a ``protocol_window`` fact after verifying ALL entry paths persist an
immutable intent before submitting. Its payload must identify capture_boundary_id,
start_ms/end_ms, strategy_ids, source_id, reviewed_by, symbols (the complete entry
symbol universe for that interval), symbol_universe_complete=True, and
all_entry_paths_before_submit=True. The window cannot precede capture installation.
For one-way orders append signed ``flat_position`` facts with symbol, at_ms,
raw_payload (symbol/positionAmt/positionSide), payload_sha256 and credential_fingerprint.
Full userTrades coverage from that flat observation proves each fill's economic
role. A reservation or an unverified HRS signal alone is never such a proof.
"""
from __future__ import annotations

from collections import defaultdict
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
import json
import uuid

from shared.readonly_client import ReadOnlyBinanceClient

STRATEGIES = frozenset(('btc_eth', 'btc_eth_aggressive', 'hrs', 'new_coin'))
SOURCE = 'signed_pm_get_order_v1'
_TERMINAL = frozenset(('FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'))
_ORDER_FIELDS = frozenset(('clientOrderId', 'orderId', 'symbol', 'side', 'status',
                           'origQty', 'executedQty', 'reduceOnly', 'positionSide',
                           'time', 'updateTime', 'type', 'origType', 'closePosition'))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _obj(value):
    return json.loads(value) if isinstance(value, str) else value


def _ms(value):
    if isinstance(value, str):
        # PostgreSQL JSON timestamps omit trailing zeros. Python 3.10's
        # fromisoformat accepts only three or six digits after the seconds.
        value = re.sub(r'(?<=\d{2}:\d{2}:\d{2}\.)[0-9]{1,6}(?=Z|[+-]|$)',
                       lambda match: match[0].ljust(6, '0'), value)
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('timezone-aware evidence required')
    return int(value.timestamp() * 1000)


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('invalid quantity')
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('invalid quantity')
    return number


def _bool(value):
    if value is False or value == 'false':
        return False
    if value is True or value == 'true':
        return True
    return None


def _valid_order(row):
    raw = _obj(row['raw_payload'])
    if (row.get('source') != SOURCE or row.get('outcome') != 'ok'
            or row.get('payload_sha256') != _digest(raw)
            or len(row.get('credential_fingerprint', '')) != 64):
        raise ValueError('invalid authenticated order')
    for field, original in (('client_order_id', 'clientOrderId'), ('order_id', 'orderId'),
                            ('symbol', 'symbol'), ('side', 'side'), ('status', 'status')):
        if str(row.get(field)) != str(raw.get(original)):
            raise ValueError('signed identity mismatch')
    for field, original in (('orig_qty', 'origQty'), ('executed_qty', 'executedQty')):
        if _number(row.get(field)) != _number(raw.get(original)):
            raise ValueError('signed quantity mismatch')
    if (_number(raw['origQty']) <= 0 or _number(raw['executedQty']) < 0
            or _number(raw['executedQty']) > _number(raw['origQty'])
            or raw['side'] not in ('BUY', 'SELL') or int(raw['orderId']) <= 0
            or raw['status'] not in _TERMINAL.union(('NEW', 'PARTIALLY_FILLED', 'PENDING_CANCEL'))
            or (raw['status'] == 'FILLED' and _number(raw['executedQty']) != _number(raw['origQty']))):
        raise ValueError('invalid order values')
    _ms(row['observed_at'])
    return raw


def _covered(cursors, account, symbol, start, end):
    covered = start
    intervals = sorted((int(c['window_start_ms']), int(c['covered_until_ms']))
                       for c in cursors if c.get('account_id') == account
                       and c.get('source') == 'userTrades' and c.get('symbol') == symbol
                       and c.get('page_complete') is True and c.get('request_status') == 'ok'
                       and c.get('covered_until_ms') is not None)
    for left, right in intervals:
        if left <= covered:
            covered = max(covered, right)
    return covered >= end


def _entry_role(raw, fill, fills, cursors, facts, account, end):
    """Return True only for exposure-increasing fills, False for verified closes."""
    if _bool(raw.get('reduceOnly')) is not False or _bool(raw.get('closePosition', False)) is not False:
        return None
    side, position = raw['side'], raw.get('positionSide')
    if position in ('LONG', 'SHORT'):
        return (position, side) in (('LONG', 'BUY'), ('SHORT', 'SELL'))
    if position != 'BOTH':
        return None
    target_ms = _ms(fill['trade_time_utc'])
    scoped_fills = [(f, _ms(f['trade_time_utc'])) for f in fills if f['symbol'] == fill['symbol']]
    anchors = []
    for fact in facts:
        if fact.get('account_id') != account or fact.get('kind') != 'flat_position':
            continue
        payload = _obj(fact['payload'])
        original = payload.get('raw_payload', {})
        left = payload.get('at_ms')
        right = payload.get('read_until_ms', left)
        explicit_zero = (original.get('symbol') == fill['symbol']
                         and original.get('positionSide') == 'BOTH'
                         and original.get('positionAmt') is not None
                         and _number(original['positionAmt']) == 0)
        confirmed_absence = (payload.get('source') == 'signed_pm_dual_inventory_v1'
                             and original == {'position_risk': [], 'um_account_positions': [],
                                              'dual_side_position': False})
        if (payload.get('symbol') == fill['symbol']
                and type(left) is int and type(right) is int and 0 <= left <= right < target_ms
                and (left == right or payload.get('source') in
                     ('signed_pm_position_risk_v1', 'signed_pm_dual_inventory_v1'))
                and _covered(cursors, account, fill['symbol'], left, end)
                and not any(left <= stamp <= right for _, stamp in scoped_fills)
                and payload.get('payload_sha256') == _digest(original)
                and len(payload.get('credential_fingerprint', '')) == 64
                and (explicit_zero or confirmed_absence)):
            anchors.append(right)
    if not anchors:
        return None
    start = max(anchors)
    qty = Decimal(0)
    ordered = sorted((f for f, stamp in scoped_fills if start < stamp < end),
                     key=lambda f: (_ms(f['trade_time_utc']), int(f['trade_id'])))
    for current in ordered:
        amount = _number(current['qty'])
        if amount <= 0 or current.get('side') not in ('BUY', 'SELL'):
            return None
        delta = amount if current['side'] == 'BUY' else -amount
        if str(current['trade_id']) == str(fill['trade_id']):
            if qty == 0 or (qty > 0) == (delta > 0):
                return True
            # A reversal combines an exit and entry and requires finer proof.
            return False if abs(delta) <= abs(qty) else None
        qty += delta
    return None


def project_activity_evidence(reservations, observations, fills, cursors, facts,
                              account_id, start_ms, end_ms, strategy_ids, *, audit_healthy=True):
    """Project exact ownership and real fill timestamps into a positive subset.

    ``None`` means no provable count, including unknown inactivity. A positive
    integer can be a verified subset while coverage_complete remains false.
    """
    ids = tuple(dict.fromkeys(strategy_ids))
    reasons = {sid: [] for sid in ids}
    positive = {sid: set() for sid in ids}
    filled_orders = {sid: set() for sid in ids}

    def gap(reason, owners=ids):
        for sid in owners:
            if sid in reasons and reason not in reasons[sid]:
                reasons[sid].append(reason)

    if not (type(start_ms) is int and type(end_ms) is int and 0 <= start_ms < end_ms):
        gap('invalid_activity_window')
    if account_id != 'default':
        gap('unsupported_account')
    if any(sid not in STRATEGIES for sid in ids):
        gap('unsupported_strategy', [sid for sid in ids if sid not in STRATEGIES])
    if not audit_healthy:
        gap('activity_audit_unhealthy')
    if any(reasons.values()):
        return {sid: dict(account_id=account_id, start_ms=start_ms, end_ms=end_ms,
                          owned_entry_fills=None, verified_filled_entry_orders=None, coverage_complete=False,
                          source_id='entry-activity:unavailable', reasons=reasons[sid]) for sid in ids}
    reservations, observations, fills, cursors, facts = map(list, (reservations, observations, fills, cursors, facts))
    # Captured intent facts retain released/deleted intents. Legacy snapshots help
    # exact positive attribution but do not establish historical protocol coverage.
    for fact in facts:
        if fact.get('account_id') == account_id and fact.get('kind') in ('intent', 'legacy_intent'):
            reservations.append(_obj(fact['payload']))
    expected_clients = {str(r['client_order_id']) for r in reservations if r.get('account_id') == account_id
                        and (r.get('created_at') is None or _ms(r['created_at']) < end_ms)}
    batches = []
    successful_clients = set()
    for fact in facts:
        if fact.get('account_id') != account_id or fact.get('kind') != 'collection_batch':
            continue
        batch = _obj(fact['payload'])
        if (batch.get('source') == SOURCE
                and batch.get('finished_ms', 0) >= end_ms
                and batch.get('candidate_sha256')
                and isinstance(batch.get('candidate_count'), int)
                and len(batch.get('credential_fingerprint', '')) == 64):
            batches.append(batch)
            successful_clients.update(batch.get('succeeded_client_order_ids', []))
    for observation in observations:
        try:
            original = _valid_order(observation)
            if (observation.get('account_id') == account_id and original['status'] in _TERMINAL
                    and (_ms(observation['observed_at']) < start_ms
                         or (type(original.get('updateTime')) is int and 0 < original['updateTime'] < start_ms))):
                successful_clients.add(observation['client_order_id'])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            pass
    if not batches or not expected_clients.issubset(successful_clients):
        gap('collection_watermark_missing')
    boundary = {str(f['id']): f for f in facts
                if f.get('account_id') == account_id and f.get('kind') == 'capture_boundary'}
    for sid in ids:
        windows = []
        required_cursors = []
        symbol_intervals = []
        for fact in facts:
            if fact.get('account_id') != account_id or fact.get('kind') != 'protocol_window':
                continue
            p = _obj(fact['payload'])
            b = boundary.get(str(p.get('capture_boundary_id')))
            if (b and fact.get('created_by') not in (None, 'trading_user')
                    and p.get('all_entry_paths_before_submit') is True
                    and sid in p.get('strategy_ids', []) and p.get('reviewed_by') and p.get('source_id')
                    and type(p.get('start_ms')) is int and type(p.get('end_ms')) is int
                    and _ms(b['observed_at']) <= p['start_ms'] < p['end_ms']):
                left, right = max(start_ms, p['start_ms']), min(end_ms, p['end_ms'])
                if left >= right:
                    continue
                windows.append((left, right))
                symbols = p.get('symbols')
                if (p.get('symbol_universe_complete') is True and isinstance(symbols, list)
                        and symbols and all(isinstance(symbol, str) and symbol.strip() for symbol in symbols)):
                    symbol_intervals.append((left, right))
                    required_cursors.extend((symbol, left, right) for symbol in symbols)

        def covers_window(intervals):
            covered = start_ms
            for left, right in sorted(intervals):
                if left > covered:
                    return False
                covered = max(covered, right)
            return covered >= end_ms

        if not covers_window(windows):
            gap('missing_protocol_cutover', (sid,))
        elif not covers_window(symbol_intervals):
            gap('entry_symbol_universe_missing', (sid,))
        elif not all(_covered(cursors, account_id, symbol, left, right)
                     for symbol, left, right in required_cursors):
            gap('user_trades_coverage_incomplete', (sid,))
    intents = defaultdict(list)
    for r in reservations:
        if r.get('account_id') == account_id:
            intents[str(r['client_order_id'])].append(r)
    unique_fills, bad_symbols = {}, set()
    for f in fills:
        if f.get('account_id') != account_id:
            continue
        key = (account_id, str(f['symbol']), str(f['trade_id']))
        if key in unique_fills and _json(unique_fills[key]) != _json(f):
            bad_symbols.add(f['symbol'])
            gap('fill_identity_conflict')
        unique_fills[key] = f
    fills = list(unique_fills.values())
    clients_by_order = defaultdict(set)
    for observation in observations:
        client_id = str(observation.get('client_order_id'))
        if observation.get('account_id') != account_id or client_id not in intents:
            continue
        try:
            order = _valid_order(observation)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        clients_by_order[order['symbol'], str(order['orderId'])].add(client_id)
    seen_orders = {}
    for client_id, claims in intents.items():
        owners = {r.get('strategy_id') for r in claims}
        symbols = {r.get('symbol') for r in claims}
        selected = owners.intersection(ids)
        if not selected:
            continue
        if len(owners) != 1 or len(symbols) != 1:
            gap('intent_owner_conflict', selected)
            continue
        sid, symbol = next(iter(owners)), next(iter(symbols))
        if client_id not in expected_clients:
            continue
        rows = [o for o in observations if o.get('account_id') == account_id
                and str(o.get('client_order_id')) == client_id]
        if not rows:
            gap('order_observation_missing', (sid,))
            continue
        valid = []
        latest_success = max(((_ms(o['observed_at']), o.get('id', 0)) for o in rows
                              if o.get('outcome') == 'ok'), default=(-1, -1))
        for o in rows:
            if o.get('outcome') != 'ok':
                if (o.get('outcome') == 'order_identity_conflict'
                        or (_ms(o['observed_at']), o.get('id', 0)) >= latest_success):
                    gap(str(o.get('outcome') or 'order_observation_unknown'), (sid,))
                continue
            try:
                valid.append((o, _valid_order(o)))
            except (ValueError, TypeError, KeyError, InvalidOperation):
                gap('invalid_order_observation', (sid,))
        identities = {(raw['symbol'], str(raw['orderId'])) for _, raw in valid}
        if len(identities) != 1 or (identities and next(iter(identities))[0] != symbol):
            gap('order_identity_conflict' if len(identities) > 1 else 'order_observation_missing', (sid,))
            continue
        row, raw = max(valid, key=lambda pair: (_ms(pair[0]['observed_at']), pair[0].get('id', 0)))
        order_key = (symbol, str(raw['orderId']))
        if len(clients_by_order[order_key]) != 1:
            gap('order_owner_conflict', (sid,))
            continue
        if order_key in seen_orders and seen_orders[order_key] != sid:
            gap('order_owner_conflict', (sid, seen_orders[order_key]))
            positive[sid].clear()
            positive[seen_orders[order_key]].clear()
            continue
        seen_orders[order_key] = sid
        if _ms(row['observed_at']) < end_ms and raw['status'] not in _TERMINAL:
            gap('entry_order_observation_stale', (sid,))
        if not _covered(cursors, account_id, symbol, start_ms, end_ms):
            gap('user_trades_coverage_incomplete', (sid,))
        terminal_at = raw.get('updateTime')
        if raw['status'] in _TERMINAL and (
                _ms(row['observed_at']) < start_ms
                or (type(terminal_at) is int and 0 < terminal_at < start_ms)):
            continue
        if symbol in bad_symbols:
            continue
        matched = [f for f in fills if f['symbol'] == symbol and str(f.get('order_id')) == str(raw['orderId'])]
        try:
            if sum((_number(f['qty']) for f in matched), Decimal(0)) != _number(raw['executedQty']):
                gap('order_fill_quantity_mismatch', (sid,))
                continue
            if any(_number(r['quantity']) != _number(raw['origQty']) for r in claims if 'quantity' in r):
                gap('intent_order_quantity_mismatch', (sid,))
                continue
            directions = set()
            for claim in claims:
                geometry = _obj(claim.get('geometry') or {})
                signal = geometry.get('signal') or {}
                direction = str(geometry.get('side') or geometry.get('direction')
                                or signal.get('direction') or signal.get('side') or '').upper()
                if direction in ('LONG', 'BUY', 'SHORT', 'SELL'):
                    directions.add('BUY' if direction in ('LONG', 'BUY') else 'SELL')
            if directions and directions != {raw['side']}:
                gap('intent_order_direction_conflict', (sid,))
                continue
            if any(f.get('side') != raw['side'] or _number(f['qty']) <= 0 for f in matched):
                gap('order_fill_direction_conflict', (sid,))
                continue
            for f in matched:
                if not start_ms <= _ms(f['trade_time_utc']) < end_ms:
                    continue
                role = _entry_role(raw, f, fills, cursors, facts, account_id, end_ms)
                if role is True:
                    positive[sid].add((account_id, symbol, str(f['trade_id'])))
                    filled_orders[sid].add(order_key)
                else:
                    gap('entry_role_unverified' if role is None else 'entry_direction_conflict', (sid,))
        except (ValueError, TypeError, KeyError, InvalidOperation):
            gap('invalid_fill_evidence', (sid,))
    for fill in fills:
        if (start_ms <= _ms(fill['trade_time_utc']) < end_ms
                and (fill['symbol'], str(fill.get('order_id'))) not in seen_orders):
            # An unowned fill may close a known position, but only a fully covered
            # flat replay can prove that it did not create exposure.
            raw = dict(side=fill.get('side'), positionSide='BOTH', reduceOnly=False)
            if _entry_role(raw, fill, fills, cursors, facts, account_id, end_ms) is not False:
                gap('unowned_fill_in_window')
    # Zero is only available with complete protocol + observations + fill coverage.
    source_id = 'entry-activity:sha256:' + _digest([account_id, start_ms, end_ms, reservations,
                                                 observations, fills, cursors, facts])
    return {sid: dict(account_id=account_id, start_ms=start_ms, end_ms=end_ms,
                      owned_entry_fills=(len(positive[sid]) if positive[sid] or not reasons[sid] else None),
                      verified_filled_entry_orders=(len(filled_orders[sid]) if filled_orders[sid]
                                                    or not reasons[sid] else None),
                      coverage_complete=not reasons[sid], source_id=source_id, reasons=reasons[sid]) for sid in ids}


_RESERVATIONS = """SELECT * FROM trading.entry_risk_reservation WHERE account_id=$1
ORDER BY (SELECT max(o.observed_at) FROM trading.entry_order_observation o
 WHERE o.account_id=$1 AND o.client_order_id=entry_risk_reservation.client_order_id) ASC NULLS FIRST,
 created_at,client_order_id"""
_OBSERVATIONS = 'SELECT * FROM trading.entry_order_observation WHERE account_id=$1 ORDER BY id'
_FACTS = 'SELECT * FROM trading.entry_activity_source_fact WHERE account_id=$1 ORDER BY id'
_INSERT_FACT = 'INSERT INTO trading.entry_activity_source_fact(account_id,kind,payload) VALUES($1,$2,$3::jsonb)'
_FLAT_CANDIDATES = """SELECT symbol FROM trading.entry_risk_reservation
WHERE account_id=$1 AND strategy_id IN ('hrs','btc_eth_aggressive')
GROUP BY symbol ORDER BY max(created_at) DESC,symbol LIMIT 30"""
_RECENT_FLAT = """SELECT DISTINCT payload->>'symbol' AS symbol FROM trading.entry_activity_source_fact
WHERE account_id=$1 AND kind='flat_position' AND observed_at >= now()-interval '1 hour'"""
_INSERT_ORDER = """INSERT INTO trading.entry_order_observation
(account_id,client_order_id,symbol,order_id,side,status,orig_qty,executed_qty,outcome,
 raw_payload,payload_sha256,credential_fingerprint,source,batch_id)
VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13,$14)"""


def _bound_pm(exchange, account_id):
    return (account_id == 'default' and isinstance(exchange, ReadOnlyBinanceClient)
            and getattr(exchange, 'entry_activity_account_id', None) == account_id
            and exchange.use_unified_account is True and not exchange.testnet)


async def collect_flat_anchors(db, exchange, account_id):
    """Append bounded signed flat evidence for replay, at most hourly per symbol."""
    if not _bound_pm(exchange, account_id):
        raise ValueError('Explicit read-only production PM account binding required')
    candidates = {row['symbol'] for row in await db.fetch_all(_FLAT_CANDIDATES, account_id)}
    recent = {row['symbol'] for row in await db.fetch_all(_RECENT_FLAT, account_id)}
    before = await exchange._request('GET', '/fapi/v1/time', signed=False)
    positions = await exchange.get_position()
    account_positions, one_way = None, False
    if candidates - recent:
        account_positions = await exchange.get_um_account_positions()
        mode = await exchange._request('GET', '/papi/v1/um/positionSide/dual')
        one_way = isinstance(mode, dict) and mode.get('dualSidePosition') is False
    after = await exchange._request('GET', '/fapi/v1/time', signed=False)
    left, right = before['serverTime'], after['serverTime']
    if (type(left) is not int or type(right) is not int or left <= 0
            or not 0 < right - left <= 10000 or not isinstance(positions, list)):
        raise ValueError('Unverified flat observation interval')
    by_symbol = defaultdict(list)
    for row in positions:
        if not isinstance(row, dict) or not isinstance(row.get('symbol'), str):
            raise ValueError('Invalid signed position response')
        by_symbol[row['symbol']].append(row)
    fingerprint = hashlib.sha256(exchange._api_key.encode()).hexdigest()
    count = 0
    account_symbols = None
    if isinstance(account_positions, list) and all(
            isinstance(row, dict) and isinstance(row.get('symbol'), str) for row in account_positions):
        account_symbols = {row['symbol'] for row in account_positions}
    for symbol in sorted((set(by_symbol) | candidates) - recent)[:100]:
        rows = by_symbol.get(symbol, [])
        if (len(rows) == 1 and rows[0].get('positionSide') == 'BOTH'
                and _number(rows[0].get('positionAmt')) == 0):
            raw = {key: rows[0][key] for key in ('symbol', 'positionSide', 'positionAmt')}
            source = 'signed_pm_position_risk_v1'
        elif (not rows and symbol in candidates and one_way and account_symbols is not None
              and symbol not in account_symbols):
            raw = {'position_risk': [], 'um_account_positions': [], 'dual_side_position': False}
            source = 'signed_pm_dual_inventory_v1'
        else:
            continue
        await db.execute(_INSERT_FACT, account_id, 'flat_position', _json(dict(
            symbol=symbol, at_ms=left, read_until_ms=right, raw_payload=raw,
            source=source, payload_sha256=_digest(raw),
            credential_fingerprint=fingerprint)))
        count += 1
    return count


async def collect_entry_activity(db, exchange, account_id, *, max_requests=100):
    """Bounded signed reads, append results and batch watermarks; never a cutover.

    Every reservation, including old released ones, remains eligible. Oldest
    observations are queried first so successive bounded runs make progress.
    No request parameters, headers, keys or exception messages are persisted.
    """
    if not _bound_pm(exchange, account_id):
        raise ValueError('Explicit read-only production PM account binding required')
    if type(max_requests) is not int or max_requests <= 0:
        raise ValueError('max_requests must be a positive integer')
    fingerprint = hashlib.sha256(exchange._api_key.encode()).hexdigest()
    reservations = [dict(r) for r in await db.fetch_all(_RESERVATIONS, account_id)]
    prior = [dict(r) for r in await db.fetch_all(_OBSERVATIONS, account_id)]
    facts = [dict(r) for r in await db.fetch_all(_FACTS, account_id)]
    seen = {r['client_order_id'] for r in reservations}
    for fact in facts:
        if fact.get('account_id') == account_id and fact.get('kind') in ('intent', 'legacy_intent'):
            archived = _obj(fact['payload'])
            if archived.get('account_id') == account_id and archived['client_order_id'] not in seen:
                reservations.append(archived)
                seen.add(archived['client_order_id'])
    last_observed = {}
    for old in prior:
        if old.get('observed_at') is not None:
            last_observed[old['client_order_id']] = max(last_observed.get(old['client_order_id'], 0), _ms(old['observed_at']))
    reservations.sort(key=lambda r: (last_observed.get(r['client_order_id'], 0), r['client_order_id']))
    batch = str(uuid.uuid4())
    start = int(datetime.now(timezone.utc).timestamp() * 1000)
    await db.execute(_INSERT_FACT, account_id, 'collection_boundary', _json(dict(
        batch_id=batch, started_ms=start, candidate_count=len(reservations),
        candidate_sha256=_digest(sorted(r['client_order_id'] for r in reservations)),
        source=SOURCE, credential_fingerprint=fingerprint)))
    gaps, observed, succeeded, pending = defaultdict(int), 0, [], []
    for reservation in reservations:
        terminal = False
        matching = [o for o in prior if o.get('client_order_id') == reservation['client_order_id']]
        if matching:
            latest = max(matching, key=lambda o: (_ms(o['observed_at']), o.get('id', 0)))
            try:
                original = _valid_order(latest)
                terminal = (original['status'] in _TERMINAL and original['symbol'] == reservation['symbol'])
            except (KeyError, TypeError, ValueError, InvalidOperation):
                pass
        if terminal:
            succeeded.append(reservation['client_order_id'])
        else:
            pending.append(reservation)
    for reservation in pending[:max_requests]:
        client_id, symbol = reservation['client_order_id'], reservation['symbol']
        raw, outcome = {}, 'invalid_order_response'
        try:
            response = await exchange.get_order_by_client_id(symbol, client_id)
            if response is None or (isinstance(response, dict) and response.get('code') in (-2013, -2011)):
                outcome = 'order_not_found'
            elif isinstance(response, dict) and response:
                raw = {key: value for key, value in response.items() if key in _ORDER_FIELDS}
                probe = dict(account_id=account_id, client_order_id=client_id, symbol=symbol,
                    order_id=str(raw.get('orderId')), side=raw.get('side'), status=raw.get('status'),
                    orig_qty=raw.get('origQty'), executed_qty=raw.get('executedQty'), raw_payload=raw,
                    payload_sha256=_digest(raw), credential_fingerprint=fingerprint, source=SOURCE,
                    outcome='ok', observed_at=datetime.now(timezone.utc))
                _valid_order(probe)
                if any(p.get('outcome') == 'ok' and p['client_order_id'] == client_id
                       and (p.get('symbol'), str(p.get('order_id'))) != (symbol, str(raw['orderId'])) for p in prior):
                    outcome = 'order_identity_conflict'
                else:
                    outcome = 'ok'
        except (TimeoutError, asyncio.TimeoutError):
            outcome = 'order_lookup_timeout'
        except (ValueError, TypeError, KeyError, InvalidOperation):
            outcome = 'invalid_order_response'
        except Exception:
            outcome = 'order_lookup_unknown'
        if outcome != 'ok':
            gaps[outcome] += 1
            raw = {}  # invalid/untrusted responses never become signed identity facts
        else:
            observed += 1
            succeeded.append(client_id)
        await db.execute(_INSERT_ORDER, account_id, client_id, symbol,
            str(raw['orderId']) if raw else None, raw.get('side'), raw.get('status'),
            _number(raw['origQty']) if raw else None, _number(raw['executedQty']) if raw else None,
            outcome, _json(raw), _digest(raw), fingerprint, SOURCE, batch)
    remaining = max(0, len(pending) - max_requests)
    if remaining:
        gaps['request_budget_exhausted'] = remaining
    summary = dict(account_id=account_id, batch_id=batch, started_ms=start, finished_ms=int(datetime.now(timezone.utc).timestamp()*1000),
                   observed=observed, attempted=min(len(pending), max_requests), remaining=remaining,
                   gaps=dict(gaps), batch_complete=not gaps, protocol_complete=False,
                   candidate_count=len(reservations),
                   candidate_sha256=_digest(sorted(r['client_order_id'] for r in reservations)),
                   succeeded_client_order_ids=succeeded,
                   source=SOURCE, credential_fingerprint=fingerprint)
    await db.execute(_INSERT_FACT, account_id, 'collection_batch', _json(summary))
    return summary


async def read_activity_evidence(db, account_id, start_ms, end_ms, strategy_ids):
    """Read immutable evidence only; database failure cannot become zero activity."""
    strategy_ids = tuple(strategy_ids)
    try:
        reservations = [dict(r) for r in await db.fetch_all(_RESERVATIONS, account_id)]
        observations = [dict(r) for r in await db.fetch_all(_OBSERVATIONS, account_id)]
        facts = [dict(r) for r in await db.fetch_all(_FACTS, account_id)]
        # Read coverage first: a later backfill must not retroactively certify a
        # fill snapshot read before that backfill committed.
        cursors = [dict(r) for r in await db.fetch_all(
            'SELECT * FROM trading.execution_fact_cursor WHERE account_id=$1', account_id)]
        fills = [dict(r) for r in await db.fetch_all(
            'SELECT * FROM trading.execution_fact_fill WHERE account_id=$1', account_id)]
        audit = await db.fetch_all("""SELECT count(*)=7 AS healthy FROM pg_trigger t
JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE NOT tgisinternal AND tgenabled='A' AND n.nspname='trading' AND
(tgrelid,tgname,tgtype::int,p.proname) IN (
 ('trading.entry_order_observation'::regclass,'entry_order_observation_immutable',27,'reject_entry_activity_mutation'),
 ('trading.entry_order_observation'::regclass,'entry_order_observation_no_truncate',34,'reject_entry_activity_mutation'),
 ('trading.entry_order_observation'::regclass,'entry_order_identity_guard',7,'guard_entry_order_identity'),
 ('trading.entry_activity_source_fact'::regclass,'entry_activity_source_fact_immutable',27,'reject_entry_activity_mutation'),
 ('trading.entry_activity_source_fact'::regclass,'entry_activity_source_fact_no_truncate',34,'reject_entry_activity_mutation'),
 ('trading.entry_activity_source_fact'::regclass,'entry_activity_source_guard',7,'guard_entry_activity_source'),
 ('trading.entry_risk_reservation'::regclass,'entry_activity_capture_intent',31,'capture_entry_activity_intent'))""")
        healthy = bool(audit) and audit[0].get('healthy') is True
        return project_activity_evidence(reservations, observations, fills, cursors, facts,
                                         account_id, start_ms, end_ms, strategy_ids, audit_healthy=healthy)
    except Exception:
        return {sid: dict(account_id=account_id, start_ms=start_ms, end_ms=end_ms,
                          owned_entry_fills=None, verified_filled_entry_orders=None, coverage_complete=False,
                          source_id='entry-activity:unavailable', reasons=['activity_source_unavailable']) for sid in strategy_ids}
