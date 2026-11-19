"""Prove exact HRS take-profit lots from one-way executions and signed flat state."""

from decimal import Decimal
import hashlib
import json

from ai_tuner.monitor.trial_risk_evidence import stressed_net
from shared.entry_fill_evidence import verified_fill


_DAY_MS = 86_400_000
_ANCHOR = """
SELECT id,payload FROM trading.entry_activity_source_fact
WHERE account_id=$1 AND kind='flat_position' AND payload->>'symbol'=$2
  AND (payload->>'at_ms')::bigint>$3 AND (payload->>'at_ms')::bigint<=$4
ORDER BY (payload->>'at_ms')::bigint,id LIMIT 1
"""
_OWNER = """
SELECT EXISTS (
  SELECT 1
  FROM trading.order_strategy_evidence o
  JOIN trading.entry_activity_source_fact i ON i.account_id=o.account_id
      AND i.kind IN ('intent','legacy_intent') AND i.payload->>'client_order_id'=$4
  WHERE (o.account_id,o.symbol,o.order_id)=($1,$2,$3) AND o.strategy='hrs'
    AND o.source_kind='trade_record' AND i.payload->>'strategy_id'='hrs'
    AND i.payload->>'symbol'=o.symbol AND i.payload->'geometry'->>'signal_id' ~ '^[1-9][0-9]*$'
    AND (i.payload->>'quantity')::numeric=$5 AND (i.payload->>'created_at')::timestamptz
        <= to_timestamp($6::bigint/1000.0)
    AND NOT EXISTS (
      SELECT 1 FROM trading.entry_activity_source_fact x
      WHERE x.account_id=o.account_id AND x.kind IN ('intent','legacy_intent')
        AND x.payload->>'client_order_id'=$4 AND
          (x.payload->>'strategy_id' IS DISTINCT FROM 'hrs'
           OR x.payload->>'symbol' IS DISTINCT FROM o.symbol
           OR (x.payload->>'quantity')::numeric IS DISTINCT FROM $5)
    )
) AS owned
"""


def _amount(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('invalid economic amount')
    return number


def _history_sections(history, link, start_ms, flat_end_ms):
    if (link['strategy'] != 'hrs' or link['kind'] not in ('TAKE_PROFIT', 'TAKE_PROFIT_MARKET')
            or not isinstance(history, list) or not 2 <= len(history) < 1000):
        raise ValueError('complete HRS history unavailable')
    seen = set()
    for row in history:
        identity, stamp = row['id'], row['time']
        if (type(identity) is not int or identity <= 0 or identity in seen or type(stamp) is not int
                or not start_ms <= stamp < flat_end_ms or row['symbol'] != link['symbol']
                or row['positionSide'] != 'BOTH' or row['side'] not in ('BUY', 'SELL')
                or type(row['orderId']) is not int or row['orderId'] <= 0):
            raise ValueError('signed history scope mismatch')
        seen.add(identity)
    ordered = sorted(history, key=lambda row: (row['time'], row['id']))
    count = len(link['trade_times_ms'])
    if not 0 < count < len(ordered):
        raise ValueError('entry missing from history')
    entry_history, exits = ordered[:-count], ordered[-count:]
    if ({str(row['id']): row['time'] for row in exits} != link['trade_times_ms']
            or any(str(row['orderId']) != link['order_id'] for row in exits)):
        raise ValueError('protective exit is not the last signed order before flat')
    entry_id = entry_history[-1]['orderId']
    opening = [row for row in entry_history if row['orderId'] == entry_id]
    if opening != entry_history[-len(opening):] or opening[-1]['time'] >= exits[0]['time']:
        raise ValueError('opening order is not contiguous before exit')
    return opening, exits


def verify_closed_lot(history, link, order, algo_created_ms, start_ms, flat_end_ms):
    opening, exits = _history_sections(history, link, start_ms, flat_end_ms)
    entry_id, entry_side = opening[0]['orderId'], opening[0]['side']
    if (order.get('reduceOnly') is not False or order.get('positionSide') != 'BOTH'
            or order.get('closePosition', False) is not False or not order.get('clientOrderId')
            or type(algo_created_ms) is not int
            or not opening[-1]['time'] <= algo_created_ms < exits[0]['time']):
        raise ValueError('opening order or conditional placement differs')
    quantity, _ = verified_fill(dict(symbol=link['symbol'], side=entry_side, order_id=entry_id), order, opening)
    if any(row['side'] == entry_side or _amount(row['qty']) <= 0 for row in exits):
        raise ValueError('exit contradicts opening direction')
    if quantity != sum((_amount(row['qty']) for row in exits), Decimal(0)):
        raise ValueError('entry and exit quantity do not conserve')

    pnl, fees, turnover = Decimal(0), Decimal(0), Decimal(0)
    for row in opening + exits:
        realized, fee, price = (_amount(row[key]) for key in ('realizedPnl', 'commission', 'price'))
        if (row['commissionAsset'] != 'USDT' or fee < 0 or price <= 0
                or (str(row['orderId']) == str(entry_id) and realized != 0)):
            raise ValueError('execution economics unavailable')
        pnl += realized
        fees += fee
        turnover += _amount(row['qty']) * price
    net = pnl - fees
    return dict(entry_order_id=str(entry_id), entry_trade_times_ms={
                    str(row['id']): row['time'] for row in opening},
                position_quantity=str(quantity), realized_pnl_usdt=str(pnl), commission_usdt=str(fees),
                turnover_usdt=str(turnover), execution_net_usdt=str(net),
                execution_stressed_net_usdt=str(stressed_net(net, fees, turnover)),
                economics_scope='closed_lot_execution_before_funding')


def _flat_bounds(payload, symbol, after_ms, limit_ms):
    raw = payload['raw_payload']
    left, right = payload['at_ms'], payload['read_until_ms']
    source = payload['source']
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (payload['symbol'] != symbol or type(left) is not int or type(right) is not int
            or not after_ms < left <= limit_ms or not 0 < right-left <= 10_000
            or payload['payload_sha256'] != digest or len(payload['credential_fingerprint']) != 64):
        raise ValueError('flat anchor provenance differs')
    explicit = (source == 'signed_pm_position_risk_v1' and raw.get('symbol') == symbol
                and raw.get('positionSide') == 'BOTH' and _amount(raw['positionAmt']) == 0)
    absent = (source == 'signed_pm_dual_inventory_v1' and
              raw == {'position_risk': [], 'um_account_positions': [], 'dual_side_position': False})
    if not (explicit or absent):
        raise ValueError('signed flat anchor unavailable')
    return right


async def read_closed_lot(db, exchange, account, link, algo, exit_trades):
    """Use an append-only flat anchor and a complete signed page ending at that anchor."""
    latest_exit = max(link['trade_times_ms'].values())
    anchor_limit = latest_exit + _DAY_MS
    anchors = await db.fetch_all(_ANCHOR, account, link['symbol'], latest_exit, anchor_limit)
    if not anchors:
        raise ValueError('no signed flat anchor within a day after exit')
    anchor = anchors[0]
    payload = anchor['payload']
    if isinstance(payload, str):
        payload = json.loads(payload)
    right = _flat_bounds(payload, link['symbol'], latest_exit, anchor_limit)
    start = min(link['trade_times_ms'].values()) - 4*_DAY_MS
    # Use the raw signed response so a malformed page never appears as an empty one.
    history = await exchange._request('GET', '/papi/v1/um/userTrades', {
        'symbol': link['symbol'], 'startTime': start, 'endTime': right, 'limit': 1000,
    }, signed=True)
    opening, exits = _history_sections(history, link, start, right)
    if {row['id']: row for row in exits} != {row['id']: row for row in exit_trades}:
        raise ValueError('signed exit responses disagree')
    order = await exchange.get_order(link['symbol'], opening[0]['orderId'])
    lot = verify_closed_lot(history, link, order, algo.get('createTime'), start, right)
    owners = await db.fetch_all(_OWNER, account, link['symbol'], int(lot['entry_order_id']),
                                order['clientOrderId'], Decimal(lot['position_quantity']), opening[0]['time'])
    if not owners or owners[0]['owned'] is not True:
        raise ValueError('canonical HRS opening identity unavailable')
    lot.update(flat_fact_id=anchor['id'], signed_history_start_ms=start, signed_history_end_ms=right,
               proof='hrs_tp_closed_lot_v1')
    return lot
