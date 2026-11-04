"""Opt-in local allocation_tests only; never reads production environment DSNs."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlsplit
from unittest.mock import AsyncMock
import asyncpg
import pytest
from shared.capital_manager import CapitalManager
from ai_tuner.allocation.pnl_collector import PnLCollector, AllocationInputUnavailable
from test_published_pnl import report,START,END,START_MS,END_MS

DSN=os.environ.get('ALLOCATION_TEST_DSN','')
pytestmark=pytest.mark.skipif(not DSN,reason='ALLOCATION_TEST_DSN unset: explicit local allocation_tests required')
if DSN:
    parsed=urlsplit(DSN)
    if parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.path!='/allocation_tests':
        raise ValueError('Only isolated local allocation_tests is permitted')
ROOT=Path(__file__).parents[2]

async def setup():
    conn=await asyncpg.connect(DSN)
    await conn.execute('CREATE SCHEMA IF NOT EXISTS trading')
    await conn.execute('''CREATE TABLE IF NOT EXISTS trading.position_claims(id bigserial primary key,
       symbol text,strategy text,claim_state text,expires_at timestamptz,released_at timestamptz,reason text)''')
    for name in ('2026-11-04-entry-risk-policy.sql','2026-11-04-ledger-report-publication.sql'):
        await conn.execute((ROOT/'database/postgres/migrations'/name).read_text())
    await conn.execute('TRUNCATE trading.strategy_risk_active,trading.strategy_risk_policy,trading.entry_risk_reservation,trading.strategy_risk_audit CASCADE')
    await conn.execute('TRUNCATE trading.ledger_report_active,trading.ledger_report_run CASCADE')
    await conn.close()
    async def runtime(conn):await conn.execute('SET ROLE trading_user')
    return await asyncpg.create_pool(DSN,min_size=1,max_size=2,setup=runtime)

@pytest.mark.asyncio
async def test_real_published_reader_same_cost_inclusive_report(monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test-account');pool=await setup()
    try:
        body=report();body['by_strategy']['hrs'].update(realized_pnl='2',commission='-3',funding_fee='-.25')
        async with pool.acquire() as conn:
            assert not await conn.fetchval('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
            await conn.execute('INSERT INTO trading.ledger_report_run(report_id,account_id,asset,window_key,report_version,start_ms,end_ms,report) VALUES($1,$2,\'USDT\',$3,$4,$5,$6,$7::jsonb)',body['report_id'],body['account_id'],body['window_key'],body['report_version'],START_MS,END_MS,json.dumps(body))
            await conn.execute('INSERT INTO trading.ledger_report_active VALUES($1,\'USDT\',$2,$3,$4,$5)',body['account_id'],body['window_key'],body['report_version'],body['report_id'],END_MS)
        with pytest.raises(AllocationInputUnavailable) as caught:
            await PnLCollector(NS(pool=pool),[{'strategy_id':'hrs'}]).collect_all_realized_pnl(START,END)
        assert caught.value.reasons==('missing_capital_evidence','missing_activity_evidence')
        assert str(caught.value.verified_net_pnl['hrs'])=='-1.25'
        assert caught.value.report_id==body['report_id']
    finally:await pool.close()

@pytest.mark.asyncio
async def test_real_capped_policy_and_expiry_during_equity_wait(monkeypatch):
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test-account');pool=await setup()
    admin=await asyncpg.connect(DSN)
    try:
        await admin.execute("""INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,mode,multiplier,ratio_cap,entry_ceiling,reason)
            VALUES('test-account','btc_eth',1,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 day','capped',1,.2,100,'test'),
                  ('test-account','btc_eth',2,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '.15 seconds','capped',1,.2,100,'expiry test')""")
        await admin.execute("INSERT INTO trading.strategy_risk_active VALUES('test-account','btc_eth',1)")
        manager=CapitalManager('/not/read',db=NS(pool=pool),strategy_id='btc_eth',entry_equity_provider=AsyncMock(return_value={'totalMarginBalance':400}))
        manager.get_effective_margin_limit=AsyncMock(return_value=(300,'db'))
        assert (await manager.can_open_within_limit(70,10))[0]
        assert not (await manager.can_open_within_limit(70,11))[0]
        async def slow():
            await admin.execute("UPDATE trading.strategy_risk_active SET revision=2")
            await asyncio.sleep(.2)
            return {'totalMarginBalance':1000}
        manager.entry_equity_provider=slow
        assert not (await manager.can_open_within_limit(0,1))[0]
        assert await manager.get_effective_margin_limit()==(300,'db')
    finally:await admin.close();await pool.close()
