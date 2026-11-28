"""Entry activity must be a signed identity + economic entry, never a reserve count."""
from datetime import datetime, timezone
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from ai_tuner.allocation.activity_evidence import (
    collect_entry_activity, project_activity_evidence, read_activity_evidence,
)
from shared.readonly_client import ReadOnlyBinanceClient


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def observation(client='old', order=10, executed='2', position_side='LONG', side='BUY'):
    raw = dict(clientOrderId=client, orderId=order, symbol='AAAUSDT', side=side,
               status='PARTIALLY_FILLED', origQty='3', executedQty=executed,
               positionSide=position_side, reduceOnly=False)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(id=1, account_id='default', client_order_id=client, symbol='AAAUSDT',
                order_id=str(order), outcome='ok', raw_payload=raw, payload_sha256=digest,
                side=side, status=raw['status'], orig_qty='3', executed_qty=executed,
                observed_at=stamp(400), credential_fingerprint='a' * 64,
                source='signed_pm_get_order_v1')


def reservation(client='old', strategy='hrs', **kw):
    return dict(client_order_id=client, account_id='default', symbol='AAAUSDT',
                strategy_id=strategy, state='released', created_at=stamp(1), **kw)


def fill(trade_id=1, time_ms=200, **kw):
    return dict(account_id='default', symbol='AAAUSDT', trade_id=trade_id, order_id=10,
                side='BUY', qty='2', trade_time_utc=stamp(time_ms), **kw)


def project(reservations=None, observations=None, fills=None, facts=(), cursors=()):
    return project_activity_evidence(reservations or [], observations or [], fills or [],
                                     cursors, facts, 'default', 100, 300, ['hrs'])['hrs']



@pytest.mark.parametrize('key,expected_gap', [
    (('default', 'AAAUSDT', '1', '10', 200), False),
    (('grid', 'AAAUSDT', '1', '10', 200), True),
    (('default', 'AAAUSDT', '1', '11', 200), True),
    (('default', 'AAAUSDT', '1', '10', 201), True),
])
def test_signed_reduction_resolves_only_exact_fill_without_certifying_window(key, expected_gap):
    row = project_activity_evidence([], [], [fill()], [], [], 'default', 100, 300, ['hrs'],
                                    verified_reductions={key})['hrs']
    assert ('unowned_fill_in_window' in row['reasons']) is expected_gap
    assert 'missing_protocol_cutover' in row['reasons']
    assert row['owned_entry_fills'] is None
    assert row['coverage_complete'] is False


@pytest.mark.parametrize('quantity,observed_qty,expected_gap', [
    ('3', '2', False), ('4', '2', True), ('3', '1', True),
])
def test_other_strategy_order_requires_exact_execution_before_excluding_it(quantity, observed_qty, expected_gap):
    row = project([reservation(strategy='btc_eth_aggressive', quantity=quantity)],
                  [observation(executed=observed_qty)], [fill()])
    assert ('unowned_fill_in_window' in row['reasons']) is expected_gap
    assert 'missing_protocol_cutover' in row['reasons']
    assert row['owned_entry_fills'] is None


@pytest.mark.parametrize('key,expected', [
    (('default', 'hrs', 'AAAUSDT', '1', '10', 200), 1),
    (('default', 'btc_eth_aggressive', 'AAAUSDT', '1', '10', 200), None),
    (('default', 'hrs', 'AAAUSDT', '1', '10', 201), None),
])
def test_exact_closed_lot_proves_one_way_entry_without_certifying_window(key, expected):
    row = project_activity_evidence([reservation()], [observation(position_side='BOTH')], [fill()],
                                    [], [], 'default', 100, 300, ['hrs'], verified_openings={key})['hrs']
    assert row['owned_entry_fills'] == expected
    assert row['coverage_complete'] is False
    assert 'missing_protocol_cutover' in row['reasons']


def test_missing_orders_are_unknown_even_for_released_reservations():
    row = project([reservation()])
    assert row['owned_entry_fills'] is None
    assert not row['coverage_complete']
    assert 'order_observation_missing' in row['reasons']
    assert 'missing_protocol_cutover' in row['reasons']


def test_cross_month_released_partial_fill_is_a_positive_subset_and_deduplicated():
    f = fill()
    row = project([reservation()], [observation()], [f, dict(f)])
    assert row['owned_entry_fills'] == 1
    assert not row['coverage_complete']
    assert 'missing_protocol_cutover' in row['reasons']
    assert row['account_id'] == 'default'
    assert row['start_ms'] == 100 and row['end_ms'] == 300


@pytest.mark.parametrize('change,reason', [
    ({'executed_qty': '3'}, 'invalid_order_observation'),
    ({'account_id': 'grid'}, 'order_observation_missing'),
    ({'payload_sha256': 'bad'}, 'invalid_order_observation'),
])
def test_observation_cannot_override_signed_identity_or_quantity(change, reason):
    row = project([reservation()], [dict(observation(), **change)], [fill()])
    assert row['owned_entry_fills'] is None
    assert reason in row['reasons']


def test_same_client_mapping_to_two_orders_is_rejected():
    a, b = observation(), observation(order=11)
    b['id'] = 2
    row = project([reservation()], [a, b], [fill()])
    assert row['owned_entry_fills'] is None
    assert 'order_identity_conflict' in row['reasons']


def test_one_way_order_without_flat_replay_is_unknown_not_entry():
    row = project([reservation()], [observation(position_side='BOTH')], [fill()])
    assert row['owned_entry_fills'] is None
    assert 'entry_role_unverified' in row['reasons']


def test_fill_quantity_mismatch_is_not_positive_evidence():
    row = project([reservation()], [observation(executed='1')], [fill()])
    assert row['owned_entry_fills'] is None
    assert 'order_fill_quantity_mismatch' in row['reasons']


def test_fill_outside_requested_month_does_not_count():
    row = project([reservation()], [observation()], [fill(time_ms=99)])
    assert row['owned_entry_fills'] is None  # absent protocol cannot establish zero


def test_conflicting_duplicate_fill_and_reservation_owner_are_unknown():
    bad = dict(fill(), qty='1')
    row = project([reservation(), reservation(strategy='btc_eth')], [observation()], [fill(), bad])
    assert row['owned_entry_fills'] is None
    assert 'intent_owner_conflict' in row['reasons']
    assert 'fill_identity_conflict' in row['reasons']


class DB:
    def __init__(self, reservations):
        self.reservations = reservations
        self.execute = AsyncMock()
        self.queries = []

    async def fetch_all(self, sql, *args):
        self.queries.append((sql, args))
        if 'FROM trading.entry_risk_reservation' in sql:
            return self.reservations
        return []


def client():
    exchange = ReadOnlyBinanceClient('test-public-key', 'test-secret')
    exchange.entry_activity_account_id = 'default'
    exchange.get_order_by_client_id = AsyncMock(return_value=observation()['raw_payload'])
    return exchange


@pytest.mark.asyncio
async def test_collector_reads_old_released_orders_appends_and_never_rewrites():
    db, exchange = DB([reservation()]), client()
    result = await collect_entry_activity(db, exchange, 'default', max_requests=3)
    assert result['observed'] == 1
    exchange.get_order_by_client_id.assert_awaited_once_with('AAAUSDT', 'old')
    assert all('UPDATE ' not in c.args[0] and 'DELETE ' not in c.args[0]
               for c in db.execute.await_args_list)
    assert any('entry_order_observation' in c.args[0] for c in db.execute.await_args_list)
    assert not any("state<>" in q or "created_at>=" in q for q, _ in db.queries)
    assert not result['protocol_complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('response,reason', [(None, 'order_not_found'), ({'code': -2013}, 'order_not_found'), ({}, 'invalid_order_response')])
async def test_collector_records_lookup_gaps_instead_of_zero(response, reason):
    db, exchange = DB([reservation()]), client()
    exchange.get_order_by_client_id.return_value = response
    row = await collect_entry_activity(db, exchange, 'default')
    assert row['gaps'] == {reason: 1}
    assert row['observed'] == 0


@pytest.mark.asyncio
async def test_collector_timeout_is_safe_and_does_not_persist_exception_secret():
    db, exchange = DB([reservation()]), client()
    exchange.get_order_by_client_id.side_effect = TimeoutError('apiKey=SECRET')
    row = await collect_entry_activity(db, exchange, 'default')
    assert row['gaps'] == {'order_lookup_timeout': 1}
    assert 'SECRET' not in repr(db.execute.await_args_list)


@pytest.mark.asyncio
async def test_collector_rejects_unbound_account_before_network():
    db, exchange = DB([reservation()]), client()
    exchange.entry_activity_account_id = 'grid'
    with pytest.raises(ValueError, match='account binding'):
        await collect_entry_activity(db, exchange, 'default')
    exchange.get_order_by_client_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_collector_request_cap_records_incomplete_batch():
    db, exchange = DB([reservation(), reservation('second')]), client()
    result = await collect_entry_activity(db, exchange, 'default', max_requests=1)
    assert result['remaining'] == 1
    assert result['gaps']['request_budget_exhausted'] == 1
    assert not result['batch_complete']


@pytest.mark.asyncio
async def test_reader_failure_has_unknown_activity():
    db = DB([])
    db.fetch_all = AsyncMock(side_effect=RuntimeError('secret connection string'))
    result = (await read_activity_evidence(db, 'default', 100, 300, ['hrs']))['hrs']
    assert result['owned_entry_fills'] is None
    assert result['reasons'] == ['activity_source_unavailable']


def test_protocol_declaration_alone_does_not_establish_zero():
    facts = [dict(id=1, account_id='default', kind='capture_boundary', observed_at=stamp(50), payload={}),
             dict(id=2, account_id='default', kind='protocol_window', created_by='migration_admin',
                  payload=dict(capture_boundary_id=1, start_ms=50, end_ms=300, strategy_ids=['hrs'],
                               all_entry_paths_before_submit=True, source_id='reviewed-code', reviewed_by='operator', symbols=['AAAUSDT'], symbol_universe_complete=True))]
    row = project(facts=facts)
    assert row['owned_entry_fills'] is None
    assert 'collection_watermark_missing' in row['reasons']


def test_signed_order_geometry_direction_conflict_is_not_an_entry():
    row = project([reservation(geometry={'direction': 'SHORT'})], [observation()], [fill()])
    assert row['owned_entry_fills'] is None
    assert 'intent_order_direction_conflict' in row['reasons']


@pytest.mark.asyncio
async def test_deleted_intent_remains_a_collection_candidate():
    db, exchange = DB([]), client()
    original_fetch = db.fetch_all
    async def fetch(sql, *args):
        if 'FROM trading.entry_activity_source_fact' in sql:
            return [dict(account_id='default', kind='intent', id=1, payload=reservation())]
        return await original_fetch(sql, *args)
    db.fetch_all = fetch
    await collect_entry_activity(db, exchange, 'default')
    exchange.get_order_by_client_id.assert_awaited_once_with('AAAUSDT', 'old')


@pytest.mark.asyncio
async def test_collector_rejects_exchange_response_for_different_client():
    db, exchange = DB([reservation()]), client()
    exchange.get_order_by_client_id.return_value = observation(client='other')['raw_payload']
    result = await collect_entry_activity(db, exchange, 'default')
    assert result['observed'] == 0
    assert result['gaps'] == {'invalid_order_response': 1}


def protocol_facts():
    return [dict(id=1, account_id='default', kind='capture_boundary', observed_at=stamp(50), payload={}),
            dict(id=2, account_id='default', kind='protocol_window', created_by='migration_admin',
                 payload=dict(capture_boundary_id=1, start_ms=50, end_ms=300, strategy_ids=['hrs'],
                              all_entry_paths_before_submit=True, source_id='reviewed-code', reviewed_by='operator', symbols=['AAAUSDT'], symbol_universe_complete=True))]


def batch(clients, fact_id=3):
    return dict(id=fact_id, account_id='default', kind='collection_batch', payload=dict(
        source='signed_pm_get_order_v1', finished_ms=400, credential_fingerprint='a'*64,
        succeeded_client_order_ids=clients, candidate_count=len(clients),
        candidate_sha256=hashlib.sha256(json.dumps(sorted(clients), separators=(',', ':')).encode()).hexdigest(),
        batch_complete=False, remaining=20, gaps={'request_budget_exhausted': 20}))


def terminal(ob, time_ms=400):
    ob = dict(ob)
    ob['status'] = 'CANCELED'
    ob['raw_payload'] = dict(ob['raw_payload'], status='CANCELED')
    ob['payload_sha256'] = hashlib.sha256(json.dumps(ob['raw_payload'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    ob['observed_at'] = stamp(time_ms)
    return ob


def cursor():
    return dict(account_id='default', source='userTrades', symbol='AAAUSDT',
                window_start_ms=100, covered_until_ms=300, page_complete=True, request_status='ok')


def test_later_success_supersedes_transient_lookup_failure():
    failure = dict(observation(), id=1, outcome='order_lookup_timeout', observed_at=stamp(250))
    success = terminal(dict(observation(), id=2))
    row = project([reservation()], [failure, success], [fill()],
                  protocol_facts() + [batch(['old'])], [cursor()])
    assert row['owned_entry_fills'] == 1
    assert row['coverage_complete'], row


def test_disjoint_partial_collection_batches_form_complete_watermark():
    second = observation(client='second', order=11, executed='0')
    second = terminal(dict(second, id=2))
    row = project([reservation(), reservation('second')], [terminal(observation()), second], [fill()],
                  protocol_facts() + [batch(['old']), batch(['second'], 4)], [cursor()])
    assert row['coverage_complete'], row


def test_terminal_historical_order_missing_old_fills_does_not_block_new_month():
    old = terminal(observation(executed='1'), time_ms=90)
    row = project([reservation()], [old], [], protocol_facts() + [batch(['old'])], [cursor()])
    assert row['coverage_complete'], row
    assert row['owned_entry_fills'] == 0


def test_unowned_fills_prevent_empty_protocol_from_claiming_zero():
    row = project([], [], [fill()], protocol_facts() + [batch([])], [cursor()])
    assert row['owned_entry_fills'] is None
    assert 'unowned_fill_in_window' in row['reasons']


@pytest.mark.asyncio
async def test_collector_summary_identifies_account_and_fresh_invocation():
    db, exchange = DB([]), client()
    row = await collect_entry_activity(db, exchange, 'default')
    assert row['account_id'] == 'default'
    assert row['finished_ms'] >= row['started_ms'] > 0
    assert row['source'] == 'signed_pm_get_order_v1'
    assert row['batch_id']


# Opt-in integration uses only the dedicated local entry_activity_tests database.
import os
from pathlib import Path
from urllib.parse import urlsplit
import asyncpg
import pytest_asyncio

DSN = os.environ.get('ENTRY_ACTIVITY_TEST_DSN', '')
if DSN:
    parsed = urlsplit(DSN)
    if parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.path != '/entry_activity_tests':
        raise ValueError('Only isolated local entry_activity_tests is permitted')
MIGRATION = Path(__file__).parents[2] / 'database/postgres/migrations/2026-11-17-entry-activity.sql'


@pytest_asyncio.fixture
async def pg_activity():
    if not DSN:
        pytest.skip('Set ENTRY_ACTIVITY_TEST_DSN to isolated local entry_activity_tests')
    conn = await asyncpg.connect(DSN)
    await conn.execute('''DROP SCHEMA IF EXISTS trading CASCADE; CREATE SCHEMA trading;
      CREATE TABLE trading.entry_risk_reservation(client_order_id text PRIMARY KEY,account_id text,
         strategy_id text,symbol text,quantity numeric,geometry jsonb,state text,created_at timestamptz DEFAULT now());
      CREATE TABLE trading.execution_fact_fill(account_id text,symbol text,trade_id bigint,order_id bigint,
         side text,qty numeric,trade_time_utc timestamptz);
      CREATE TABLE trading.execution_fact_cursor(account_id text,source text,symbol text,window_start_ms bigint,
         covered_until_ms bigint,page_complete bool,request_status text);
      DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='trading_user') THEN
         CREATE ROLE trading_user NOSUPERUSER NOLOGIN; END IF; END $$;
      GRANT SELECT,INSERT,UPDATE,DELETE ON trading.entry_risk_reservation TO trading_user;
      GRANT SELECT ON trading.execution_fact_fill,trading.execution_fact_cursor TO trading_user;
      INSERT INTO trading.entry_risk_reservation VALUES('old','default','hrs','AAAUSDT',3,'{}','released',now());''')
    try:
        yield conn
    finally:
        if conn.is_in_transaction():
            await conn.execute('ROLLBACK')
        await conn.execute('RESET ROLE')
        await conn.close()


class PGWrapper:
    def __init__(self, conn):
        self.conn = conn

    async def fetch_all(self, sql, *args):
        return [dict(row) for row in await self.conn.fetch(sql, *args)]

    async def execute(self, sql, *args):
        return await self.conn.execute(sql, *args)


@pytest.mark.asyncio
async def test_postgres_idempotent_capture_privileges_and_append_only_observations(pg_activity):
    conn = pg_activity
    await conn.execute(MIGRATION.read_text())
    await conn.execute(MIGRATION.read_text())
    assert await conn.fetchval("SELECT count(*) FROM trading.entry_activity_source_fact WHERE kind='capture_boundary'") == 1
    await conn.execute('SET ROLE trading_user')
    await conn.execute("INSERT INTO trading.entry_risk_reservation VALUES('fresh','default','hrs','AAAUSDT',3,'{}','reserved',now())")
    await conn.execute("UPDATE trading.entry_risk_reservation SET state='submitting' WHERE client_order_id='fresh'")
    assert await conn.fetchval("SELECT count(*) FROM trading.entry_activity_source_fact WHERE kind='intent'") == 1
    with pytest.raises(asyncpg.RaiseError, match='immutable'):
        await conn.execute("UPDATE trading.entry_risk_reservation SET strategy_id='btc_eth' WHERE client_order_id='fresh'")
    with pytest.raises(asyncpg.RaiseError, match='Administrator'):
        await conn.execute("INSERT INTO trading.entry_activity_source_fact(account_id,kind,payload) VALUES('default','protocol_window','{}')")
    exchange = client()
    exchange.get_order_by_client_id.side_effect = lambda symbol, cid: observation(client=cid, order=10 if cid=='old' else 11)['raw_payload']
    result = await collect_entry_activity(PGWrapper(conn), exchange, 'default')
    assert result['observed'] == 2
    assert await conn.fetchval('SELECT count(*) FROM trading.entry_order_observation') == 2
    for table in ('entry_order_observation', 'entry_activity_source_fact'):
        for operation in ('DELETE FROM', 'TRUNCATE'):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(f'{operation} trading.{table}')
    await conn.execute('RESET ROLE')
    for table in ('entry_order_observation', 'entry_activity_source_fact'):
        for operation in ('DELETE FROM', 'TRUNCATE'):
            with pytest.raises(asyncpg.RaiseError, match='append-only'):
                await conn.execute(f'{operation} trading.{table}')
    # Signed identity cannot be rebound by a later collector, including concurrent sources.
    with pytest.raises(asyncpg.RaiseError, match='identity conflict'):
        await conn.execute("""INSERT INTO trading.entry_order_observation(account_id,client_order_id,symbol,order_id,
           side,status,orig_qty,executed_qty,outcome,raw_payload,payload_sha256,credential_fingerprint,source,batch_id)
           SELECT account_id,client_order_id,symbol,'99',side,status,orig_qty,executed_qty,outcome,
             jsonb_set(raw_payload,'{orderId}','99'),payload_sha256,credential_fingerprint,source,batch_id
           FROM trading.entry_order_observation WHERE client_order_id='old'""")


@pytest.mark.asyncio
async def test_postgres_capture_continuity_cannot_be_reinstalled_as_old_history(pg_activity):
    await pg_activity.execute(MIGRATION.read_text())
    await pg_activity.execute('ALTER TABLE trading.entry_risk_reservation DISABLE TRIGGER entry_activity_capture_intent')
    result = (await read_activity_evidence(PGWrapper(pg_activity), 'default', 100, 300, ['hrs']))['hrs']
    assert not result['coverage_complete']
    assert result['owned_entry_fills'] is None
    assert result['reasons'] == ['activity_audit_unhealthy']
    with pytest.raises(asyncpg.RaiseError, match='continuity'):
        await pg_activity.execute(MIGRATION.read_text())


@pytest.mark.asyncio
async def test_collector_reuses_verified_terminal_orders_without_spending_requests():
    db, exchange = DB([reservation(), reservation('new')]), client()
    original_fetch = db.fetch_all
    async def fetch(sql, *args):
        if sql.startswith('SELECT * FROM trading.entry_order_observation'):
            return [terminal(observation())]
        return await original_fetch(sql, *args)
    db.fetch_all = fetch
    exchange.get_order_by_client_id.return_value = observation(client='new', order=11)['raw_payload']
    result = await collect_entry_activity(db, exchange, 'default', max_requests=1)
    exchange.get_order_by_client_id.assert_awaited_once_with('AAAUSDT', 'new')
    assert result['remaining'] == 0
    assert result['batch_complete']
    assert set(result['succeeded_client_order_ids']) == {'old', 'new'}


@pytest.mark.asyncio
async def test_postgres_repeatable_read_cannot_bypass_concurrent_identity_guard(pg_activity):
    await pg_activity.execute(MIGRATION.read_text())
    async with pg_activity.transaction(isolation='repeatable_read'):
        with pytest.raises(asyncpg.RaiseError, match='read committed'):
            await collect_entry_activity(PGWrapper(pg_activity), client(), 'default')


def test_old_terminal_signed_before_window_needs_no_new_exchange_lookup():
    old_batch = batch(['old'])
    old_batch['payload']['finished_ms'] = 90
    current_batch = batch([], 4)
    row = project([reservation()], [terminal(observation(executed='1'), 90)], [],
                  protocol_facts() + [old_batch, current_batch], [cursor()])
    assert row['coverage_complete'], row
    assert row['owned_entry_fills'] == 0


def test_protocol_without_symbol_universe_or_all_symbol_cursors_cannot_prove_zero():
    facts = protocol_facts()
    facts[-1]['payload']['symbols'] = ['AAAUSDT', 'BBBUSDT']
    row = project([], [], [], facts + [batch([])], [cursor()])
    assert not row['coverage_complete']
    assert row['owned_entry_fills'] is None
    assert 'user_trades_coverage_incomplete' in row['reasons']


def test_protocol_incomplete_symbol_interval_cannot_borrow_adjacent_universe():
    boundary, first = protocol_facts()
    first['payload']['end_ms'] = 200
    second = dict(first, id=4, payload=dict(first['payload'], start_ms=200, end_ms=300,
                                             symbol_universe_complete=False))
    row = project(facts=[boundary, first, second, batch([])], cursors=[cursor()])
    assert not row['coverage_complete']
    assert row['owned_entry_fills'] is None
    assert 'entry_symbol_universe_missing' in row['reasons']


def test_protocol_requires_cursors_only_during_each_certified_symbol_interval():
    boundary, first = protocol_facts()
    first['payload']['end_ms'] = 200
    second = dict(first, id=4, payload=dict(first['payload'], start_ms=200, end_ms=300,
                                             symbols=['BBBUSDT']))
    cursors = [dict(cursor(), covered_until_ms=200),
               dict(cursor(), symbol='BBBUSDT', window_start_ms=200)]
    row = project(facts=[boundary, first, second, batch([])], cursors=cursors)
    assert row['coverage_complete']
    assert row['owned_entry_fills'] == 0


def test_postgres_json_timestamp_with_trimmed_microseconds_keeps_positive_fill():
    legacy = reservation()
    legacy['created_at'] = '1970-01-01T00:00:00.00123+00:00'
    fact = dict(id=9, account_id='default', kind='legacy_intent', payload=json.dumps(legacy))
    row = project([], [observation()], [fill()], [fact])
    assert row['owned_entry_fills'] == 1


@pytest.mark.asyncio
async def test_flat_anchor_collects_only_explicit_unique_zero_with_exchange_bounds():
    from ai_tuner.allocation.activity_evidence import collect_flat_anchors

    db, exchange = DB([]), client()
    exchange._request = AsyncMock(side_effect=[{'serverTime': 100}, {'serverTime': 120}])
    exchange.get_position = AsyncMock(return_value=[
        {'symbol': 'AAAUSDT', 'positionSide': 'BOTH', 'positionAmt': '0'},
        {'symbol': 'BBBUSDT', 'positionSide': 'BOTH', 'positionAmt': '0'},
        {'symbol': 'BBBUSDT', 'positionSide': 'LONG', 'positionAmt': '1'},
    ])
    assert await collect_flat_anchors(db, exchange, 'default') == 1
    fact = json.loads(db.execute.await_args.args[3])
    assert (fact['symbol'], fact['at_ms'], fact['read_until_ms']) == ('AAAUSDT', 100, 120)


def test_flat_observation_cannot_anchor_trade_during_signed_read():
    raw = dict(symbol='AAAUSDT', positionSide='BOTH', positionAmt='0')
    flat = dict(id=10, account_id='default', kind='flat_position', payload=dict(
        symbol='AAAUSDT', at_ms=101, read_until_ms=210, source='signed_pm_position_risk_v1',
        raw_payload=raw, credential_fingerprint='a'*64,
        payload_sha256=hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))
    obs = observation(position_side='BOTH')
    assert project([reservation()], [obs], [fill()], [flat], [cursor()])['owned_entry_fills'] is None
    flat['payload']['read_until_ms'] = 180
    assert project([reservation()], [obs], [fill()], [flat], [cursor()])['owned_entry_fills'] == 1


@pytest.mark.asyncio
async def test_flat_absence_needs_both_signed_inventories_and_one_way_mode():
    from ai_tuner.allocation.activity_evidence import collect_flat_anchors

    db, exchange = DB([reservation()]), client()
    exchange._request = AsyncMock(side_effect=[
        {'serverTime': 100}, {'dualSidePosition': False}, {'serverTime': 120}])
    exchange.get_position = AsyncMock(return_value=[])
    exchange.get_um_account_positions = AsyncMock(return_value=[{'symbol': 'AAAUSDT'}])
    assert await collect_flat_anchors(db, exchange, 'default') == 0

    exchange._request.side_effect = [
        {'serverTime': 130}, {'dualSidePosition': False}, {'serverTime': 150}]
    exchange.get_um_account_positions.return_value = []
    assert await collect_flat_anchors(db, exchange, 'default') == 1
    assert json.loads(db.execute.await_args.args[3])['source'] == 'signed_pm_dual_inventory_v1'


def test_reports_unique_filled_orders_separately_from_trade_fill_count():
    obs = observation()
    row = project([reservation()], [obs], [dict(fill(), qty='1'), dict(fill(trade_id=2), qty='1')])
    assert row['owned_entry_fills'] == 2
    assert row['verified_filled_entry_orders'] == 1


def test_order_seen_under_another_strategy_cannot_verify_one_strategy_subset():
    obs = observation()
    conflicting = observation(client='other')
    row = project([reservation(), reservation(client='other', strategy='btc_eth_aggressive')],
                  [obs, conflicting], [fill()])
    assert row['owned_entry_fills'] is None
    assert 'order_owner_conflict' in row['reasons']


@pytest.mark.asyncio
async def test_postgres_lot_budget_accepts_only_contiguous_ordered_reductions(pg_activity):
    from ai_tuner.monitor.reduced_trial import _COVERED_RESERVATIONS_SQL

    conn = pg_activity
    await conn.execute('ALTER TABLE trading.entry_risk_reservation ADD COLUMN margin numeric')
    await conn.execute(MIGRATION.read_text())
    await conn.execute("""
        INSERT INTO trading.entry_risk_reservation
        (client_order_id,account_id,strategy_id,symbol,quantity,geometry,state,created_at,margin)
        VALUES('live','default','hrs','AAAUSDT',2,'{}','submitting',to_timestamp(1),7);
        INSERT INTO trading.execution_fact_fill VALUES
        ('default','AAAUSDT',1,10,'BUY',2,to_timestamp(2));
        INSERT INTO trading.execution_fact_cursor VALUES
        ('default','userTrades','AAAUSDT',1000,2100,true,'ok'),
        ('default','userTrades','AAAUSDT',2100,3000,true,'ok');
    """)
    signed = dict(observation(client='live')['raw_payload'], status='FILLED',
                  origQty='2', positionSide='BOTH')
    # Insert the precise signed order observation without a network request.
    await conn.execute('SET ROLE trading_user')
    args = ('default', 'hrs', ['AAAUSDT'], 3000)
    from ai_tuner.allocation.activity_evidence import _INSERT_ORDER, _digest
    await conn.execute(_INSERT_ORDER, 'default', 'live', 'AAAUSDT', '10', 'BUY', 'FILLED',
                       2, 2, 'ok', json.dumps(signed), _digest(signed),
                       'a'*64, 'signed_pm_get_order_v1', 'budget-test-batch')
    row = await conn.fetchrow(_COVERED_RESERVATIONS_SQL, *args)
    assert row['cursor_complete'] and row['fill_qty'] == 2 and row['exit_qty'] == 0 and row['margin'] == 7
    await conn.execute('RESET ROLE')
    await conn.execute("""INSERT INTO trading.execution_fact_fill VALUES
        ('default','AAAUSDT',2,20,'SELL',1,to_timestamp(2.5))""")
    reduced = await conn.fetchrow(_COVERED_RESERVATIONS_SQL, *args)
    assert reduced['cursor_complete'] and reduced['fill_qty'] == 2 and reduced['exit_qty'] == 1
    await conn.execute("UPDATE trading.execution_fact_cursor SET window_start_ms=2101 WHERE window_start_ms=2100")
    assert not (await conn.fetchrow(_COVERED_RESERVATIONS_SQL, *args))['cursor_complete']
    await conn.execute("UPDATE trading.execution_fact_fill SET trade_time_utc=to_timestamp(2) WHERE trade_id=2")
    assert (await conn.fetchrow(_COVERED_RESERVATIONS_SQL, *args))['fill_qty'] is None
    await conn.execute("""UPDATE trading.execution_fact_fill
        SET trade_time_utc=to_timestamp(2.5), order_id=NULL WHERE trade_id=2""")
    assert (await conn.fetchrow(_COVERED_RESERVATIONS_SQL, *args))['fill_qty'] is None


def test_overlapping_protocol_declarations_require_coverage_for_both_universes():
    boundary, first = protocol_facts()
    second = dict(first, id=4, payload=dict(first['payload'], symbols=['BBBUSDT']))
    row = project([], [], [], [boundary, first, second, batch([])], [cursor()])
    assert not row['coverage_complete']
    assert row['owned_entry_fills'] is None
    assert 'user_trades_coverage_incomplete' in row['reasons']


def test_explicit_order_identity_conflict_excludes_old_positive_observation():
    old = observation()
    later = dict(old, id=2, outcome='order_identity_conflict', observed_at=stamp(450))
    row = project([reservation()], [old, later], [fill()])
    assert row['owned_entry_fills'] is None
    assert row['verified_filled_entry_orders'] is None
    assert 'order_identity_conflict' in row['reasons']


@pytest.mark.parametrize('defect', [None, 'raw_negative_missing', 'submitting', 'legacy', 'no_protocol', 'wrong_id'])
def test_released_without_submitting_has_a_separate_terminal_proof(defect):
    r = dict(reservation(), quantity='3', created_at=stamp(110), revision=7)
    state = dict(id=60, account_id='default', kind='intent', observed_at=stamp(120),
                 created_by='trading_migration_admin', payload=dict(r, state='reserved'))
    released = dict(state, id=61, kind='intent_state', observed_at=stamp(350), payload=r)
    negative = dict(observation(), order_id=None, raw_payload={}, observed_at=stamp(370),
                    payload_sha256=hashlib.sha256(b'{}').hexdigest(), outcome='order_not_found')
    events, replies = [state, released], [negative]
    facts = protocol_facts()
    facts[-1]['payload']['policy_revision'] = 7
    if defect == 'raw_negative_missing':
        replies = []
    elif defect == 'submitting':
        events.insert(1, dict(state, kind='intent_state', observed_at=stamp(180),
                              payload=dict(r, state='submitting')))
    elif defect == 'legacy':
        state['kind']='legacy_intent'
    elif defect == 'no_protocol':
        facts = facts[:1]
    elif defect == 'wrong_id':
        released['payload']=dict(r, revision=6)
    row = project([r], replies, [], facts + events + [batch([])], [cursor()])
    assert row['coverage_complete'] is (defect is None)
    assert row['owned_entry_fills'] == (0 if defect is None else None)


def test_signed_other_strategy_order_cannot_count_as_current_strategy_open():
    key = ('default', 'AAAUSDT', '1', '10', 200)
    rows = project_activity_evidence([], [], [fill()], [cursor()], protocol_facts()+[batch([])],
                                     'default', 100, 300, ['hrs','new_coin'],
                                     verified_execution_owners={(*key, 'new_coin')})
    assert rows['hrs']['coverage_complete'] and rows['hrs']['owned_entry_fills']==0
    assert rows['new_coin']['owned_entry_fills'] is None
    assert 'owned_fill_without_entry_intent' in rows['new_coin']['reasons']


@pytest.mark.asyncio
async def test_pg_classic_orders_keep_strategy_and_pm_provenance(pg_activity, monkeypatch):
    conn = pg_activity
    await conn.execute(MIGRATION.read_text())
    await conn.execute((MIGRATION.parent / '2026-11-25-classic-entry-observation.sql').read_text())
    await conn.execute("SET ROLE trading_user")
    exchange = client()
    exchange.get_order_by_client_id.side_effect = lambda symbol, cid: observation(client=cid)["raw_payload"]
    first = await collect_entry_activity(PGWrapper(conn), exchange, 'default')
    assert first["observed"] == 1
    cutover = await conn.fetchval("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", str(cutover))
    classic = ReadOnlyBinanceClient("key", "secret", use_unified_account=False)
    classic.entry_activity_account_id = "default"
    strategies = ["btc_eth", "btc_eth_aggressive", "hrs"]
    for strategy in strategies:
        await conn.execute("""INSERT INTO trading.entry_risk_reservation
          VALUES($1,'default',$1,'AAAUSDT',3,'{}','reserved',clock_timestamp())""", strategy)
    classic.get_order_by_client_id = AsyncMock(side_effect=lambda symbol, cid:
        observation(client=cid, order=20 + strategies.index(cid))["raw_payload"])
    result = await collect_entry_activity(PGWrapper(conn), classic, 'default')
    assert result['observed'] == 3 and not result['gaps']
    rows = await conn.fetch("""SELECT r.strategy_id,o.source,o.order_id FROM trading.entry_order_observation o
        JOIN trading.entry_risk_reservation r USING (account_id,client_order_id) ORDER BY o.order_id""")
    assert [tuple(r) for r in rows] == [
        ("hrs", "signed_pm_get_order_v1", "10"),
        *[(s, "signed_classic_get_order_v1", str(20 + i)) for i, s in enumerate(strategies)],
    ]
