"""Read-only signal outcomes tied to entry intents and complete signed order windows."""

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from shared.entry_fill_evidence import verified_fill


_AGGRESSIVE = """
SELECT s.id AS signal_id, s.symbol, s.direction,
       s.created_at AT TIME ZONE 'UTC' AS signal_at, s.status,
       COALESCE(s.metadata->>'skip_reason',s.metadata->>'reject_reason') AS reason,
       s.metadata->>'entry_order_id' AS order_id,
       s.metadata->>'_risk_client_id' AS client_order_id,
       (SELECT CASE WHEN count(*)=1 THEN min(r.client_order_id) ELSE '!conflict' END
        FROM trading.entry_risk_reservation r
        WHERE r.account_id=$3 AND r.strategy_id='btc_eth_aggressive' AND r.symbol=s.symbol
          AND r.client_order_id=s.metadata->>'_risk_client_id'
        HAVING count(*)>0) AS intent_client_id, NULL::text AS preflight_receipt
FROM btc_eth_aggressive.trade_signals s WHERE s.created_at >= $1 AND s.created_at < $2
ORDER BY s.created_at,s.id LIMIT 2001
"""
_HRS = """
SELECT s.id AS signal_id,s.symbol,s.direction,s.triggered_at AS signal_at,s.resolution AS status,
       native.reason,NULL::text AS order_id,NULL::text AS client_order_id,
       (SELECT CASE WHEN count(*)=1 THEN min(r.client_order_id) ELSE '!conflict' END
        FROM trading.entry_risk_reservation r
        WHERE r.account_id=$3 AND r.strategy_id='hrs' AND r.symbol=s.symbol
          AND r.geometry->>'signal_id'=s.id::text
        HAVING count(*)>0) AS intent_client_id,
       native.receipt AS preflight_receipt
FROM hrs.hrs_signals s
LEFT JOIN LATERAL (
    SELECT CASE WHEN count(*)=1 THEN min(x.evidence_id) END AS receipt,
           CASE WHEN count(*)=1 THEN min(x.reason) END AS reason
    FROM (
        SELECT e.evidence_id,CASE
          WHEN e.kind='pool_circuit_preflight_denial'
               AND e.payload->>'signal_id'=s.id::text AND e.payload->>'direction'=s.direction
               AND e.payload->>'entry_mode'=s.entry_mode AND e.payload->'is_shadow'='false'::jsonb
               AND e.payload->>'reason'='pool_circuit_breaker' AND s.resolution='pool_circuit_breaker'
            THEN 'pool_circuit_breaker'
          WHEN e.kind='signal_execution_outcome' AND e.payload->'result'='false'::jsonb
               AND e.payload->'signal'->>'signal_id'=s.id::text
               AND e.payload->'signal'->>'direction'=s.direction
               AND e.payload->'signal'->>'entry_mode'=s.entry_mode
            THEN CASE
              WHEN e.payload->'signal'->>'skip_reason'='HRS entry risk policy blocks new positions'
                THEN 'entry_risk_policy'
              WHEN e.payload->'signal'->>'_audit_stage'='entry_risk_budget'
                   AND e.payload->'signal'->>'_audit_denial'='minimum_order_exceeds_risk_budget'
                THEN 'minimum_order_exceeds_risk_budget'
              WHEN e.payload->'signal'->>'_audit_stage'='pre_entry_funding'
                THEN 'funding_stage_did_not_advance'
            END
        END AS reason
        FROM hrs.hrs_decision_evidence e
        WHERE e.kind IN ('pool_circuit_preflight_denial','signal_execution_outcome')
          AND e.symbol=s.symbol AND e.event_time>=s.triggered_at AND e.event_time<$2
          AND e.event_time<s.triggered_at+interval '5 minutes'
    ) x WHERE x.reason IS NOT NULL
) native ON true
WHERE s.triggered_at >= $1 AND s.triggered_at < $2
  AND (s.resolution='executed' OR (s.signal_origin IS DISTINCT FROM 'shadow_scoring'
       AND (s.entry_mode='standard' OR (s.entry_mode='lv_rm' AND s.signal_origin='nonempty_pool'))))
ORDER BY s.triggered_at,s.id LIMIT 2001
"""
_PRE_SUBMIT = frozenset({
    'legacy STOP episode unverified',
    '已进入分批止盈，拒绝加仓',
    '入场风控禁止加仓（当前风控策略仅允许新开仓）',
    '反向信号拒绝加仓',
})
_COOLDOWN = re.compile(r'verified STOP cooldown \([1-9][0-9]*h\)')
_RECEIPT_ID = re.compile(r'[0-9a-f]{64}')
_HISTORICAL_REASONS = {'legacy_pool_circuit_breaker', 'legacy_shadow_displaced_standard'}
_NATIVE_HRS_REASONS = {'entry_risk_policy', 'minimum_order_exceeds_risk_budget',
                       'funding_stage_did_not_advance'}


async def _apply_hrs_review(db, rows, account, start, end):
    path = Path(__file__).with_name('reviewed_hrs_signal_outcomes.json')
    review = json.loads(path.read_text())
    if (review['account_id'] != account or datetime.fromisoformat(review['start']) != start
            or datetime.fromisoformat(review['end']) != end):
        return rows
    facts = {fact['signal_id']: fact for fact in review['decisions']}
    evidence = await db.fetch_all(
        "SELECT evidence_id,symbol,kind FROM hrs.hrs_decision_evidence "
        "WHERE evidence_id=ANY($1::text[]) AND kind='execution_batch'",
        sorted({fact['batch_receipt'] for fact in facts.values()}))
    known = {e['evidence_id'] for e in evidence}
    updated = []
    for row in rows:
        fact = facts.get(row['signal_id'])
        if (fact and not row['status'] and not row['preflight_receipt']
                and row['symbol'] == fact['symbol'] and row['direction'] == fact['direction']
                and row['signal_at'] == datetime.fromisoformat(fact['signal_at'])
                and fact['reason'] in _HISTORICAL_REASONS
                and fact['batch_receipt'] in known):
            row = dict(row, reason=fact['reason'], preflight_receipt=fact['batch_receipt'])
        updated.append(row)
    return updated


async def _signed_history(exchange, symbol, left, right):
    params = dict(symbol=symbol, startTime=left, endTime=right - 1, limit=1000)
    orders = await exchange._request('GET', '/papi/v1/um/allOrders', params, signed=True)
    fills = await exchange._request('GET', '/papi/v1/um/userTrades', params, signed=True)
    for rows, identity in ((orders, 'orderId'), (fills, 'id')):
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise ValueError('incomplete signed page')
        ids = set()
        for item in rows:
            if (item['symbol'] != symbol or type(item[identity]) is not int or item[identity] <= 0
                    or item[identity] in ids or type(item['time']) is not int
                    or not left <= item['time'] < right):
                raise ValueError('signed row outside evidence scope')
            ids.add(item[identity])
    if any(type(order['reduceOnly']) is not bool or order['positionSide'] != 'BOTH'
           or type(order.get('closePosition', False)) is not bool for order in orders):
        raise ValueError('order role unavailable')
    known_orders = {item['orderId'] for item in orders}
    trades = defaultdict(list)
    for fill in fills:
        if (type(fill['orderId']) is not int or fill['orderId'] not in known_orders
                or fill['positionSide'] != 'BOTH'):
            raise ValueError('fill has no signed window order')
        trades[fill['orderId']].append(fill)
    openings = [o for o in orders if not o['reduceOnly'] and not o.get('closePosition', False)]
    return openings, trades, not orders and not fills


def _outcomes(rows, orders, fills, strategy, empty_history):
    by_client = defaultdict(list)
    for order in orders:
        by_client[order['clientOrderId']].append(order)
    matched = {}
    for row in rows:
        client = row['intent_client_id']
        candidates = by_client.get(client, [])
        if row['status'] != 'executed' or len(candidates) != 1:
            continue
        order = candidates[0]
        order_id = order['orderId']
        if strategy != 'hrs' and (row['client_order_id'] != client or row['order_id'] != str(order_id)):
            continue
        side = {'LONG': 'BUY', 'SHORT': 'SELL'}.get(row['direction'].upper())
        try:
            verified_fill(dict(symbol=row['symbol'], side=side, order_id=order_id), order,
                          fills[order_id])
        except (TypeError, ValueError, KeyError):
            continue
        matched[row['signal_id']] = order_id
    owners = Counter(matched.values())
    unexplained = set(o['orderId'] for o in orders) != set(matched.values()) or any(n > 1 for n in owners.values())
    details = {}
    for row in rows:
        signal_id = row['signal_id']
        order_id = matched.get(signal_id)
        reason = row['reason']
        detail = {'signal_id': signal_id, 'symbol': row['symbol'], 'direction': row['direction'],
                  'signal_at': row['signal_at'], 'outcome': 'unverified', 'reason': reason}
        if order_id is not None and owners[order_id] == 1:
            detail.update(outcome='filled', order_id=str(order_id),
                          trade_ids=[str(fill['id']) for fill in fills[order_id]])
        elif not unexplained and not row['client_order_id'] and not row['intent_client_id']:
            receipt = row['preflight_receipt']
            if strategy == 'hrs' and isinstance(receipt, str) and _RECEIPT_ID.fullmatch(receipt):
                if row['status'] == 'pool_circuit_breaker':
                    detail.update(outcome='pre_submit_rejected', reason='pool_circuit_breaker', receipt=receipt)
                elif reason in _NATIVE_HRS_REASONS or (empty_history and reason in _HISTORICAL_REASONS):
                    detail.update(outcome='pre_submit_rejected', receipt=receipt)
            elif strategy != 'hrs' and row['status'] == 'cancelled' and (
                    reason in _PRE_SUBMIT or (isinstance(reason, str) and _COOLDOWN.fullmatch(reason))):
                detail['outcome'] = 'pre_submit_rejected'
            elif not orders:
                detail['outcome'] = 'no_order_reason_unverified'
        details[signal_id] = detail
    return details


async def read_signal_execution(db, exchange, account, strategy, start, end):
    result = dict(verified=False, signals=[], issues=[])
    if (account != 'default' or exchange is None or strategy not in {'hrs', 'btc_eth_aggressive'}
            or not 0 < (end - start).total_seconds() <= 7 * 86400):
        result['issues'].append('unsupported_signal_window')
        return result
    query = _HRS if strategy == 'hrs' else _AGGRESSIVE
    left, right = ((start, end) if strategy == 'hrs' else
                   tuple(t.astimezone(timezone.utc).replace(tzinfo=None) for t in (start, end)))
    rows = await db.fetch_all(query, left, right, account)
    if strategy == 'hrs':
        rows = await _apply_hrs_review(db, rows, account, start, end)
    symbols = defaultdict(list)
    for row in rows:
        symbols[row['symbol']].append(row)
    if len(rows) > 2000 or len(symbols) > 64 or len({row['signal_id'] for row in rows}) != len(rows):
        result['issues'].append('signal_scope_incomplete')
        return result
    limit = asyncio.Semaphore(3)

    async def verify(symbol, scope):
        async with limit:
            try:
                orders, fills, empty_history = await _signed_history(exchange, symbol,
                    int(start.timestamp() * 1000), int(end.timestamp() * 1000))
                return _outcomes(scope, orders, fills, strategy, empty_history)
            except Exception as exc:
                result['issues'].append({'symbol': symbol, 'reason': type(exc).__name__})
                return {row['signal_id']: {'signal_id': row['signal_id'], 'symbol': symbol,
                        'outcome': 'unverified', 'reason': 'signed_history_unavailable'} for row in scope}

    proofs = {}
    for detail in await asyncio.gather(*(verify(symbol, scope) for symbol, scope in symbols.items())):
        proofs.update(detail)
    result['signals'] = [proofs[row['signal_id']] for row in rows]
    result['verified'] = not result['issues'] and all(
        row['outcome'] in {'filled', 'pre_submit_rejected'} for row in result['signals'])
    return result
