"""Production-shaped ACL tests on a genuinely restricted PostgreSQL role.

Use an isolated administrator DSN: each test creates/drops its own UTF8 database.
Every application pool lease executes SET ROLE trading_user and checks NOSUPERUSER.
Only the exchange GET responses are simulated; collector/exporter/sink/locks,
real income immutability/audit triggers, migrations and repository SQL are real.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import pytest_asyncio

from shared.database import DatabaseManager
from strategies.hrs.equity_service import HrsEquityService

DSN = os.environ.get('HRS_TEST_POSTGRES_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='isolated PostgreSQL administrator DSN required')
MIGRATIONS = tuple(Path('database/postgres/migrations') / ('2026-11-17-' + name + '.sql') for name in (
    'hrs-risk-ledger-loss-gates', 'hrs-equity-service', 'hrs-allocation-event-audit',
    'hrs-equity-reconciliation', 'hrs-funding-publication'))
SERVICE_MIGRATION = MIGRATIONS[1]


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    import asyncpg
    control = await asyncpg.connect(DSN)
    name = 'hrs_runtime_acl_' + uuid.uuid4().hex
    if not await control.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user')"):
        await control.execute('CREATE ROLE trading_user NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION')
    assert await control.fetchval("SELECT NOT rolsuper FROM pg_roles WHERE rolname='trading_user'")
    await control.execute(f"CREATE DATABASE {name} TEMPLATE template0 ENCODING 'UTF8'")
    admin = None
    pool = None
    service = None
    try:
        admin = await asyncpg.connect(DSN, database=name)
        await admin.execute('CREATE SCHEMA trading; CREATE SCHEMA btc_eth; CREATE SCHEMA new_coin; CREATE SCHEMA hrs')
        await admin.execute(Path('database/postgres/init-scripts/11-execution-facts.sql').read_text())
        await admin.execute(Path('database/postgres/migrations/2026-11-04-income-immutability.sql').read_text())
        await admin.execute('''
            CREATE TABLE trading.trade_records(id bigserial PRIMARY KEY,account_id text,symbol text,order_id text,
                strategy text,side text,status text,order_type text,quantity numeric,realized_pnl numeric,executed_at timestamp);
            CREATE TABLE public.capital_allocation(id serial PRIMARY KEY,month text,entries jsonb,status text,created_at timestamp);
            CREATE TABLE trading.strategy_risk_policy(account_id text,strategy_id text,revision bigint,effective_at timestamptz,expires_at timestamptz,PRIMARY KEY(account_id,strategy_id,revision));
            CREATE TABLE trading.strategy_risk_active(account_id text,strategy_id text,revision bigint,PRIMARY KEY(account_id,strategy_id));
            CREATE TABLE trading.entry_risk_reservation(client_order_id text PRIMARY KEY,account_id text,strategy_id text,state text,symbol text,quantity numeric,updated_at timestamptz);
            CREATE TABLE public.ai_tuner_funding_schedule_observations(attempt_id uuid,symbol text,source_time timestamptz,received_at timestamptz,next_funding_time timestamptz,payload_hash text,raw_payload jsonb);
            GRANT USAGE ON SCHEMA trading,public,btc_eth,new_coin,hrs TO trading_user;
            GRANT SELECT ON trading.trade_records,trading.strategy_risk_policy,trading.strategy_risk_active,trading.entry_risk_reservation,public.ai_tuner_funding_schedule_observations TO trading_user;
            GRANT SELECT,INSERT,UPDATE,DELETE ON public.capital_allocation TO trading_user;
            GRANT USAGE,SELECT ON SEQUENCE public.capital_allocation_id_seq TO trading_user;
        ''')
        for migration in MIGRATIONS:
            await admin.execute(migration.read_text())
            await admin.execute(migration.read_text())
        # Keep synthetic source observations after actual audit installation.
        point = (datetime.now(timezone.utc).replace(day=28) + timedelta(days=4)).replace(day=17,hour=15,minute=0,second=0,microsecond=0)
        account = 'runtime-acl-' + uuid.uuid4().hex
        monkeypatch.setenv('TRADE_ACCOUNT_ID', account)
        monkeypatch.setenv('HRS_EQUITY_ALLOW_INCEPTION', 'true')
        await admin.execute("INSERT INTO public.capital_allocation(month,entries,status,created_at) VALUES($1,'[{\"strategy_id\":\"hrs\",\"allocated_amount\":\"1000\"}]','active',$2)",point.strftime('%Y-%m'),point.replace(tzinfo=None))
        await admin.execute("INSERT INTO trading.strategy_risk_policy VALUES($1,'hrs',1,$2,$3)",account,point-timedelta(days=1),point+timedelta(days=2))
        await admin.execute("INSERT INTO trading.strategy_risk_active VALUES($1,'hrs',1)",account)
        for identity,side,at in ((1,'BUY',point-timedelta(hours=1)),(2,'SELL',point-timedelta(minutes=30))):
            await admin.execute("INSERT INTO trading.trade_records(account_id,symbol,order_id,strategy,side,status,order_type,executed_at) VALUES($1,'BTCUSDT',$2,'hrs',$3,'FILLED','LIMIT',$4)",account,str(identity),side,at.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
        async def app_role(connection):
            await connection.execute('SET ROLE trading_user')
            assert await connection.fetchval("SELECT current_user='trading_user' AND NOT rolsuper FROM pg_roles WHERE rolname=current_user")
        pool = await asyncpg.create_pool(DSN, database=name, min_size=1, max_size=4, setup=app_role)
        db = DatabaseManager('unused',1,'unused','unused','',1,4)
        db.pool = pool
        api = AsyncMock()
        api.use_unified_account = True
        api.get_position.return_value = []
        api.get_um_account_positions.return_value = []
        api.get_open_orders.return_value = []
        records = [dict(symbol='BTCUSDT',id=1,orderId=1,side='BUY',qty='1',price='100',quoteQty='100',commission='.1',commissionAsset='USDT',realizedPnl='0',time=int((point-timedelta(hours=1)).timestamp()*1000)),
                   dict(symbol='BTCUSDT',id=2,orderId=2,side='SELL',qty='1',price='100',quoteQty='100',commission='0',commissionAsset='USDT',realizedPnl='0',time=int((point-timedelta(minutes=30)).timestamp()*1000))]
        api.get_user_trades.side_effect = lambda symbol,**kw: [r for r in records if (kw.get('start_time') is None or kw['start_time']<=r['time']) and (kw.get('end_time') is None or r['time']<=kw['end_time'])]
        api.get_income_history.side_effect = lambda **kw: ([dict(symbol='BTCUSDT',incomeType='COMMISSION',income='-.1',asset='USDT',tranId=100,tradeId='1',time=records[0]['time'],info='')]
            if kw['income_type']=='COMMISSION' and kw['start_time']<=records[0]['time']<=kw['end_time'] else [])
        api.get_funding_rate_history.return_value = []
        def market(method,path,**kwargs):
            assert method=='GET'
            if path=='/fapi/v1/fundingInfo':return []
            if path=='/fapi/v1/exchangeInfo':return dict(symbols=[dict(symbol='BTCUSDT',underlyingType='COIN',contractType='PERPETUAL',quoteAsset='USDT',marginAsset='USDT')])
            raise AssertionError(path)
        api._request.side_effect = market
        service = HrsEquityService(db,api,SimpleNamespace(strategy_id='hrs'),SimpleNamespace(get_all_positions=lambda:{}))
        clock = [point]
        service.clock = lambda:clock[0]
        yield SimpleNamespace(service=service,pool=pool,admin=admin,clock=clock,account=account,db=db,api=api)
    finally:
        if service is not None:await service.close()
        if pool is not None:await pool.close()
        if admin is not None:await admin.close()
        await control.execute(f'DROP DATABASE {name} WITH (FORCE)')
        await control.close()


@pytest.mark.asyncio
async def test_restricted_collector_initializes_and_restarts_after_idempotent_migration(runtime):
    state = runtime
    first = await state.service.refresh()
    assert first['reasons']==['daily_origin_missing']
    assert first['ledger_version']==1
    async with state.pool.acquire() as app:
        assert await app.fetchval('SELECT current_user')=='trading_user'
        assert not await app.fetchval("SELECT has_table_privilege(current_user,'trading.hrs_equity_segment','UPDATE')")
        assert await app.fetchval("SELECT has_column_privilege(current_user,'trading.hrs_equity_segment','account_id','UPDATE')")
        assert not await app.fetchval("SELECT has_column_privilege(current_user,'trading.hrs_equity_segment','opening_cash','UPDATE')")
        assert await app.fetchval('SELECT opening_cash FROM trading.hrs_equity_segment WHERE account_id=$1',state.account)==Decimal('999.9')
        assert await app.fetchval('SELECT count(*) FROM trading.execution_fact_income WHERE account_id=$1',state.account)==1
        assert await app.fetchval("SELECT count(*) FROM trading.income_ingestion_attempt WHERE account_id=$1 AND state<>'complete'",state.account)==0
        assert await app.fetchval('SELECT count(*) FROM trading.income_ingestion_attempt_event')>0
        for sequence in ('trading.hrs_allocation_event_event_id_seq','trading.income_ingestion_attempt_event_event_id_seq'):
            assert not await app.fetchval("SELECT has_sequence_privilege(current_user,$1,'USAGE')",sequence)
        await app.fetch('SELECT * FROM public.ai_tuner_funding_schedule_observations WHERE false')
        assert not await app.fetchval("SELECT has_table_privilege(current_user,'public.ai_tuner_funding_schedule_observations','INSERT')")
        # Capital writes also use their existing SECURITY DEFINER audit trigger;
        # its private identity sequence must remain inaccessible to the caller.
        transaction=app.transaction()
        await transaction.start()
        try:
            previous=await app.fetchval('SELECT max(event_id) FROM trading.hrs_allocation_event')
            await app.execute("UPDATE public.capital_allocation SET entries='[{\"strategy_id\":\"hrs\",\"allocated_amount\":\"1001\"}]'::jsonb")
            assert await app.fetchval('SELECT max(event_id) FROM trading.hrs_allocation_event')>previous
        finally:
            await transaction.rollback()
    # Re-running the old release's migration order must preserve the new grants
    # and immutable initial segment rather than silently recreating its state.
    for migration in MIGRATIONS:
        await state.admin.execute(migration.read_text())
    restarted=HrsEquityService(state.db,state.api,state.service.capital_mgr,state.service.position_manager)
    restarted.clock=state.service.clock
    state.clock[0]+=timedelta(seconds=30)
    result=await restarted.refresh()
    assert result['reasons']==['daily_origin_missing'] and result['ledger_version']==2
    async with state.pool.acquire() as app:
        assert await app.fetchval('SELECT count(*) FROM trading.hrs_equity_segment WHERE account_id=$1',state.account)==1
        assert await app.fetchval('SELECT count(*) FROM trading.execution_fact_income WHERE account_id=$1',state.account)==1
    await restarted.close()


@pytest.mark.asyncio
async def test_lock_column_grant_cannot_modify_or_reseed_segment(runtime):
    import asyncpg
    state=runtime
    result=await state.service.refresh()
    assert result['ledger_version']==1
    async with state.pool.acquire() as app:
        async with app.transaction():
            await app.fetchrow('SELECT * FROM trading.hrs_equity_segment WHERE account_id=$1 FOR UPDATE',state.account)
        with pytest.raises(asyncpg.RaiseError,match='immutable'):
            await app.execute('UPDATE trading.hrs_equity_segment SET account_id=account_id WHERE account_id=$1',state.account)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute('UPDATE trading.hrs_equity_segment SET opening_cash=opening_cash WHERE account_id=$1',state.account)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute('DELETE FROM trading.hrs_equity_segment WHERE account_id=$1',state.account)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute('TRUNCATE trading.hrs_equity_segment')
    # The migration owner also encounters the explicit immutable guard, so the
    # added lock privilege is never an application-level mutation path.
    with pytest.raises(asyncpg.RaiseError,match='immutable'):
        await state.admin.execute('DELETE FROM trading.hrs_equity_segment WHERE account_id=$1',state.account)


@pytest.mark.asyncio
async def test_forecast_prefix_reference_refresh_restart_under_actual_nosuperuser(runtime):
    import json
    from strategies.hrs.equity_evidence import canonical
    from strategies.hrs.funding_reference import resolve_evidence_forecasts,COUNT_SQL,ROWS_SQL
    state=runtime
    at=state.clock[0]
    async def observe(instant):
        payload=dict(forecasts=[dict(symbol='BTCUSDT',source_time=instant,received_at=instant,
            next_funding_time=instant+timedelta(hours=1),source='authenticated_premium_index')])
        encoded,digest=canonical(payload)
        async with state.pool.acquire() as app:
            assert not await app.fetchval('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
            await app.execute('INSERT INTO trading.hrs_funding_observation VALUES($1,$2,$3,$4::jsonb)',state.account,digest,instant,encoded)
    await observe(at)
    first=await state.service.refresh()
    assert first['ledger_version']==1 and first['reasons']==['daily_origin_missing']
    async with state.pool.acquire() as app:
        before=dict(await app.fetchrow('SELECT account_id,evidence_sha256,payload FROM trading.hrs_equity_evidence WHERE evidence_sha256=$1',first['evidence_sha256']))
        payload=json.loads(before['payload'])
        publication=payload['capture']['funding_publication']
        assert 'forecasts' not in publication
        assert publication['forecasts_ref']['row_count']==1
        cutoff=datetime.fromisoformat(publication['forecasts_ref']['available_through'])
        rows=await app.fetch(ROWS_SQL,state.account,cutoff)
        expected=await app.fetchval(COUNT_SQL,state.account,cutoff)
        assert len(resolve_evidence_forecasts(before,rows,account_id=state.account,expected_count=expected))==1
    state.clock[0]+=timedelta(seconds=30)
    await observe(state.clock[0])
    restarted=HrsEquityService(state.db,state.api,state.service.capital_mgr,state.service.position_manager)
    restarted.clock=state.service.clock
    second=await restarted.refresh()
    assert second['ledger_version']==2 and second['reasons']==['daily_origin_missing']
    async with state.pool.acquire() as app:
        current=json.loads(await app.fetchval('SELECT payload FROM trading.hrs_equity_evidence WHERE evidence_sha256=$1',second['evidence_sha256']))
        new_ref=current['capture']['funding_publication']['forecasts_ref']
        old_ref=payload['capture']['funding_publication']['forecasts_ref']
        assert new_ref['row_count']==new_ref['forecast_count']==2
        assert len(canonical(new_ref)[0])<len(canonical(old_ref)[0])+20
        assert await app.fetchval('SELECT payload FROM trading.hrs_equity_evidence WHERE evidence_sha256=$1',first['evidence_sha256'])==before['payload']
        assert canonical(json.loads(before['payload']))[1]==before['evidence_sha256']
        # This role still cannot discard the source rows referred to by evidence.
        import asyncpg
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute('DELETE FROM trading.hrs_funding_observation WHERE account_id=$1',state.account)
    await restarted.close()


@pytest.mark.asyncio
async def test_release_reader_has_only_select_access_to_forecast_reference_closure(runtime):
    import asyncpg
    from strategies.hrs.equity_evidence import canonical
    from strategies.hrs.funding_reference import read_forecasts_prefix
    state=runtime
    manifest=json.loads(Path('database/postgres/permissions.json').read_text())
    tables=[rule['schema']+'.'+rule['name'] for rule in manifest['objects']
            if rule['grants'].get('hrs_acceptance_reader')=='read']
    role='hrs_ref_reader_'+uuid.uuid4().hex
    reader=None
    await state.admin.execute(f'CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
    try:
        for table in tables:
            await state.admin.execute(f'CREATE TABLE IF NOT EXISTS {table}(id integer)')
        database=await state.admin.fetchval('SELECT current_database()')
        for schema in manifest['roles']['hrs_acceptance_reader']['schemas']:
            await state.admin.execute(f'GRANT USAGE ON SCHEMA {schema} TO {role}')
        for table in tables:
            await state.admin.execute(f'GRANT SELECT ON {table} TO {role}')
        payload=dict(forecasts=[dict(symbol='BTCUSDT',source_time=state.clock[0],received_at=state.clock[0],
            next_funding_time=state.clock[0]+timedelta(hours=1),source='authenticated_premium_index')])
        encoded,digest=canonical(payload)
        async with state.pool.acquire() as app:
            await app.execute('INSERT INTO trading.hrs_funding_observation VALUES($1,$2,$3,$4::jsonb)',state.account,digest,state.clock[0],encoded)
        reader=await asyncpg.connect(DSN,database=database)
        await reader.execute(f'SET ROLE {role}')
        assert not await reader.fetchval('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
        for action in ('INSERT','UPDATE','DELETE','TRUNCATE'):
            assert not await reader.fetchval('SELECT has_table_privilege(current_user,$1,$2)','trading.hrs_funding_observation',action)
        async with reader.transaction(readonly=True,isolation='repeatable_read'):
            ref,forecasts=await read_forecasts_prefix(reader,state.account,state.clock[0])
            assert ref['row_count']==ref['forecast_count']==len(forecasts)==1
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await reader.execute('DELETE FROM trading.hrs_funding_observation WHERE account_id=$1',state.account)
    finally:
        if reader is not None:await reader.close()
        await state.admin.execute(f'DROP OWNED BY {role}')
        await state.admin.execute(f'DROP ROLE {role}')


@pytest.mark.asyncio
@pytest.mark.parametrize(('classic_owner','preperiod_entry'), [('hrs',False),('new_coin',False),('hrs',True)])
async def test_same_hrs_nav_segment_and_loss_gate_continue_across_cutover(runtime, monkeypatch, classic_owner, preperiod_entry):
    state = runtime
    at = state.clock[0]
    cutover = int(at.timestamp()*1000) + 1
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS',str(cutover))
    await state.admin.execute("UPDATE trading.trade_records SET account_id=NULL WHERE account_id=$1 AND order_id='1'",state.account)
    entry_at = at - timedelta(hours=1)
    if preperiod_entry:
        entry_at = at.replace(day=1) - timedelta(days=2)
        closed_at = at - timedelta(minutes=30)
        rows = [dict(symbol='BTCUSDT',id=identity,orderId=identity,side=side,qty='1',price='100',quoteQty='100',
                     commission=fee,commissionAsset='USDT',realizedPnl='0',time=int(when.timestamp()*1000))
                for identity,side,fee,when in [(1,'BUY','0',entry_at),(2,'SELL','.1',closed_at)]]
        state.api.get_user_trades.side_effect = lambda symbol,**kw: [row for row in rows
            if kw['start_time'] <= row['time'] <= kw['end_time']]
        state.api.get_income_history.side_effect = lambda **kw: [dict(symbol='BTCUSDT',incomeType='COMMISSION',
            income='-.1',asset='USDT',tranId=100,tradeId='2',time=rows[1]['time'],info='')] if (
            kw['income_type']=='COMMISSION' and kw['start_time'] <= rows[1]['time'] <= kw['end_time']) else []
        await state.admin.execute("UPDATE trading.trade_records SET executed_at=$1 WHERE order_id='1'",
                                  entry_at.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
    state.api.get_order.return_value = dict(symbol='BTCUSDT', orderId=1, side='BUY', status='FILLED',
                                           time=int(entry_at.timestamp()*1000), reduceOnly=False, executedQty='1')
    first = await state.service.refresh()
    assert first['ledger_version'] == 1
    async with state.pool.acquire() as app:
        anchor = dict(await app.fetchrow('SELECT segment_id,inception,opening_cash FROM trading.hrs_equity_segment WHERE account_id=$1',state.account))
    state.api.use_unified_account = False
    state.api.get_income_history.reset_mock()
    state.clock[0] += timedelta(seconds=30)
    state.api.get_order.reset_mock()
    from strategies.hrs.equity_evidence import canonical
    encoded, digest = canonical(dict(forecasts=[dict(symbol='BTCUSDT',source_time=at,received_at=at,
        next_funding_time=at+timedelta(hours=1),source='authenticated_premium_index')]))
    await state.admin.execute('INSERT INTO trading.hrs_funding_observation VALUES($1,$2,$3,$4::jsonb)',
                              state.account,digest,at,encoded)
    await state.admin.execute("""
        ALTER TABLE trading.trade_records ADD COLUMN algo_id varchar(100);
        CREATE TABLE trading.order_strategy_evidence(account_id text,symbol text,order_id bigint,strategy text,source_kind text,evidence jsonb);
        GRANT SELECT ON trading.order_strategy_evidence TO trading_user;
    """)
    for identity, side, at_ms, kind, algo in [('1','BUY',cutover,'MARKET',None),
                                               (None,'SELL',cutover+500,'CONDITIONAL_ORDER','50')]:
        await state.admin.execute("""INSERT INTO trading.trade_records
            (account_id,symbol,order_id,strategy,side,status,order_type,executed_at,algo_id)
            VALUES($1,'BTCUSDT',$2,$7,$3,'FILLED',$4,timezone('Asia/Shanghai',to_timestamp($5::bigint/1000.0)),$6)""",
            state.account,identity,side,kind,at_ms,algo,classic_owner)
    await state.admin.execute("""INSERT INTO trading.order_strategy_evidence VALUES
        ($1,'BTCUSDT',2,$2,'classic_algo_link','{"algo_id":"50"}')""",state.account,classic_owner)
    state.api.get_algo_order.return_value=dict(algoId=50,symbol='BTCUSDT',side='SELL',algoStatus='FINISHED',
                                              actualOrderId='2',createTime=cutover+500)
    fills = [dict(symbol='BTCUSDT', id=11, orderId=1, side='BUY', price='100', qty='1', quoteQty='100',
                  realizedPnl='0', commission='0', commissionAsset='USDT', time=cutover),
             dict(symbol='BTCUSDT', id=12, orderId=2, side='SELL', price='102', qty='1', quoteQty='102',
                  realizedPnl='2', commission='.2', commissionAsset='USDT', time=cutover+1000)]
    incomes = [dict(symbol='BTCUSDT', incomeType=kind, income=amount, asset='USDT', tranId=tid,
                    tradeId='12', time=cutover+1000, info='') for kind,amount,tid
               in [('COMMISSION','-.2',111), ('REALIZED_PNL','2',112)]]
    state.api.get_user_trades.side_effect=lambda symbol, **kw: [
        fill for fill in fills if kw['start_time'] <= fill['time'] <= kw['end_time']]
    state.api.get_income_history.side_effect=lambda **kw: [
        row for row in incomes if kw['start_time'] <= row['time'] <= kw['end_time'] and row['incomeType']==kw['income_type']]
    restarted = HrsEquityService(state.db,state.api,state.service.capital_mgr,state.service.position_manager)
    restarted.clock = state.service.clock
    try:
        second = await restarted.refresh()
        assert second['ledger_version'] == 2, second
        assert second['reasons'] == first['reasons']
        assert all(c.kwargs['start_time']>=cutover for c in state.api.get_income_history.await_args_list)
        async with state.pool.acquire() as app:
            assert dict(await app.fetchrow('SELECT segment_id,inception,opening_cash FROM trading.hrs_equity_segment WHERE account_id=$1',state.account)) == anchor
            assert await app.fetchval('SELECT count(*) FROM trading.execution_fact_income WHERE account_id=$1',state.account)==3
            doc = json.loads(await app.fetchval('SELECT payload FROM trading.hrs_equity_evidence WHERE account_id=$1 AND evidence_sha256=$2',state.account,second['evidence_sha256']))
            if preperiod_entry:
                assert [i['pm_evidence_sha256'] for i in doc['capture']['extended_fill_coverage']] == [first['evidence_sha256']]
            pm, classic = doc['income_report']['mode_segments']
            assert Decimal(pm['by_strategy']['hrs']['net_usdt']) == Decimal('-.1')
            assert Decimal(classic['by_strategy'][classic_owner]['net_usdt']) == Decimal('1.8')
            assert state.api.get_order.await_count == 0
            assert set(await app.fetchval('SELECT array_agg(DISTINCT source) FROM trading.execution_fact_cursor WHERE account_id=$1',state.account)) >= {'um/income','fapi/income'}
    finally:
        await restarted.close()
