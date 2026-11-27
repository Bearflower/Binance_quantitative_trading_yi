"""Bounded grid release evidence and explicit owned-order maintenance.

The default operation copies the journal and permits exchange reads only.
Maintenance never submits entries, closes a position, or clears a risk lock.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from shared.binance_api import BinanceAPIError
from strategies.grid.execution.contracts import ExecutionSettings
from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.exchange import GridExchange
from strategies.grid.execution.fills import ProtectionCoordinator
from strategies.grid.execution.planner import HardLimits
from strategies.grid.execution.risk import RiskLimits, RiskStatus
from strategies.grid.execution.risk_tracker import RiskTracker
from strategies.grid.execution.store import DecisionStore, ExecutionLock


class GuardError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise GuardError(message)


def number(value):
    result = Decimal(str(value))
    require(result.is_finite(), 'nonfinite account evidence')
    return result


TABLES = ('schema_meta', 'engine_state', 'grid_epoch', 'order_intent', 'fill',
          'protection', 'equity_checkpoint', 'grid_move', 'grid_window', 'grid_stream_restore')


def journal_digest(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return digest({name: sorted([tuple(r) for r in conn.execute('SELECT * FROM "' + name + '"')], key=str)
                   for name in TABLES if name in tables})


def risk_history(conn):
    return digest([tuple(r) for r in conn.execute('SELECT * FROM equity_checkpoint ORDER BY id')])


def copy_journal(source, destination):
    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as live:
        with sqlite3.connect(destination) as target:
            live.backup(target)
    return DecisionStore(destination)


def binding(store, settings, identity, env):
    bound = store.conn.execute("SELECT value FROM schema_meta WHERE key='account_identity'").fetchone()
    require(bound and bound[0] == identity, 'journal account identity mismatch')
    row = store.conn.execute("SELECT value FROM schema_meta WHERE key='classic_risk_continuity_v1'").fetchone()
    require(row is not None, 'classic continuity evidence missing')
    continuity = json.loads(row[0])
    pin = env.get('GRID_CLASSIC_RISK_CONTINUITY_SHA256')
    require(pin and continuity.get('manifest_sha256') == pin and continuity.get('identity') == identity,
            'classic continuity binding mismatch')
    policy = asdict(settings)
    del policy['api_key'], policy['api_secret']
    account = env.get('GRID_ACCOUNT_ID', '').strip()
    require(account and account not in ('default', 'main'), 'explicit grid account binding required')
    return dict(account=account, account_mode='classic', account_identity_sha256=digest(identity),
                risk_policy_sha256=digest(policy), continuity_sha256=digest(continuity),
                risk_history_sha256=risk_history(store.conn))


def position_amount(rows):
    require(len(rows) == 1 and rows[0].get('symbol') == 'ETHUSDT'
            and rows[0].get('positionSide') == 'BOTH', 'one-way account position unavailable')
    return number(rows[0]['positionAmt'])


def validate_order(row, intent):
    require(intent is not None and intent['role'] in ('ENTRY', 'REVERSE')
            and row.get('symbol') == 'ETHUSDT' and row.get('clientOrderId') == intent['client_order_id']
            and row.get('side') == intent['side']
            and number(row['origQty']) == number(intent['quantity'])
            and number(row['price']) == number(intent['price'])
            and number(intent['executed_qty']) <= number(row['executedQty']) <= number(intent['quantity'])
            and (not intent['exchange_order_id'] or str(row['orderId']) == intent['exchange_order_id'])
            and row.get('status') in ('NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED'),
            'ordinary order ownership mismatch')


def valid_stop(row, intent, epoch, amount, context):
    if not intent or not context or not amount:
        return False
    side = 'SELL' if amount > 0 else 'BUY'
    boundary = context['stop_loss_low'] if amount > 0 else context['stop_loss_high']
    try:
        return (row.get('symbol') == 'ETHUSDT' and row.get('clientAlgoId') == intent['client_algo_id']
                and row.get('side') == intent['side'] == side and intent['epoch_id'] == epoch
                and row.get('algoStatus') == 'NEW' and row.get('closePosition') is True
                and row.get('workingType') == 'MARK_PRICE' and row.get('positionSide') == 'BOTH'
                and row.get('orderType', row.get('type')) == 'STOP_MARKET'
                and number(row['triggerPrice']) == number(intent['trigger_price']) == number(boundary)
                and (not intent['exchange_algo_id'] or str(row['algoId']) == intent['exchange_algo_id']))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


class RestrictedExchange:
    READS = {'position', 'mark_price', 'ticker_price', 'account_info', 'income', 'journal_identity',
             'open_orders', 'open_stops', 'query_order', 'query_order_by_id', 'query_stop',
             'order_trades', 'verify_account'}

    def __init__(self, exchange, store, *, apply=False):
        self.exchange, self.store, self.apply = exchange, store, apply
        self.pm = getattr(exchange, 'pm', False)
        self.writes = []

    def __getattr__(self, name):
        if name in self.READS:
            return getattr(self.exchange, name)
        raise GuardError('unapproved exchange operation')

    async def place_limit(self, *args, **kwargs):
        raise GuardError('maintenance forbids ENTRY and REVERSE orders')

    async def close_position(self, *args, **kwargs):
        raise GuardError('maintenance forbids market position closure')

    async def order_trades(self, order_id, start, end):
        intents = [r for r in self.store.all_orders() if r['exchange_order_id'] == str(order_id)]
        require(len(intents) == 1, 'trade history order identity ambiguous')
        rows = await self.exchange.order_trades(order_id, start, end)
        ids = set()
        for row in rows:
            require(str(row.get('orderId')) == str(order_id) and row.get('symbol') == 'ETHUSDT'
                    and row.get('side') == intents[0]['side'] and str(row.get('id', '')).isdigit()
                    and str(row['id']) not in ids and number(row['qty']) > 0 and number(row['price']) > 0,
                    'trade history ownership or completeness mismatch')
            ids.add(str(row['id']))
        require(sum((number(r['qty']) for r in rows), Decimal(0)) == number(intents[0]['executed_qty']),
                'trade history quantity incomplete')
        return rows

    async def cancel_order(self, client_id):
        require(self.apply, 'read-only evidence forbids cancellation')
        intent = self.store.get_order(client_id)
        row = await self.exchange.query_order(client_id)
        validate_order(row, intent)
        if row['status'] in ('FILLED', 'CANCELED', 'EXPIRED'):
            return row
        self.writes.append(dict(operation='cancel_owned_order', client_id_sha256=digest(client_id)))
        return await self.exchange.cancel_order(client_id)

    async def place_stop(self, side, trigger, client_id):
        require(self.apply, 'read-only evidence forbids protection creation')
        epoch = self.store.active_epoch_id()
        context = self.store.epoch(epoch)
        amount = position_amount(await self.exchange.position())
        intent = self.store.conn.execute('SELECT * FROM protection WHERE client_algo_id=?', (client_id,)).fetchone()
        require(amount != 0 and context and intent and intent['epoch_id'] == epoch
                and intent['status'] == 'PREPARED' and intent['side'] == side
                and side == ('SELL' if amount > 0 else 'BUY')
                and number(trigger) == number(intent['trigger_price']) == number(
                    context['stop_loss_low'] if amount > 0 else context['stop_loss_high']),
                'protection is not bound to current position and original boundary')
        self.writes.append(dict(operation='original_boundary_close_position_stop', client_id_sha256=digest(client_id)))
        return await self.exchange.place_stop(side, trigger, client_id)

    async def cancel_stop(self, client_id):
        require(self.apply, 'read-only evidence forbids stop cancellation')
        known = {r['client_algo_id']: r for r in self.store.active_protections()}
        require(client_id in known, 'unowned stop cancellation forbidden')
        amount = position_amount(await self.exchange.position())
        if amount:
            epoch = self.store.active_epoch_id()
            rows = await self.exchange.open_stops()
            require(any(r.get('clientAlgoId') != client_id and valid_stop(r, known.get(r.get('clientAlgoId')),
                        epoch, amount, self.store.epoch(epoch)) for r in rows),
                    'replacement protection is not confirmed')
        self.writes.append(dict(operation='retire_owned_stop', client_id_sha256=digest(client_id)))
        return await self.exchange.cancel_stop(client_id)


def limits(settings):
    return HardLimits(settings.max_margin_usdt, settings.max_notional_usdt,
                      settings.max_open_orders, settings.expected_leverage)


async def owned_view(store, exchange):
    rows = await exchange.open_orders()
    known = {r['client_order_id']: r for r in store.all_orders()}
    require(len({r.get('clientOrderId') for r in rows}) == len(rows), 'duplicate ordinary order identity')
    for row in rows:
        validate_order(row, known.get(row.get('clientOrderId')))
    stops = await exchange.open_stops()
    protections = {r['client_algo_id']: r for r in store.active_protections()}
    require(len({r.get('clientAlgoId') for r in stops}) == len(stops), 'duplicate protective order identity')
    for row in stops:
        intent = protections.get(row.get('clientAlgoId'))
        context = store.epoch(intent['epoch_id']) if intent else None
        direction = Decimal(1) if intent and intent['side'] == 'SELL' else Decimal(-1)
        require(intent and valid_stop(row, intent, store.active_epoch_id(), direction, context),
                'unowned or altered protective order')
    return rows, stops


async def complete_history(store, exchange):
    """Verify every submitted durable ordinary intent before maintenance writes."""
    uncreated = []
    verified_orders, verified_trades = {}, {}
    for intent in store.all_orders():
        if intent['status'] in ('REJECTED', 'ABSENT'):
            require(intent['role'] == 'ENTRY' and not intent['exchange_order_id']
                    and number(intent['executed_qty']) == 0 and not store.fills(intent['client_order_id'])
                    and store.epoch(intent['epoch_id'])['status'] == 'CLOSED',
                    'uncreated order has execution or an open epoch')
            uncreated.append(intent)
            continue
        if intent['role'] not in ('ENTRY', 'REVERSE'):
            continue
        if not intent['exchange_order_id'] and store.unsubmitted_move_draft(intent['client_order_id']):
            continue
        row = await exchange.query_order(intent['client_order_id'])
        validate_order(row, intent)
        verified_orders[str(row['orderId'])] = row
        # Local fills may lag a new fill, but may never exceed the verified record.
        fills = store.fills(intent['client_order_id'])
        require(sum((number(f['quantity']) for f in fills), Decimal(0)) <= number(row['executedQty']),
                'journal fills exceed exchange order')
        if number(row['executedQty']):
            trades = await exchange.order_trades(int(row['orderId']), intent['created_at_ms'], int(time.time() * 1000))
            ids = set()
            for trade in trades:
                require(str(trade.get('orderId')) == str(row['orderId']) and trade.get('symbol') == 'ETHUSDT'
                        and trade.get('side') == intent['side'] and str(trade.get('id', '')).isdigit()
                        and str(trade['id']) not in ids and number(trade['qty']) > 0 and number(trade['price']) > 0,
                        'historical trade ownership mismatch')
                ids.add(str(trade['id']))
                verified_trades[(str(row['orderId']), str(trade['id']))] = (
                    trade['side'], number(trade['qty']), number(trade['price']))
            require(sum((number(t['qty']) for t in trades), Decimal(0)) == number(row['executedQty']),
                    'historical trade quantity incomplete')
            actual = {str(t['id']): t for t in trades}
            for fill in fills:
                trade = actual.get(fill['trade_id'])
                require(trade and number(trade['qty']) == number(fill['quantity'])
                        and number(trade['price']) == number(fill['price']), 'historical fill differs from journal')
    if uncreated:
        await verify_uncreated_orders(store, exchange, uncreated, verified_orders, verified_trades)


def _audit(store, key):
    row = store.conn.execute('SELECT value FROM schema_meta WHERE key=?', (key,)).fetchone()
    require(row is not None, 'uncreated order audit missing')
    value = json.loads(row[0])
    require(isinstance(value, dict), 'invalid uncreated order audit')
    return value


def _timestamp(value):
    require(type(value) is int and value > 0, 'invalid audit timestamp')
    return value


def _history_order(actual, intent, verified=None):
    expected = verified or intent
    quantity = expected['origQty'] if verified else intent['quantity']
    executed = expected['executedQty'] if verified else intent['executed_qty']
    require(actual.get('symbol') == 'ETHUSDT' and actual.get('side') == intent['side']
            and (intent['role'] == 'STOP' or actual.get('clientOrderId') == intent['client_order_id'])
            and actual.get('status') == expected['status']
            and number(actual['origQty']) == number(quantity)
            and number(actual['executedQty']) == number(executed), 'account order history differs from journal')


def _absence_audit(store, intent, identity, now_ms, known, fills):
    from strategies.grid.execution.startup_recovery import fingerprint
    evidence = _audit(store, 'startup_absence_recovery:' + str(intent['epoch_id']))
    first, second = evidence['first'], evidence['second']
    epoch = store.epoch(intent['epoch_id'])
    fields = ('identity', 'journal_sha256', 'epoch_id', 'client_order_id', 'query_code', 'history_sha256')
    require(all(first[key] == second[key] for key in fields)
            and first['identity'] == identity and first['epoch_id'] == intent['epoch_id']
            and first['client_order_id'] == intent['client_order_id'] and first['query_code'] == -2013,
            'absence audit identity differs')
    require(isinstance(first['journal_sha256'], str) and len(first['journal_sha256']) == 64
            and all(char in '0123456789abcdef' for char in first['journal_sha256']), 'invalid journal audit hash')
    t1, t2 = _timestamp(first['checked_at_ms']), _timestamp(second['checked_at_ms'])
    recovered = _timestamp(evidence['recovered_at_ms'])
    cutoff = store.conn.execute("SELECT value FROM schema_meta WHERE key='position_recovery_cutoff_ms'").fetchone()
    require(t1 - epoch['created_at_ms'] >= 300_000 and intent['created_at_ms'] <= t1
            and 5000 <= t2-t1 <= 60_000 and 0 <= recovered-t2 <= 30_000
            and cutoff is not None and recovered <= int(cutoff[0]) <= now_ms,
            'absence audit timing or signal cutoff invalid')
    peers = [row for row in store.all_orders()
             if row['epoch_id'] == intent['epoch_id'] and row['client_order_id'] != intent['client_order_id']]
    require(peers and all(row['role'] == 'ENTRY' and row['status'] == 'CANCELED'
            and row['exchange_order_id'] and number(row['executed_qty']) == 0
            and not store.fills(row['client_order_id']) for row in peers), 'absence startup peers differ')
    required = {row['exchange_order_id'] for row in peers}
    for sample in (first, second):
        history = sample['history']
        require(fingerprint(history) == sample['history_sha256'], 'absence history audit hash differs')
        confirmed = set()
        for actual in history['orders']:
            venue = str(actual['orderId'])
            require(venue in known and venue not in confirmed
                    and actual.get('status') in ('CANCELED', 'EXPIRED', 'FILLED'), 'absence order audit unowned')
            _history_order(actual, known[venue])
            confirmed.add(venue)
        require(required <= confirmed, 'absence startup order history incomplete')
        seen = set()
        for trade in history['trades']:
            key = (str(trade['orderId']), str(trade['id']))
            require(key not in seen and trade.get('symbol') == 'ETHUSDT'
                    and epoch['created_at_ms']-5000 <= trade['time'] <= sample['checked_at_ms']
                    and fills.get(key) == (trade['side'], number(trade['qty']), number(trade['price'])),
                    'absence trade audit differs')
            seen.add(key)


async def verify_uncreated_orders(store, exchange, intents, verified_orders, verified_trades):
    """An audited non-order is still checked against fresh whole-account facts.

    Legacy -5022 records retain their original minimal schema. Their trusted
    typed-POST contract is supplemented by the same signed absence/history
    proof; a GET -2013 alone never invents a rejection or retires an intent.
    """
    now_ms = int(time.time() * 1000)
    bound = store.conn.execute("SELECT value FROM schema_meta WHERE key='account_identity'").fetchone()
    identity = await exchange.journal_identity()
    require(bound and bound[0] == identity and identity.startswith('classic:'), 'uncreated order account differs')
    require(position_amount(await exchange.position()) == 0, 'uncreated order proof requires flat position')
    orders = store.all_orders()
    known = {row['exchange_order_id']: row for row in orders if row['exchange_order_id']}
    require(len(known) == sum(bool(row['exchange_order_id']) for row in orders), 'duplicate journal venue identity')
    by_client = {row['client_order_id']: row for row in orders}
    fills = {}
    net = Decimal(0)
    for fill in store.all_fills():
        source = by_client[fill['client_order_id']]
        key = (fill['exchange_order_id'], fill['trade_id'])
        require(source['exchange_order_id'] == key[0] and key not in fills, 'journal fill identity differs')
        fills[key] = (source['side'], number(fill['quantity']), number(fill['price']))
        net += number(fill['quantity']) * (1 if source['side'] == 'BUY' else -1)
    require(net == 0, 'uncreated order proof requires flat journal')
    for intent in intents:
        client_id = intent['client_order_id']
        if intent['status'] == 'REJECTED':
            evidence = _audit(store, 'order_rejection:' + client_id)
            require(evidence.get('api_code') == -5022 and evidence.get('reason') == 'GTX_ORDER_REJECT'
                    and intent['created_at_ms'] <= _timestamp(evidence['observed_at_ms']) <= now_ms,
                    'maker rejection audit is not an explicit POST denial')
            if 'intent' in evidence:
                require(evidence['intent'] == {key: intent[key] for key in (
                    'client_order_id', 'epoch_id', 'role', 'side', 'price', 'quantity')}, 'maker rejection intent differs')
        else:
            _absence_audit(store, intent, identity, now_ms, known, fills)
        try:
            await exchange.query_order(client_id)
        except BinanceAPIError as exc:
            require(exc.code == -2013, 'signed absence query failed')
        else:
            raise GuardError('audited uncreated order now exists')
    start = max(1, min(store.epoch(row['epoch_id'])['created_at_ms'] for row in intents) - 5000)
    confirmed = set()
    for actual in await exchange.order_history(start, now_ms):
        venue = str(actual['orderId'])
        require(venue in known and venue not in confirmed, 'fresh account order history unowned or duplicated')
        _history_order(actual, known[venue], verified_orders.get(venue))
        confirmed.add(venue)
    required = {venue for venue, row in known.items() if row['created_at_ms'] >= start}
    require(required <= confirmed, 'fresh account order history incomplete')
    facts = dict(fills)
    for key, fact in verified_trades.items():
        require(key not in facts or facts[key] == fact, 'fresh trade differs from journal')
        facts[key] = fact
    for venue in required:
        expected = (verified_orders[venue]['executedQty'] if venue in verified_orders
                    else known[venue]['executed_qty'])
        require(sum((fact[1] for key, fact in facts.items() if key[0] == venue), Decimal(0))
                == number(expected), 'journal execution history incomplete')
    seen = set()
    for trade in await exchange.trade_history(start, now_ms):
        key = (str(trade['orderId']), str(trade['id']))
        require(key not in seen and trade.get('symbol') == 'ETHUSDT' and start <= trade['time'] <= now_ms
                and facts.get(key) == (trade['side'], number(trade['qty']), number(trade['price'])),
                'fresh account trade history unowned or inconsistent')
        seen.add(key)
    require({key for key in facts if key[0] in required} <= seen, 'fresh account trade history incomplete')


async def safety_view(store, exchange):
    ordinary, stops = await owned_view(store, exchange)
    amount = position_amount(await exchange.position())
    epoch = store.active_epoch_id()
    context = store.epoch(epoch) if epoch is not None else None
    known = {r['client_algo_id']: r for r in store.active_protections()}
    protected = amount == 0 or any(valid_stop(r, known.get(r.get('clientAlgoId')), epoch, amount, context) for r in stops)
    return dict(flat=amount == 0, position=str(amount), ordinary_orders=len(ordinary), algo_orders=len(stops),
                protected=protected, ordinary_orders_sha256=digest(ordinary), algo_orders_sha256=digest(stops),
                journal_active_orders=len(store.active_orders()), journal_active_protections=len(store.active_protections()),
                fills_sha256=digest(store.all_fills()), journal_net=str(sum((number(r['quantity']) *
                    (1 if r['side'] == 'BUY' else -1) for r in store.all_fills()), Decimal(0))))


async def pause_owned(store, exchange, settings, *, restore_entries=False):
    state = store.engine_state()
    require(state['state'] in ('ACTIVE', 'PAUSED', 'FLAT'), 'maintenance refuses locked or exiting state')
    await owned_view(store, exchange)  # No write before whole-account ownership proof.
    await complete_history(store, exchange)
    client = RestrictedExchange(exchange, store, apply=True)
    epoch = store.active_epoch_id()
    pause_complete = state['state'] == 'FLAT' and epoch is None
    if epoch is not None:
        context = store.epoch(epoch)
        require(context and context['stop_loss_low'] and context['stop_loss_high'], 'missing original stop boundaries')
        engine = GridEngine(store, client, limits(settings))
        if restore_entries:
            # SIGINT may arrive before the original stream callback captures
            # its entry set. Preserve the existing recovery contract explicitly.
            from strategies.grid.execution.trailing import TrailingController
            from strategies.grid.execution.stream_recovery import StreamRecovery
            StreamRecovery(TrailingController(engine)).capture(epoch)
        async def no_exit(*args, **kwargs):
            raise GuardError('maintenance cannot perform exit_all')
        engine.exit_all = no_exit
        pause_complete = await engine.pause(epoch, stop_loss_low=number(context['stop_loss_low']),
                                            stop_loss_high=number(context['stop_loss_high']),
                                            reason='user stream disconnected', trigger='release maintenance')
        # Even an uncertain cancellation must leave confirmed residual protection.
        # This does not clear the LOCKED state or authorize deployment.
        try:
            amount = position_amount(await exchange.position())
            if amount:
                await ProtectionCoordinator(store, client).ensure(epoch, amount,
                    number(context['stop_loss_low']), number(context['stop_loss_high']))
        except Exception:
            pass
    try:
        view = await safety_view(store, exchange)
    except Exception:
        view = dict(flat=False, protected=False)
    ready = (pause_complete and view['flat'] and view['protected']
             and view.get('ordinary_orders') == view.get('algo_orders') == 0
             and view.get('journal_active_orders') == view.get('journal_active_protections') == 0
             and number(view.get('journal_net', 'NaN')) == 0)
    outcome = ('flat_ready' if ready else 'urgent_unprotected_failure' if not view['protected']
               else 'safe_nonflat_abort' if not view['flat'] else 'incomplete_pause_abort')
    return dict(view, pause_complete=pause_complete, ready_for_writer=ready, outcome=outcome,
                writes=client.writes, engine_state=store.engine_state())


def trace_client(exchange, requests, *, apply=False):
    original = exchange.client._request_once
    async def traced(method, endpoint, params=None, signed=True):
        path = exchange.client._get_endpoint(endpoint)
        name = path.rsplit('/', 1)[-1]
        public = method == 'GET' and signed is False and name in ('premiumIndex', 'price', 'exchangeInfo')
        require((signed is True or public) and path.startswith('/fapi/'), 'unexpected account endpoint')
        allowed = method == 'GET' or (apply and (
            (method == 'DELETE' and name in ('order', 'algoOrder'))
            or (method == 'POST' and name == 'algoOrder' and (params or {}).get('closePosition') == 'true'
                and (params or {}).get('type') == 'STOP_MARKET')))
        require(allowed, 'maintenance request is outside write allowlist')
        requests.append(dict(method=method, path=path, signed=signed))
        return await original(method, endpoint, params, signed)
    exchange.client._request_once = traced


async def assess(store, exchange, settings):
    risk = await RiskTracker(store, exchange, RiskLimits(settings.max_daily_loss_usdt,
                            settings.max_drawdown_usdt)).assess(int(time.time() * 1000))
    require(risk.status == RiskStatus.ALLOW, 'current grid risk does not allow release')
    return dict(risk_status=risk.status.value, risk_reasons=list(risk.reasons))


async def inspect_grid(env, *, exchange=None):
    settings = ExecutionSettings.from_env(env)
    require(not settings.use_unified_account, 'classic grid required')
    # Reuse the production weighted budget and response-header IP cooldown.
    # trace_client(apply=False) blocks every exchange mutation before dispatch.
    exchange = exchange or GridExchange.from_settings(settings)
    requests = []
    trace_client(exchange, requests)
    source = env['GRID_EXECUTION_DB']
    try:
        for attempt in range(3):
            attempt_started = time.time()
            with tempfile.TemporaryDirectory(prefix='grid-release-read-') as path:
                clone = copy_journal(source, Path(path) / 'journal')
                try:
                    before = journal_digest(clone.conn)
                    facts = binding(clone, settings, await exchange.journal_identity(), env)
                    require(clone.engine_state()['state'] in ('ACTIVE', 'PAUSED', 'FLAT'), 'unsafe grid engine state')
                    restricted = RestrictedExchange(exchange, clone)
                    await restricted.verify_account(settings.expected_leverage)
                    await owned_view(clone, restricted)
                    engine = GridEngine(clone, restricted, limits(settings))
                    require(await engine.reconcile(), 'read-only grid reconciliation incomplete')
                    await engine.check_limits()
                    risk = await assess(clone, restricted, settings)
                    view = await safety_view(clone, restricted)
                    require(view['protected'], 'current grid protection incomplete')
                    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as live:
                        live.execute('BEGIN')
                        unchanged = before == journal_digest(live)
                    if not unchanged:
                        continue
                    heartbeat = json.loads(Path(env.get('GRID_EXECUTION_HEARTBEAT',
                        '/app/data/grid_execution_heartbeat.json')).read_text())
                    age = int(time.time() * 1000) - heartbeat['updated_at_ms']
                    state = clone.engine_state()['state']
                    require(0 <= age <= 120_000 and heartbeat.get('state') == state
                            and heartbeat.get('risk_status') == 'ALLOW'
                            and (heartbeat.get('stream_ready') is True if state == 'ACTIVE'
                                 else view['ordinary_orders'] == 0), 'fresh safe grid heartbeat required')
                    return dict(facts, **view, **risk, finite_balances=True, reconciled=True,
                        snapshot_sha256=before, snapshot_stable=True, engine_state=[clone.engine_state()],
                        engine_state_sha256=digest([clone.engine_state()]), read_only=True,
                        observed_at=attempt_started,
                        live_journal_untouched=True, heartbeat_fresh=True,
                        heartbeat_age_ms=age, stream_ready=heartbeat.get('stream_ready'), requests=requests)
                finally:
                    clone.close()
        raise GuardError('grid journal changed during all bounded evidence attempts')
    finally:
        await exchange.close()


async def maintain_grid(env, expected):
    settings = ExecutionSettings.from_env(env)
    require(not settings.use_unified_account, 'classic grid required')
    result = dict(ready_for_writer=False, outcome='urgent_unprotected_failure', phases=[], requests=[])
    exchange = None
    try:
        with ExecutionLock(env['GRID_EXECUTION_LOCK']):
            result['lock_acquired_at'] = time.time()
            result['phases'].append('maintenance_lock_acquired')
            store = DecisionStore(Path(env['GRID_EXECUTION_DB']))
            try:
                exchange = GridExchange.from_settings(settings)
                trace_client(exchange, result['requests'], apply=True)
                before = binding(store, settings, await exchange.journal_identity(), env)
                for key in ('account', 'account_mode', 'account_identity_sha256', 'risk_policy_sha256', 'continuity_sha256'):
                    require(before[key] == expected.get(key), 'maintenance binding differs from preflight')
                result['before'] = before
                await exchange.verify_account(settings.expected_leverage)
                # The original risk history is read on a private copy, never reseeded.
                with tempfile.TemporaryDirectory(prefix='grid-maintenance-risk-') as path:
                    clone = copy_journal(env['GRID_EXECUTION_DB'], Path(path) / 'journal')
                    try:
                        result.update(await assess(clone, RestrictedExchange(exchange, clone), settings))
                    finally:
                        clone.close()
                result['phases'].append('risk_verified')
                result.update(await pause_owned(store, exchange, settings,
                    restore_entries=env.get('GRID_TRAILING_ENABLED', 'false').strip().lower() == 'true'))
                result['phases'].append('pause_checked')
                samples = []
                for index in range(2):
                    if index:
                        await asyncio.sleep(5)
                    sample_started = time.time()
                    sample = await safety_view(store, exchange)
                    sample['observed_at'] = sample_started
                    samples.append(sample)
                    if not sample['flat'] or not sample['protected']:
                        result['ready_for_writer'] = False
                result['samples'] = samples
                with tempfile.TemporaryDirectory(prefix='grid-maintenance-risk-after-') as path:
                    clone = copy_journal(env['GRID_EXECUTION_DB'], Path(path) / 'journal')
                    try:
                        result.update(await assess(clone, RestrictedExchange(exchange, clone), settings))
                    finally:
                        clone.close()
                after = binding(store, settings, await exchange.journal_identity(), env)
                require(after == before, 'maintenance changed account, continuity or risk history')
                result['after'] = after
                result['ready_for_writer'] = bool(result['ready_for_writer'] and all(
                    r['flat'] and r['protected'] and r['ordinary_orders'] == r['algo_orders'] == 0
                    and r['journal_active_orders'] == r['journal_active_protections'] == 0
                    and number(r['journal_net']) == 0 for r in samples))
                if result['ready_for_writer']:
                    result['phases'].append('postpause_flat_proven')
                elif not all(r['protected'] for r in samples):
                    result['outcome'] = 'urgent_unprotected_failure'
                elif any(not r['flat'] for r in samples):
                    result['outcome'] = 'safe_nonflat_abort'
            finally:
                store.close()
        result['lock_released_at'] = time.time()
    except Exception as exc:
        result.update(ready_for_writer=False, failure_type=type(exc).__name__,
                      outcome='urgent_unprotected_failure' if result.get('protected') is False
                      else 'maintenance_failed' if result.get('protected') is True else 'safety_unproven_abort')
    finally:
        if exchange is not None:
            await exchange.close()
    result['observed_at'] = (result['samples'][-1]['observed_at'] if result.get('samples')
                             else result.get('lock_acquired_at', time.time()))
    return result
