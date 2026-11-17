"""Opt-in PostgreSQL checks. Only a disposable local capital_history_tests DB."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest
import pytest_asyncio

from ai_tuner.allocation.capital_evidence import read_capital_evidence
from shared.database import DatabaseManager

DSN = os.environ.get('CAPITAL_HISTORY_TEST_DSN', '')
pytestmark = pytest.mark.skipif(not DSN, reason='Set CAPITAL_HISTORY_TEST_DSN to isolated local capital_history_tests')
if DSN:
    parsed = urlsplit(DSN)
    if parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.path != '/capital_history_tests':
        raise ValueError('Only isolated local capital_history_tests is permitted')
MIGRATION = Path(__file__).parents[2] / 'database/postgres/migrations/2026-11-17-capital-history.sql'


@pytest_asyncio.fixture
async def owner():
    conn = await asyncpg.connect(DSN)
    await conn.execute('DROP SCHEMA IF EXISTS trading CASCADE; CREATE SCHEMA trading; DROP TABLE IF EXISTS public.capital_allocation')
    await conn.execute('''DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='trading_user') THEN
        CREATE ROLE trading_user NOSUPERUSER NOLOGIN; END IF; END $$;
        CREATE TABLE public.capital_allocation(id serial PRIMARY KEY,month varchar UNIQUE,
            total_capital numeric,strategy_count int,is_first_month bool,entries jsonb,
            status varchar,created_at timestamp without time zone);
        GRANT SELECT,INSERT,UPDATE,DELETE ON public.capital_allocation TO trading_user;
        GRANT USAGE,SELECT ON SEQUENCE public.capital_allocation_id_seq TO trading_user;
        INSERT INTO public.capital_allocation(month,entries,status,created_at) VALUES
            ('2026-11','[{"strategy_id":"hrs","allocated_amount":"20"}]','active','2026-08-31');''')
    try:
        yield conn
    finally:
        if conn.is_in_transaction():
            await conn.execute('ROLLBACK')
        await conn.execute('RESET ROLE')
        await conn.close()


class Wrapper:
    def __init__(self, conn):
        self.conn = conn

    async def fetch_all(self, sql, *args):
        DatabaseManager._validate_sql(self, sql)
        return [dict(row) for row in await self.conn.fetch(sql, *args)]


@pytest.mark.asyncio
async def test_baseline_idempotence_normal_runtime_writes_and_history_immutability(owner):
    await owner.execute(MIGRATION.read_text())
    baseline = await owner.fetchrow('SELECT * FROM trading.capital_allocation_history')
    assert baseline['operation'] == 'BASELINE'
    assert baseline['account_id'] == 'default'
    assert baseline['observed_at'].tzinfo is not None
    assert json.loads(baseline['allocations'])[0]['created_at'].startswith('2026-08-31')
    await owner.execute(MIGRATION.read_text())
    assert await owner.fetchval('SELECT count(*) FROM trading.capital_allocation_history') == 1
    await owner.execute('SET ROLE trading_user')
    assert not await owner.fetchval('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
    await owner.execute("UPDATE public.capital_allocation SET entries='[{\"strategy_id\":\"hrs\",\"allocated_amount\":\"40\"}]'")
    assert await owner.fetchval('SELECT count(*) FROM trading.capital_allocation_history') == 2
    for sql in ("UPDATE trading.capital_allocation_history SET operation='DELETE'",
                'DELETE FROM trading.capital_allocation_history',
                'TRUNCATE trading.capital_allocation_history',
                "INSERT INTO trading.capital_allocation_history(account_id,operation,allocations) VALUES('default','UPDATE','[]')"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await owner.execute(sql)
    await owner.execute('RESET ROLE')
    for sql in ("UPDATE trading.capital_allocation_history SET operation='DELETE'",
                'DELETE FROM trading.capital_allocation_history',
                'TRUNCATE trading.capital_allocation_history', 'TRUNCATE public.capital_allocation'):
        with pytest.raises(asyncpg.RaiseError):
            await owner.execute(sql)
    await owner.execute("INSERT INTO public.capital_allocation(month,entries,status) VALUES('2026-12','[]','draft')")
    await owner.execute("DELETE FROM public.capital_allocation WHERE month='2026-12'")
    assert [r['operation'] for r in await owner.fetch('SELECT operation FROM trading.capital_allocation_history ORDER BY id')] == ['BASELINE', 'UPDATE', 'INSERT', 'DELETE']


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['DISABLE TRIGGER capital_history_capture', 'DROP'])
async def test_reader_and_reinstaller_reject_missing_or_disabled_source_audit(owner, damage):
    await owner.execute(MIGRATION.read_text())
    if damage == 'DROP':
        await owner.execute('DROP TRIGGER capital_history_capture ON public.capital_allocation')
    else:
        await owner.execute(f'ALTER TABLE public.capital_allocation {damage}')
    start = await owner.fetchval('SELECT floor(extract(epoch FROM observed_at)*1000)::bigint + 1 FROM trading.capital_allocation_history')
    result = (await read_capital_evidence(Wrapper(owner), 'default', start, start + 1000, ['hrs']))['hrs']
    assert result['average_capital_usdt'] is None
    assert result['reasons'] == ['capital_audit_unhealthy']
    with pytest.raises(asyncpg.RaiseError, match='audit continuity'):
        await owner.execute(MIGRATION.read_text())


@pytest.mark.asyncio
async def test_real_reader_uses_allocated_amount_and_does_not_backdate_baseline(owner):
    await owner.execute(MIGRATION.read_text())
    start = await owner.fetchval('SELECT ceil(extract(epoch FROM observed_at)*1000)::bigint FROM trading.capital_allocation_history')
    await owner.execute('SET ROLE trading_user')
    result = (await read_capital_evidence(Wrapper(owner), 'default', start, start + 1000, ['hrs']))['hrs']
    assert result['coverage_complete'], result
    assert result['average_capital_usdt'] == 20
    result = (await read_capital_evidence(Wrapper(owner), 'default', start - 86400000, start + 1000, ['hrs']))['hrs']
    assert not result['coverage_complete']
    assert 'missing_start_coverage' in result['reasons']


@pytest.mark.asyncio
async def test_concurrent_transactions_preserve_full_snapshot_of_both_committed_changes(owner):
    await owner.execute(MIGRATION.read_text())
    second = await asyncpg.connect(DSN)
    first_tx = owner.transaction()
    await first_tx.start()
    try:
        await owner.execute("UPDATE public.capital_allocation SET total_capital=40 WHERE month='2026-11'")
        task = asyncio.create_task(second.execute("INSERT INTO public.capital_allocation(month,total_capital,entries,status) VALUES('2026-12',80,'[]','draft')"))
        await asyncio.sleep(.05)
        assert not task.done()
        await first_tx.commit()
        await asyncio.wait_for(task, 5)
        snapshots = await owner.fetch('SELECT allocations FROM trading.capital_allocation_history ORDER BY id')
        assert len(snapshots) == 3
        latest = {row['month']: row for row in json.loads(snapshots[-1]['allocations'])}
        assert latest['2026-11']['total_capital'] == 40
        assert latest['2026-12']['total_capital'] == 80
    finally:
        if owner.is_in_transaction():
            await first_tx.rollback()
        await second.close()


@pytest.mark.asyncio
async def test_non_read_committed_writer_is_rejected_instead_of_recording_stale_snapshot(owner):
    await owner.execute(MIGRATION.read_text())
    async with owner.transaction(isolation='repeatable_read'):
        with pytest.raises(asyncpg.RaiseError, match='read committed'):
            await owner.execute('UPDATE public.capital_allocation SET total_capital=100')


@pytest.mark.asyncio
async def test_reader_rejects_history_with_missing_baseline_even_if_source_audit_enabled(owner):
    await owner.execute(MIGRATION.read_text())
    await owner.execute('UPDATE public.capital_allocation SET total_capital=40')
    # Simulate owner-level damage to prove the reader does not accept a truncated
    # audit lineage merely because the current source triggers are present.
    await owner.execute('ALTER TABLE trading.capital_allocation_history DISABLE TRIGGER capital_history_immutable')
    await owner.execute("DELETE FROM trading.capital_allocation_history WHERE operation='BASELINE'")
    await owner.execute('ALTER TABLE trading.capital_allocation_history ENABLE ALWAYS TRIGGER capital_history_immutable')
    start = await owner.fetchval('SELECT ceil(extract(epoch FROM observed_at)*1000)::bigint FROM trading.capital_allocation_history')
    result = (await read_capital_evidence(Wrapper(owner), 'default', start, start + 1000, ['hrs']))['hrs']
    assert result['average_capital_usdt'] is None
    assert result['reasons'] == ['capital_audit_unhealthy']
