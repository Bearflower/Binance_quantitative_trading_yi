"""Read-only HRS signal and flat-to-flat accounting by entry mode.

Run with DATABASE_URL and TRADE_ACCOUNT_ID for the HRS instance, for example
python -m strategies.hrs.mode_report --start 2026-11-01T00:00:00+08:00
(--end defaults to the snapshot time). Dates must include a timezone.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os

from shared.accounting.income_integrity import require_income_integrity
from shared.accounting.report_reader import read_hrs_summary_provenance
from .acceptance_report import (MODES, _Reader, _object, _time, read_lifecycle_income,
                                reconcile_lifecycles, write_report_atomic)
from .decision_capture import canonical_payload


# hrs_signals is instance-wide. Never infer a historical routing decision from
# today's config or the scoring origin (either can differ from the actual route).
SIGNALS = """
WITH preflight AS (
    SELECT e.payload->>'signal_id' AS signal_id, e.symbol,
           e.payload->>'direction' AS direction, e.payload->>'entry_mode' AS mode,
           min(e.run_id) AS first_run, max(e.run_id) AS last_run,
           bool_or(e.payload->'is_shadow'='true'::jsonb) AS all_shadow,
           bool_or(e.payload->'is_shadow'='false'::jsonb) AS all_live
    FROM hrs.hrs_decision_evidence e
    WHERE e.kind='pool_circuit_preflight_denial' AND e.event_time >= $1
      AND e.available_at < $2 AND e.recorded_at < $2 AND e.payload->>'reason'='pool_circuit_breaker'
    GROUP BY 1,2,3,4
), decisions AS (
    SELECT e.payload->'signal'->>'signal_id' AS signal_id, e.symbol,
           e.payload->'signal'->>'direction' AS direction, e.payload->'signal'->>'entry_mode' AS mode,
           min(e.run_id) AS first_run, max(e.run_id) AS last_run,
           bool_or(e.payload->'result'='false'::jsonb) AS denied,
           bool_or(e.payload->'signal'->'shadow_blocked'='true'::jsonb) AS shadowed
    FROM hrs.hrs_decision_evidence e WHERE e.kind='signal_execution_outcome'
      AND e.event_time >= $1 AND e.available_at < $2 AND e.recorded_at < $2
    GROUP BY 1,2,3,4
), signal_rows AS (
    SELECT s.id, s.symbol, s.triggered_at, s.direction, s.entry_mode, s.signal_origin,
           CASE WHEN least(p.first_run,d.first_run)=greatest(p.last_run,d.last_run)
                THEN least(p.first_run,d.first_run) END AS run_id,
           COALESCE(CASE WHEN s.resolved_at < $2 THEN s.resolution END,
                    CASE WHEN d.denied THEN 'execution_declined' WHEN d.shadowed THEN 'shadow' END) AS outcome,
           (p.all_shadow OR d.shadowed) AS all_shadow,p.all_live AS all_live,
           EXISTS (SELECT 1 FROM trading.entry_risk_reservation r
                   WHERE r.account_id=$3 AND r.strategy_id='hrs' AND r.symbol=s.symbol
                     AND r.geometry->>'signal_id'=s.id::text AND r.created_at < $2) AS reserved
    FROM hrs.hrs_signals s LEFT JOIN preflight p ON p.signal_id=s.id::text
        AND p.symbol=s.symbol AND p.direction=s.direction AND p.mode=s.entry_mode
    LEFT JOIN decisions d ON d.signal_id=s.id::text AND d.symbol=s.symbol
        AND d.direction=s.direction AND d.mode=s.entry_mode
    WHERE s.triggered_at >= $1 AND s.triggered_at < $2
)
SELECT id, run_id, symbol, triggered_at, direction, entry_mode, signal_origin, COALESCE(outcome,'unresolved') AS outcome,
       CASE WHEN (outcome='shadow' OR all_shadow) AND (reserved OR outcome='executed' OR all_live) THEN 'unknown'
            WHEN outcome='shadow' OR all_shadow THEN 'shadow'
            WHEN outcome='executed' OR reserved OR all_live THEN 'live'
            ELSE 'unknown' END AS route,
       1 AS n FROM signal_rows ORDER BY id
"""

# A captured actual entry order links the exchange order to the exact signal
# via its account-scoped reservation client ID. A symbol/time join alone would
# misattribute parallel modes and consecutive positions in the same symbol.
ENTRY_SOURCES = """
SELECT e.run_id, e.symbol, s.id AS signal_id, s.entry_mode, s.direction, s.resolution,
       s.triggered_at, e.event_time AS observed_at, e.payload AS execution,
       e.payload_sha256 AS execution_hash, r.client_order_id,
       b.event_time AS budget_at, b.payload AS budget, b.payload_sha256 AS budget_hash
FROM hrs.hrs_decision_evidence e
JOIN trading.entry_risk_reservation r
  ON r.client_order_id=e.payload->'outcome'->'order'->>'clientOrderId'
 AND r.account_id=$1 AND r.strategy_id='hrs' AND r.symbol=e.symbol
JOIN hrs.hrs_signals s ON s.id::text=r.geometry->>'signal_id' AND s.symbol=r.symbol
LEFT JOIN hrs.hrs_decision_evidence b ON b.run_id=e.run_id AND b.symbol=e.symbol
 AND b.kind='entry_budget_reserved' AND b.available_at < $2 AND b.recorded_at < $2
 AND b.payload->'decision'->>'client_order_id'=r.client_order_id
WHERE e.kind='order_execution_result' AND e.available_at < $2 AND e.recorded_at < $2
  AND e.payload->'outcome'->'order'->>'orderId'=ANY($3::text[])
"""


TREND_BARS = """
WITH requested AS (
    SELECT * FROM unnest($1::bigint[], $2::text[], $3::text[], $4::text[], $5::timestamptz[])
        AS r(signal_id, run_id, symbol, kind, triggered_at)
)
SELECT r.signal_id, a.event_time, a.payload->'klines_4h' AS bars
FROM requested r JOIN hrs.hrs_decision_evidence a
    ON a.run_id=r.run_id AND a.kind=r.kind AND a.symbol=r.symbol
   AND a.event_time <= a.available_at AND a.available_at <= r.triggered_at AND a.recorded_at < $6
"""


def trend_label(bars, at, direction):
    """Direction vs last closed 4h close / EMA20, SMA-seeded on the captured series."""
    bars = json.loads(bars) if isinstance(bars, str) else bars
    if not isinstance(bars, list) or len(bars) < 20 or direction not in ('long', 'short'):
        return 'unknown'
    interval = 4*3600*1000
    prior, closes = None, []
    try:
        for bar in bars:
            start = bar['open_time']
            if type(start) is not int or start % interval or (prior is not None and start != prior + interval):
                return 'unknown'
            prior = start
            o, h, l, c = (Decimal(str(bar[k])) for k in ('open','high','low','close'))
            if not all(v.is_finite() and v > 0 for v in (o,h,l,c)) or not (l <= o <= h and l <= c <= h):
                return 'unknown'
            closes.append(c)
        if not prior + interval <= int(at.timestamp()*1000) < prior + 2*interval:
            return 'unknown'
        ema = sum(closes[:20])/20
        for close in closes[20:]:
            ema += (close-ema)*2/21
        if closes[-1] == ema:
            return 'unknown'
        return 'aligned' if (closes[-1] > ema) == (direction == 'long') else 'opposed'
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 'unknown'


async def read_trends(connection, signals, entries, end):
    # Exact signal ID -> captured run -> one same-symbol mode-family analysis.
    # Duplicate runs/analyses and older signals without receipts stay unknown.
    requests = defaultdict(set)
    for rows, id_key in ((signals,'id'), (entries,'signal_id')):
        for row in rows:
            if row['run_id'] and row['entry_mode'] in MODES:
                kind = 'lv_rm_analysis' if row['entry_mode'] == 'lv_rm' else 'analysis'
                requests[row[id_key]].add((row['run_id'],row['symbol'],kind,row['triggered_at'],row['direction']))
    scope = {key: next(iter(items)) for key, items in requests.items() if len(items) == 1}
    if not scope:
        return {}
    rows = await connection.fetch(TREND_BARS, list(scope), *([v[i] for v in scope.values()] for i in range(4)), end)
    by_signal = defaultdict(list)
    for row in rows:
        key = row['signal_id']
        by_signal[key].append(trend_label(row['bars'], row['event_time'], scope[key][4]))
    return {key: values[0] for key, values in by_signal.items() if len(values) == 1}


def entry_sources(rows, account):
    sources = defaultdict(set)
    for row in rows:
        payload = _object(row['execution'])
        order = payload['outcome']['order']
        key = (row['symbol'], str(order['orderId']))
        direction = row['direction']
        client = 'risk_' + hashlib.sha256(f"{account}:hrs:{row['signal_id']}".encode()).hexdigest()[:30]
        if (canonical_payload(payload)[1] != row['execution_hash']
                or payload['outcome'].get('order_filled') is not True
                or payload.get('operation') != f'execute_{direction}'
                or order.get('symbol') != row['symbol']
                or client != row['client_order_id'] or order.get('clientOrderId') != client
                or row['resolution'] == 'shadow' or row['entry_mode'] not in MODES):
            sources[key].add(None)
            continue
        stop = budget_at = None
        if row['budget'] is not None:
            budget = _object(row['budget'])
            budget_valid = canonical_payload(budget)[1] == row['budget_hash']
            if budget_valid and budget.get('entry_mode') != row['entry_mode']:
                sources[key].add(None)
                continue
            if (budget_valid
                    and budget.get('decision', {}).get('client_order_id') == client
                    and budget['decision'].get('allowed') is True):
                stop = budget.get('stop_price')
                budget_at = row['budget_at']
        sources[key].add((row['entry_mode'], direction, row['signal_id'],
                          row['triggered_at'], row['observed_at'], stop, budget_at))
    return sources


def lifecycle_mode_and_risk(sample, sources):
    legs = sample['fill_legs']
    direction = 'long' if legs[0]['side'] == 'BUY' else 'short'
    entries = [leg for leg in legs if leg['side'] == legs[0]['side']]
    modes, risk = set(), Decimal(0)
    for leg in entries:
        matches = sources.get((sample['symbol'], leg['order_id']), set())
        if len(matches) != 1 or None in matches:
            return 'unknown', None
        mode, side, _, triggered, observed, stop, budget_at = next(iter(matches))
        at = _time(leg['at'])
        if side != direction or not triggered <= at <= observed:
            return 'unknown', None
        modes.add(mode)
        if risk is None or stop is None or budget_at is None or not triggered <= budget_at <= at:
            risk = None
            continue
        if leg.get('price') is None:
            risk = None
            continue
        stop, price = Decimal(str(stop)), Decimal(str(leg['price']))
        distance = (price-stop) if side == 'long' else (stop-price)
        if not stop.is_finite() or not price.is_finite() or stop <= 0 or distance <= 0:
            risk = None
        else:
            risk += distance * Decimal(leg['quantity'])
    if len(modes) != 1:
        return 'unknown', None
    # Adding exposure later changes the risk baseline. Only the original entry
    # order (possibly many partial fills) qualifies for an initial R multiple.
    if len({leg['order_id'] for leg in entries}) != 1:
        risk = None
    return modes.pop(), risk


def summarize(signals, samples, sources, trends=None):
    trends = trends or {}
    groups = {mode: dict(signals={lane: dict(count=0, outcomes=Counter(), origins=Counter(), trend_4h=Counter())
                                 for lane in ('live', 'shadow', 'unknown')}, trades=[])
              for mode in (*MODES, 'unknown')}
    for s in signals:
        mode = s['entry_mode'] if s['entry_mode'] in MODES else 'unknown'
        bucket = groups[mode]['signals'][s['route']]
        bucket['trend_4h'][trends.get(s['id'], 'unknown')] += s['n']
        bucket['count'] += s['n']
        bucket['outcomes'][s['outcome']] += s['n']
        bucket['origins'][s['signal_origin'] or 'unspecified'] += s['n']
    for sample in samples:
        mode, risk = lifecycle_mode_and_risk(sample, sources)
        reason = None
        net = None
        if sample['status'] == 'RECONCILED':
            gross, fees, funding = (Decimal(sample[k]) for k in
                                    ('realized_pnl_usdt', 'commission_usdt', 'funding_usdt'))
            legs = sample['fill_legs']
            if (gross == sum(Decimal(leg['realized_pnl']) for leg in legs)
                    and fees == -sum(Decimal(leg['commission']) for leg in legs)
                    and gross + fees + funding == Decimal(sample['net_income_usdt'])):
                net = gross + fees + funding
            else:
                reason = 'fill_and_income_totals_disagree'
        else:
            reason = 'lifecycle_or_cost_coverage_incomplete'
        entry_ids = {match[2] for leg in sample['fill_legs'] if leg['side'] == sample['fill_legs'][0]['side']
                     for match in sources.get((sample['symbol'], leg['order_id']), ()) if match}
        trend = trends.get(next(iter(entry_ids)), 'unknown') if len(entry_ids) == 1 and mode != 'unknown' else 'unknown'
        groups[mode]['trades'].append(dict(trend_4h=trend,
            symbol=sample['symbol'], opening_trade_id=sample['fill_ids'][0], opened_at=sample['opened_at'],
            closed_at=sample['closed_at'], fill_count=sample['fill_count'],
            net_usdt=net, gross_usdt=gross if net is not None else None,
            commission_usdt=fees if net is not None else None, funding_usdt=funding if net is not None else None,
            initial_stop_risk_usdt=risk, net_r=net/risk if net is not None and risk else None,
            missing_evidence=reason))
    for bucket in groups.values():
        closed = bucket.pop('trades')
        bucket['live'] = dict(**live_statistics(closed), lifecycles=closed, by_trend_4h={
            label: live_statistics([t for t in closed if t['trend_4h'] == label])
            for label in ('aligned', 'opposed', 'unknown')})
    return groups


def live_statistics(closed):
    verified = [t for t in closed if t['net_usdt'] is not None]
    rs = [t['net_r'] for t in verified if t['net_r'] is not None]
    total = sum((t['net_usdt'] for t in verified), Decimal(0))
    wins = sum(t['net_usdt'] > 0 for t in verified)
    return dict(observed_closed=len(closed), verified_net_count=len(verified),
        missing_net_count=len(closed)-len(verified), net_wins=wins, net_losses=sum(t['net_usdt'] < 0 for t in verified),
        net_breakeven=sum(t['net_usdt'] == 0 for t in verified),
        win_rate=Decimal(wins)/len(verified) if verified else None,
        net_usdt=total if verified else None, expectancy_usdt=total/len(verified) if verified else None,
        known_r_count=len(rs), expectancy_r=sum(rs)/len(rs) if rs else None)


async def read_report(connection, account, start, end, now):
    start, end, now = (_time(t) for t in (start, end, now))
    if not account or not start < end <= now:
        raise ValueError('require instance account and start < end <= snapshot time')
    async with connection.transaction(isolation='repeatable_read', readonly=True):
        signals = await connection.fetch(SIGNALS, start, end, account)
        legacy = await read_hrs_summary_provenance(_Reader(connection), account,
                    int(start.timestamp()*1000), int(end.timestamp()*1000))
        anchor = await connection.fetchrow('SELECT inception,opening_evidence FROM trading.hrs_equity_segment WHERE account_id=$1', account)
        state = await connection.fetchrow('SELECT ledger_state,version FROM trading.hrs_loss_gate_state WHERE account_id=$1', account)
        financial = dict(samples=[], reasons=[], income_report_id=None)
        inception = _time(anchor['inception']) if anchor else None
        through = _time(_object(state['ledger_state'])['as_of']) if state else None
        entries = []
        if not (inception and through and inception < through <= now):
            financial['reasons'].append('missing_or_invalid_reconciled_segment')
        elif inception >= end or through < start:
            financial['reasons'].append('outside_reconciled_segment')
        else:
            await require_income_integrity(connection, account)
            proof = _object(anchor['opening_evidence'])
            fills = [dict(row) for row in await connection.fetch("""
                SELECT account_id,symbol,trade_id,order_id,side,qty,price,commission,commission_asset,
                       realized_pnl,trade_time_utc FROM trading.execution_fact_fill
                WHERE account_id=$1 AND trade_time_utc > $2 AND trade_time_utc <= $3""", account, inception, through)]
            owners = await connection.fetch("""SELECT account_id,symbol,order_id,strategy
                FROM trading.trade_records WHERE account_id=$1 AND order_id=ANY($2::text[])""",
                account, sorted({str(f['order_id']) for f in fills}))
            incomes, economic = await read_lifecycle_income(connection, account, inception, through, now,
                state['version'], proof, fills, owners)
            financial = reconcile_lifecycles(fills, owners, incomes, economic, account_id=account,
                inception=inception, through=through, opening_inventory=proof.get('opening_inventory', ()),
                anchor_certified=proof.get('account_flat_anchor_certified') is True or proof.get('opening_inventory_certified') is True,
                acceptance_samples=False)
            financial['samples'] = [s for s in financial['samples'] if start <= _time(s['closed_at']) < end]
            ids = {leg['order_id'] for s in financial['samples'] for leg in s['fill_legs']
                   if leg['side'] == s['fill_legs'][0]['side']}
            if ids:
                entries = await connection.fetch(ENTRY_SOURCES, account, end, sorted(ids))
        trends = await read_trends(connection, signals, entries, end)
        modes = summarize(signals, financial['samples'], entry_sources(entries, account), trends)
        if any(t['missing_evidence'] == 'fill_and_income_totals_disagree' for v in modes.values() for t in v['live']['lifecycles']):
            financial['reasons'].append('fill_and_income_totals_disagree')
        return dict(report_version='hrs-mode-accounting-v1', account_id=account,
            generated_at=now.isoformat(), start_inclusive=start.isoformat(), end_exclusive=end.isoformat(),
            signal_scope='HRS instance-wide hrs_signals; grouped by trigger time and resolution known before end',
            financial_scope='HRS account-owned exchange fills flat-to-flat by close time; excludes inherited opening positions',
            net_basis='USDT realized income + signed commissions + funding; matched exchange fill and income coverage',
            r_basis='net USDT divided by original filled quantity times adverse distance from fill price to reserved initial stop',
            shadow_basis='shadow decision resolutions only; no simulated or actual PnL assigned',
            equity_segment_inception=inception.isoformat() if inception else None,
            reconciled_through=through.isoformat() if through else None,
            full_window_financial_coverage=bool(inception and through and inception <= start and through >= end
                and not financial['reasons']),
            financial_coverage_issues=financial['reasons'], income_report_id=financial['income_report_id'],
            summary_record_provenance=legacy['sources'], trend_4h_basis='last closed 4h close versus EMA20 at original analysis, relative to signal side; flat or missing is unknown', modes=modes)


async def _run(args):
    import asyncpg
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    now = datetime.now(timezone.utc)
    account = os.environ.get('TRADE_ACCOUNT_ID', '').strip()
    if not account or not os.environ.get('DATABASE_URL'):
        raise ValueError('set DATABASE_URL and TRADE_ACCOUNT_ID for the HRS instance')
    # The container DSN may contain Prisma's schema parameter. SQL here is
    # schema-qualified, and PostgreSQL/asyncpg does not accept this parameter.
    url = urlsplit(os.environ['DATABASE_URL'])
    dsn = urlunsplit(url._replace(query=urlencode([(k, v) for k, v in parse_qsl(url.query) if k != 'schema'])))
    connection = await asyncpg.connect(dsn)
    try:
        result = await read_report(connection, account, _time(args.start), _time(args.end) if args.end else now, now)
    finally:
        await connection.close()
    # Keep decimal monetary and R values exact in JSON, including zero.
    data = json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False)
    if args.output:
        write_report_atomic(json.loads(data), args.output)
    else:
        print(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True, help='ISO 8601 with timezone, inclusive')
    parser.add_argument('--end', help='ISO 8601 with timezone, exclusive')
    parser.add_argument('--output', help='write JSON atomically to this file; default is stdout')
    asyncio.run(_run(parser.parse_args()))
