"""Actual PostgreSQL, actual DatabaseManager, isolated ingestion_tests only."""
import asyncio
import copy
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from types import SimpleNamespace
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

from shared.database import DatabaseManager
from shared.execution_facts_sink import PostgresExecutionFactsSink
from scripts.export_execution_facts import _parse_income_row, ExecutionFactExporter

DSN=os.environ.get('INGESTION_TEST_DSN','')
pytestmark=pytest.mark.skipif(not DSN,reason='INGESTION_TEST_DSN unset: explicit local ingestion_tests required')
ROOT=Path(__file__).resolve().parents[2]


def fact(tran=1,kind='REALIZED_PNL',amount='5',**overrides):
    row=dict(tranId=tran,incomeType=kind,symbol='ETHUSDT',income=amount,
             asset='USDT',info='settled',tradeId='11',time=1500)
    row.update(overrides)
    return _parse_income_row('main',row)


@pytest_asyncio.fixture
async def pg():
    url=urlparse(DSN)
    assert url.hostname in {'localhost','127.0.0.1'} and url.path=='/ingestion_tests'
    owner=await asyncpg.connect(DSN)
    await owner.execute('DROP SCHEMA IF EXISTS trading CASCADE; CREATE SCHEMA trading')
    await owner.execute((ROOT/'database/postgres/init-scripts/11-execution-facts.sql').read_text())
    await owner.execute('''GRANT USAGE ON SCHEMA trading TO trading_user;
        GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA trading TO trading_user;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA trading TO trading_user''')
    migration=ROOT/'database/postgres/migrations/2026-11-04-income-immutability.sql'
    if migration.exists(): await owner.execute(migration.read_text())
    async def setup(conn): await conn.execute('SET ROLE trading_user')
    pool=await asyncpg.create_pool(DSN,min_size=1,max_size=1,setup=setup)
    db=DatabaseManager(url.hostname,url.port,url.path[1:],url.username,'',min_pool_size=1,max_pool_size=1)
    db.pool=pool
    yield SimpleNamespace(owner=owner,db=db,pool=pool,sink=PostgresExecutionFactsSink(db))
    await pool.close()
    await owner.close()


@pytest.mark.asyncio
async def test_identical_retry_keeps_first_fact_raw_and_fetch_time(pg):
    first=fact()
    await pg.sink.upsert_income([first])
    old_fact=dict(await pg.owner.fetchrow('SELECT * FROM trading.execution_fact_income'))
    old_raw=dict(await pg.owner.fetchrow('SELECT * FROM trading.execution_fact_raw'))
    await pg.sink.upsert_income([fact(amount='5.000',tranId='1',time='1500')])
    assert dict(await pg.owner.fetchrow('SELECT * FROM trading.execution_fact_income'))==old_fact
    assert dict(await pg.owner.fetchrow('SELECT * FROM trading.execution_fact_raw'))==old_raw


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('income','6'),('symbol','BTCUSDT'),('asset','BNB'),('info','changed'),('tradeId','12'),('time',1600)])
async def test_conflicting_fact_rolls_back_page_and_persists_evidence(pg,field,value):
    await pg.sink.upsert_income([fact()])
    before=dict(await pg.owner.fetchrow('SELECT * FROM trading.execution_fact_income'))
    with pytest.raises(Exception) as caught:
        await pg.sink.upsert_income([fact(tran=2),fact(**{field:value})])
    assert caught.value.sqlstate=='P7501'
    assert dict(await pg.owner.fetchrow('SELECT * FROM trading.execution_fact_income'))==before
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_raw')==1
    proof=await pg.owner.fetchrow('SELECT * FROM trading.income_ingestion_conflict')
    assert proof and json.loads(proof['evidence'])['old'] and json.loads(proof['evidence'])['new']
    assert await pg.owner.fetchval("SELECT count(*) FROM trading.income_ingestion_attempt WHERE state='conflicted'")==1


@pytest.mark.asyncio
async def test_cross_type_same_id_coexists(pg):
    await pg.sink.upsert_income([fact(),fact(kind='COMMISSION',amount='-0.2')])
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_income')==2
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_raw')==2


def complete_report():
    return dict(account_id='main',start_ms=1000,end_ms=2000,report_version='exchange-income-v1',
                report_id='candidate',account_coverage_complete=True,strategy_attribution_complete=False,
                account_usdt_total='5',coverage_gaps=[])


async def publish(pg,db=None):
    from shared.accounting.published_report import read_published_report
    with patch('shared.accounting.published_report.read_monthly_economic_report',return_value=complete_report()):
        return await read_published_report(db or pg.db,'main',1000,2000,'daily:1000')


async def enable_publication(pg):
    await pg.owner.execute((ROOT/'database/postgres/migrations/2026-11-04-ledger-report-publication.sql').read_text())


@pytest.mark.asyncio
async def test_fresh_cached_report_blocked_and_later_good_cursor_cannot_clear(pg):
    from shared.accounting.income_integrity import IncomeIntegrityBlocked
    await enable_publication(pg)
    prior=await publish(pg)
    await pg.sink.upsert_income([fact()])
    with pytest.raises(Exception): await pg.sink.upsert_income([fact(amount='9')])
    await pg.sink.upsert_income([fact(tran=2)])
    result=await publish(pg)
    assert result['report_id']==prior['report_id'] and result['publication_status']=='stale'
    assert result['refresh_error']=='IncomeIntegrityBlocked'
    await pg.owner.execute('DELETE FROM trading.ledger_report_active')
    with pytest.raises(IncomeIntegrityBlocked): await publish(pg)


@pytest.mark.asyncio
async def test_actual_exporter_preserves_same_key_contradiction_and_cursor(pg):
    class Client:
        async def get_income_history(self,**kwargs): return [fact().raw_payload,fact(amount='9').raw_payload]
    exporter=ExecutionFactExporter(account_id='main',api_client=Client(),sink=pg.sink)
    result=await exporter.export_income(1000,2000)
    assert result.errors and result.page_complete is False
    assert len(result.records)==2
    assert result.cursor.covered_until_ms==1000 and result.cursor.last_event_id is None
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_income')==0
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_raw')==0
    assert await pg.owner.fetchval('SELECT page_complete FROM trading.execution_fact_cursor') is False
    proof=json.loads(await pg.owner.fetchval('SELECT evidence FROM trading.income_ingestion_conflict'))
    assert proof['old']['income']==5 and proof['new']['income']==9
    assert proof['old_raw']['raw_payload']['income']=='5'


@pytest.mark.asyncio
async def test_substantive_unknown_raw_change_conflicts(pg):
    await pg.sink.upsert_income([fact(extra={'nested':[1,True]})])
    with pytest.raises(Exception) as caught:
        await pg.sink.upsert_income([fact(tran=2),fact(extra={'nested':[1,False]})])
    assert caught.value.sqlstate=='P7502'
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_income')==1
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_raw')==1


@pytest.mark.asyncio
async def test_audit_failure_retains_pending_and_blocks_publication(pg):
    from shared.accounting.income_integrity import IncomeIntegrityBlocked
    await enable_publication(pg)
    await pg.sink.upsert_income([fact()])
    await pg.owner.execute("""CREATE FUNCTION trading.fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit failed'; END $$;
        CREATE TRIGGER fail_audit BEFORE INSERT ON trading.income_ingestion_conflict FOR EACH ROW EXECUTE FUNCTION trading.fail_audit()""")
    with pytest.raises(Exception,match='audit failed'): await pg.sink.upsert_income([fact(amount='9')])
    assert await pg.owner.fetchval("SELECT count(*) FROM trading.income_ingestion_attempt WHERE state='pending'")==1
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.income_ingestion_conflict')==0
    with pytest.raises(IncomeIntegrityBlocked): await publish(pg)


@pytest.mark.asyncio
async def test_cancellation_after_pending_releases_lock_but_keeps_gate(pg):
    from shared.accounting.income_integrity import IncomeIntegrityBlocked
    await enable_publication(pg)
    real=pg.db.execute_transaction
    async def cancel(queries,**kwargs):
        if queries[0][0].startswith('INSERT INTO trading.execution_fact_income '): raise asyncio.CancelledError()
        return await real(queries,**kwargs)
    with patch.object(pg.db,'execute_transaction',side_effect=cancel):
        with pytest.raises(asyncio.CancelledError): await pg.sink.upsert_income([fact()])
    assert await pg.owner.fetchval("SELECT count(*) FROM trading.income_ingestion_attempt WHERE state='pending'")==1
    assert await pg.owner.fetchval("SELECT pg_try_advisory_lock(hashtextextended('income-integrity-v1:main',0))") is True
    await pg.owner.execute('SELECT pg_advisory_unlock_all()')
    with pytest.raises(IncomeIntegrityBlocked): await publish(pg)

@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_in_gap',[False,True])
async def test_publisher_cannot_enter_rollback_to_quarantine_gap(pg,cancel_in_gap):
    await enable_publication(pg);prior=await publish(pg)
    await pg.sink.upsert_income([fact()])
    entered=asyncio.Event();release=asyncio.Event();real=pg.db.execute_transaction
    async def pause(queries,**kwargs):
        if queries[0][0].startswith('INSERT INTO trading.income_ingestion_conflict'):
            entered.set();await release.wait()
        return await real(queries,**kwargs)
    other=await asyncpg.create_pool(DSN,min_size=1,max_size=1)
    try:
        with patch.object(pg.db,'execute_transaction',side_effect=pause):
            writer=asyncio.create_task(pg.sink.upsert_income([fact(amount='9')]))
            await asyncio.wait_for(entered.wait(),2)
            # The failed page has rolled back, but its committed pending latch exists.
            assert await pg.owner.fetchval("SELECT count(*) FROM trading.income_ingestion_attempt WHERE state='pending'")==1
            reader=asyncio.create_task(publish(pg,SimpleNamespace(pool=other)))
            await asyncio.sleep(.05);assert not reader.done()
            if cancel_in_gap:writer.cancel()
            else:release.set()
            with pytest.raises(BaseException):await writer
            result=await asyncio.wait_for(reader,2)
            assert result['report_id']==prior['report_id']
            assert result['publication_status']=='stale'
            assert result['refresh_error']=='IncomeIntegrityBlocked'
            state='pending' if cancel_in_gap else 'conflicted'
            assert await pg.owner.fetchval('SELECT count(*) FROM trading.income_ingestion_attempt WHERE state=$1',state)==1
            assert await pg.owner.fetchval('SELECT income FROM trading.execution_fact_income')==5
    finally:await other.close()

@pytest.mark.asyncio
async def test_concurrent_same_key_different_observations_never_last_writer_wins(pg):
    other=await asyncpg.create_pool(DSN,min_size=1,max_size=1)
    db=DatabaseManager('127.0.0.1',55439,'ingestion_tests','yl','');db.pool=other
    try:
        outcomes=await asyncio.gather(pg.sink.upsert_income([fact()]),PostgresExecutionFactsSink(db).upsert_income([fact(amount='9')]),return_exceptions=True)
        assert sum(isinstance(value,Exception) for value in outcomes)==1
        assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_income')==1
        assert await pg.owner.fetchval('SELECT count(*) FROM trading.income_ingestion_conflict')==1
        assert await pg.owner.fetchval("SELECT count(*) FROM trading.income_ingestion_attempt WHERE state='conflicted'")==1
    finally:await other.close()

@pytest.mark.asyncio
async def test_migration_rerun_role_and_immutable_audit(pg):
    await pg.owner.execute((ROOT/'database/postgres/migrations/2026-11-04-income-immutability.sql').read_text())
    await pg.sink.upsert_income([fact()])
    async with pg.pool.acquire() as conn:
        assert not await conn.fetchval('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
        for sql in ["DELETE FROM trading.execution_fact_income","DELETE FROM trading.execution_fact_raw",
                    "DELETE FROM trading.income_ingestion_attempt", "UPDATE trading.income_ingestion_attempt SET observations='{}'",
                    "UPDATE trading.income_ingestion_attempt SET state='pending'", "DELETE FROM trading.income_ingestion_attempt_event"]:
            with pytest.raises(Exception):await conn.execute(sql)
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.income_ingestion_attempt_event')==2

@pytest.mark.asyncio
async def test_pending_insert_failure_never_writes_facts(pg):
    await pg.owner.execute("REVOKE INSERT ON trading.income_ingestion_attempt FROM trading_user")
    with pytest.raises(Exception):await pg.sink.upsert_income([fact()])
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_income')==0

@pytest.mark.asyncio
async def test_same_tran_different_account_independent_gate(pg):
    from dataclasses import replace
    await pg.sink.upsert_income([fact()])
    with pytest.raises(Exception):await pg.sink.upsert_income([fact(amount='9')])
    await pg.sink.upsert_income([replace(fact(),account_id='other')])
    from shared.accounting.income_integrity import require_income_integrity
    async with pg.pool.acquire() as conn:await require_income_integrity(conn,'other')
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_income')==2

@pytest.mark.asyncio
async def test_nonconflict_database_failure_retains_pending_not_economic_conflict(pg):
    real=pg.db.execute_transaction
    async def fail(queries,**kwargs):
        if queries[0][0].startswith('INSERT INTO trading.execution_fact_income '):raise ConnectionError('transport failure')
        return await real(queries,**kwargs)
    with patch.object(pg.db,'execute_transaction',side_effect=fail):
        with pytest.raises(ConnectionError):await pg.sink.upsert_income([fact()])
    assert await pg.owner.fetchval("SELECT count(*) FROM trading.income_ingestion_attempt WHERE state='pending'")==1
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.income_ingestion_conflict')==0

@pytest.mark.asyncio
async def test_connection_loss_after_durable_pending_survives_new_pool(pg):
    from shared.accounting.income_integrity import IncomeIntegrityBlocked
    await enable_publication(pg);real=pg.db.execute_transaction
    async def crash(queries,**kwargs):
        result=await real(queries,**kwargs)
        if queries[0][0].startswith('INSERT INTO trading.income_ingestion_attempt '):kwargs['connection'].terminate()
        return result
    with patch.object(pg.db,'execute_transaction',side_effect=crash):
        with pytest.raises(Exception):await pg.sink.upsert_income([fact()])
    assert await pg.owner.fetchval("SELECT count(*) FROM trading.income_ingestion_attempt WHERE state='pending'")==1
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.execution_fact_income')==0
    with pytest.raises(IncomeIntegrityBlocked):await publish(pg)

@pytest.mark.asyncio
async def test_raw_conflict_evidence_and_attempt_events_cannot_be_overwritten(pg):
    await pg.sink.upsert_income([fact(extra={'x':1})])
    with pytest.raises(Exception):await pg.sink.upsert_income([fact(extra={'x':2})])
    async with pg.pool.acquire() as conn:
        for sql in ["DELETE FROM trading.income_ingestion_conflict", "UPDATE trading.income_ingestion_conflict SET evidence='{}'", "INSERT INTO trading.income_ingestion_attempt(attempt_id,account_id,observations,state) VALUES('illegal','main','[]','complete')"]:
            with pytest.raises(Exception):await conn.execute(sql)
    assert await pg.owner.fetchval('SELECT count(*) FROM trading.income_ingestion_conflict')==1

@pytest.mark.asyncio
async def test_exact_semantic_exporter_retry_collapses_without_erasing_first_raw(pg):
    class Client:
        async def get_income_history(self,**kwargs):return [fact().raw_payload,fact(amount='5.000').raw_payload]
    exporter=ExecutionFactExporter(account_id='main',api_client=Client(),sink=pg.sink)
    result=await exporter.export_income(1000,2000)
    assert not result.errors and result.page_complete and len(result.records)==1
    assert json.loads(await pg.owner.fetchval('SELECT raw_payload FROM trading.execution_fact_raw'))['income']=='5'
