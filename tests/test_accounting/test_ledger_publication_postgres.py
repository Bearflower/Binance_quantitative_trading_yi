"""Run only against an explicitly supplied, disposable local ledger_tests DB."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

DSN=os.environ.get('LEDGER_TEST_DSN')
pytestmark=pytest.mark.skipif(not DSN,reason='Set LEDGER_TEST_DSN to local disposable ledger_tests database')


def economic_report(end=2000,amount='5'):
    return dict(account_coverage_complete=True,strategy_attribution_complete=False,
                account_usdt_total=amount,report_id='prepublish',end_ms=end,start_ms=1000,
                account_id='main',report_version='exchange-income-v1',coverage_gaps=[])


@pytest.mark.asyncio
async def test_atomic_publish_rollback_immutable_run_and_concurrent_readers():
    import asyncpg
    from shared.accounting.published_report import read_published_report
    assert 'localhost' in DSN or '127.0.0.1' in DSN
    assert DSN.endswith('/ledger_tests')
    pool=await asyncpg.create_pool(DSN,min_size=1,max_size=5)
    db=SimpleNamespace(pool=pool)
    try:
        async with pool.acquire() as conn:
            await conn.execute('DROP SCHEMA IF EXISTS trading CASCADE; CREATE SCHEMA trading')
            await conn.execute(Path('database/postgres/migrations/2026-11-04-ledger-report-publication.sql').read_text())
            await conn.execute('''CREATE TABLE trading.execution_fact_cursor (
                source text, account_id text, income_type text, symbol text,
                window_start_ms bigint, covered_until_ms bigint, page_complete boolean, request_status text)''')
        with patch('shared.accounting.published_report.read_monthly_economic_report',AsyncMock(return_value=economic_report())):
            first=await read_published_report(db,'main',1000,2000,'month:1000',refresh_seconds=0)
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT count(*) FROM trading.ledger_report_run')==1
            assert await conn.fetchval('SELECT report_id FROM trading.ledger_report_active')==first['report_id']
            with pytest.raises(asyncpg.RaiseError):
                await conn.execute("UPDATE trading.ledger_report_run SET report='{}'")
            await conn.execute('''CREATE FUNCTION trading.fail_activation() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'activation failure'; END $$;
                CREATE TRIGGER fail_activation BEFORE UPDATE ON trading.ledger_report_active
                FOR EACH ROW EXECUTE FUNCTION trading.fail_activation()''')
        with patch('shared.accounting.published_report.read_monthly_economic_report',AsyncMock(return_value=economic_report(3000,'7'))):
            failed=await read_published_report(db,'main',1000,3000,'month:1000',current=True,refresh_seconds=0)
        assert failed['report_id']==first['report_id']
        assert failed['publication_status']=='stale'
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT count(*) FROM trading.ledger_report_run')==1
            assert await conn.fetchval('SELECT report_id FROM trading.ledger_report_active')==first['report_id']
            await conn.execute('DROP TRIGGER fail_activation ON trading.ledger_report_active')
        with patch('shared.accounting.published_report.read_monthly_economic_report',AsyncMock(return_value=economic_report(3000,'7'))):
            results=await asyncio.gather(*(read_published_report(db,'main',1000,3000,'month:1000',current=True,refresh_seconds=0) for _ in range(3)))
        assert all(row['account_coverage_complete'] for row in results)
        async with pool.acquire() as conn:
            active=await conn.fetchval('SELECT end_ms FROM trading.ledger_report_active')
            assert active==3000
            assert await conn.fetchval('SELECT count(*) FROM trading.ledger_report_run')==2
    finally:
        await pool.close()

@pytest.mark.asyncio
async def test_real_reader_current_watermark_funding_and_unknown_historical_owner():
    import asyncpg
    from shared.accounting.published_report import read_published_report
    assert ('localhost' in DSN or '127.0.0.1' in DSN) and DSN.endswith('/ledger_tests')
    pool=await asyncpg.create_pool(DSN,min_size=1,max_size=3)
    try:
        async with pool.acquire() as conn:
            await conn.execute('DROP SCHEMA IF EXISTS trading CASCADE; CREATE SCHEMA trading')
            await conn.execute(Path('database/postgres/migrations/2026-11-04-ledger-report-publication.sql').read_text())
            await conn.execute('''
                CREATE TABLE trading.execution_fact_income(account_id text,tran_id bigint,symbol text,
                    income_type text,income numeric,asset text,trade_id bigint,event_time_utc timestamptz,
                    PRIMARY KEY(account_id,income_type,tran_id));
                CREATE TABLE trading.execution_fact_fill(account_id text,symbol text,trade_id bigint,
                    order_id bigint,commission numeric,commission_asset text,realized_pnl numeric,trade_time_utc timestamptz);
                CREATE TABLE trading.trade_records(account_id text,symbol text,order_id text,strategy text);
                CREATE TABLE trading.reviewed_income_attribution(account_id text,tran_id bigint,strategy text,symbol text,
                    income_type text,income numeric,asset text,event_time_utc timestamptz,evidence text,reviewed_by text);
                INSERT INTO trading.reviewed_income_attribution VALUES ('main',1,'hrs','ETHUSDT','FUNDING_FEE',
                    3.42271362,'USDT',to_timestamp(1.5),'audited funding proof','reviewer');
                CREATE TABLE trading.execution_fact_cursor(source text,account_id text,income_type text,symbol text,
                    window_start_ms bigint,covered_until_ms bigint,page_complete boolean,request_status text);
                INSERT INTO trading.execution_fact_income VALUES
                    ('main',1,'ETHUSDT','REALIZED_PNL',8.28318681,'USDT',NULL,to_timestamp(1.5)),
                    ('main',1,'ETHUSDT','COMMISSION',-5.89624705,'USDT',NULL,to_timestamp(1.5)),
                    ('main',1,'ETHUSDT','FUNDING_FEE',3.42271362,'USDT',NULL,to_timestamp(1.5)),
                    ('main',2,'ETHUSDT','FUNDING_FEE',2,'U',NULL,to_timestamp(1.5));
                INSERT INTO trading.execution_fact_cursor
                    SELECT 'um/income','main',kind,NULL,1000,2000,true,'ok'
                    FROM unnest(ARRAY['REALIZED_PNL','COMMISSION','FUNDING_FEE']) kind;
            ''')
        report=await read_published_report(SimpleNamespace(pool=pool),'main',1000,3000,'daily:1000',current=True)
        assert report['end_ms']==2000
        assert report['account_usdt_total']=='5.80965338'
        assert report['account_components']['funding_usdt']=='3.42271362'
        assert report['other_asset_totals']=={'U':'2'}
        assert report['account_coverage_complete'] is True
        assert report['strategy_attribution_complete'] is False
        assert report['stale_seconds']==1
        assert report['publication_status']=='published'
        assert report['by_strategy']['hrs']['funding_usdt']=='3.42271362'
        # Once the same bucket is historical, an earlier partial run is not a complete month/day.
        historical=await read_published_report(SimpleNamespace(pool=pool),'main',1000,3000,'daily:1000',current=False)
        assert historical['account_coverage_complete'] is False
        assert historical['publication_status']=='unverified'
        assert historical['as_of'] is None
    finally:
        await pool.close()

@pytest.mark.asyncio
async def test_publication_under_non_superuser_application_role():
    import asyncpg
    from shared.accounting.published_report import read_published_report
    assert ('localhost' in DSN or '127.0.0.1' in DSN) and DSN.endswith('/ledger_tests')
    owner=await asyncpg.connect(DSN)
    try:
        await owner.execute("DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='trading_user') THEN CREATE ROLE trading_user NOSUPERUSER NOLOGIN; END IF; END $$")
        await owner.execute('DROP SCHEMA IF EXISTS trading CASCADE; CREATE SCHEMA trading')
        await owner.execute(Path('database/postgres/migrations/2026-11-04-ledger-report-publication.sql').read_text())
        await owner.execute('''CREATE TABLE trading.execution_fact_cursor (
            source text, account_id text, income_type text, symbol text,
            window_start_ms bigint, covered_until_ms bigint, page_complete boolean, request_status text);
            GRANT USAGE ON SCHEMA trading TO trading_user;
            GRANT SELECT ON trading.execution_fact_cursor TO trading_user''')
        async def setup(conn): await conn.execute('SET ROLE trading_user')
        pool=await asyncpg.create_pool(DSN,min_size=1,max_size=2,setup=setup)
        try:
            async with pool.acquire() as conn:
                assert await conn.fetchval('SELECT rolsuper FROM pg_roles WHERE rolname=current_user') is False
            with patch('shared.accounting.published_report.read_monthly_economic_report',AsyncMock(return_value=economic_report())):
                result=await read_published_report(SimpleNamespace(pool=pool),'main',1000,2000,'month:1000')
            assert result['publication_status']=='published'
            async with pool.acquire() as conn:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute("UPDATE trading.ledger_report_run SET report='{}'")
        finally: await pool.close()
    finally: await owner.close()
