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
