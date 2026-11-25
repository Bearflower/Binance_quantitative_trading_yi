"""Real PostgreSQL tests; isolated Unix-socket cluster, no production credentials."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / 'scripts/database_permissions.py'


@pytest.fixture(scope='module')
def pg():
    if not all(shutil.which(x) for x in ('initdb', 'pg_ctl', 'psql')):
        pytest.skip('local PostgreSQL binaries required')
    with tempfile.TemporaryDirectory(prefix='acl-') as tmp:
        data = Path(tmp) / 'data'
        subprocess.run(['initdb', '-D', str(data), '-U', 'acl_test_admin', '-A', 'trust', '--no-locale', '-E', 'UTF8'], check=True, capture_output=True)
        subprocess.run(['pg_ctl', '-D', str(data), '-l', str(Path(tmp)/'log'), '-o', f"-F -k {tmp} -h '' -p 55439", '-w', 'start'], check=True, capture_output=True)
        env = dict(os.environ, PGHOST=tmp, PGPORT='55439', PGDATABASE='postgres', PGUSER='acl_test_admin')
        def sql(query):
            return subprocess.run(['psql', '-XAt', '-v', 'ON_ERROR_STOP=1'], input=query, env=env, text=True, capture_output=True)
        assert sql('CREATE ROLE trading_user LOGIN; CREATE ROLE inherited_reader; CREATE SCHEMA app; GRANT USAGE ON SCHEMA app TO trading_user;').returncode == 0
        yield env, sql, Path(tmp)
        subprocess.run(['pg_ctl', '-D', str(data), '-m', 'immediate', '-w', 'stop'], check=True, capture_output=True)


@pytest.fixture
def case(pg):
    env, sql, tmp = pg
    assert sql('DROP SCHEMA app CASCADE; CREATE SCHEMA app; GRANT USAGE ON SCHEMA app TO trading_user; CREATE TABLE app.anchor(id text PRIMARY KEY, payload text); INSERT INTO app.anchor VALUES(\'a\',\'b\');').returncode == 0
    manifest = {'version': 1, 'schemas': ['app'], 'roles': {'trading_user': {'required': True, 'schemas': {'app': ['USAGE']}}}, 'profiles': {'none': [], 'read': ['SELECT'], 'append': ['SELECT','INSERT']}, 'objects': [{'schema': 'app', 'name': 'anchor', 'kind': 'table', 'grants': {'trading_user': 'append'}, 'columns': {'trading_user': {'id': ['UPDATE']}}}]}
    def run(mode='audit', contract=None):
        path = tmp/'manifest.json'
        path.write_text(json.dumps(contract or manifest))
        result = subprocess.run(['python3', str(CLI), mode, '--local', '--manifest', str(path)], env=env, text=True, capture_output=True)
        return result, json.loads(result.stdout) if result.stdout.strip() else None
    yield sql, manifest, run
    sql('REVOKE inherited_reader FROM trading_user; DROP OWNED BY inherited_reader;')


def test_missing_grants_repair_and_idempotence(case):
    sql, _, run = case
    result, report = run()
    assert result.returncode == 1 and any(x['code']=='missing_privilege' for x in report['issues'])
    result, report = run('apply')
    assert result.returncode == 0, result.stderr + result.stdout
    assert report['ok']
    assert run('apply')[0].returncode == 0
    assert sql("BEGIN; SET LOCAL ROLE trading_user; SELECT * FROM app.anchor FOR UPDATE; ROLLBACK;").returncode == 0


def test_unknown_object_prevents_partial_repair(case):
    sql, _, run = case
    assert sql('CREATE TABLE app.unregistered(id int);').returncode == 0
    result, report = run('apply')
    assert result.returncode == 1 and any(x['code']=='unregistered_object' for x in report['issues'])
    assert sql("SELECT has_table_privilege('trading_user','app.anchor','SELECT');").stdout.strip() == 'f'


def test_column_and_public_overgrant_detected(case):
    sql, _, run = case
    sql('GRANT UPDATE(payload) ON app.anchor TO PUBLIC;')
    result, report = run('apply')
    assert result.returncode == 1
    assert any(x['code']=='unexpected_column_privilege' for x in report['issues'])


def test_inherited_overgrant_detected(case):
    sql, _, run = case
    sql('GRANT DELETE ON app.anchor TO inherited_reader; GRANT inherited_reader TO trading_user;')
    assert any(x['code']=='unexpected_privilege' for x in run()[1]['issues'])


def test_missing_required_table_and_role(case):
    _, manifest, run = case
    manifest['objects'][0]['name'] = 'absent'
    manifest['objects'][0]['required'] = True
    manifest['roles']['absent_user'] = {'required': True, 'schemas': {}}
    codes = {x['code'] for x in run()[1]['issues']}
    assert {'missing_object','missing_role'} <= codes


def test_sequence_permissions_and_unknown_column(case):
    sql, manifest, run = case
    sql('CREATE SEQUENCE app.ids;')
    manifest['profiles']['seq'] = ['USAGE','SELECT']
    manifest['objects'].append({'schema':'app','name':'ids','kind':'sequence','grants':{'trading_user':'seq'}})
    assert run('apply')[0].returncode == 0
    assert sql("SET ROLE trading_user; SELECT nextval('app.ids');").returncode == 0
    manifest['objects'][0]['columns']['trading_user']['absent']=['UPDATE']
    assert any(x['code']=='missing_column' for x in run()[1]['issues'])


def test_trigger_required_before_lock_permission(case):
    sql, manifest, run = case
    manifest['objects'][0]['lock']={'role':'trading_user','column':'id','trigger':'anchor_immutable','function':'app.reject_mutation'}
    assert any(x['code']=='missing_immutable_trigger' for x in run('apply')[1]['issues'])
    sql("CREATE OR REPLACE FUNCTION app.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable'; END $$; CREATE TRIGGER anchor_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON app.anchor FOR EACH STATEMENT EXECUTE FUNCTION app.reject_mutation();")
    result, report=run('apply')
    assert result.returncode == 0, result.stdout + result.stderr
    assert sql('SET ROLE trading_user; UPDATE app.anchor SET id=id WHERE false;').returncode != 0
    sql('ALTER TABLE app.anchor DISABLE TRIGGER anchor_immutable;')
    assert any(x['code']=='missing_immutable_trigger' for x in run()[1]['issues'])


def test_invalid_manifest_cannot_inject_sql(case):
    _, manifest, run = case
    manifest['objects'][0]['columns']['trading_user']['id']=['UPDATE); DROP SCHEMA app CASCADE; --']
    result,_=run('apply')
    assert result.returncode == 2


def test_table_level_update_does_not_hide_forbidden_column(case):
    sql, _, run=case
    sql('GRANT UPDATE ON app.anchor TO trading_user;')
    assert any(x['code']=='unexpected_privilege' for x in run()[1]['issues'])


def test_trigger_that_allows_mutation_is_rejected(case):
    sql, manifest, run = case
    manifest['objects'][0]['lock']={'role':'trading_user','column':'id','trigger':'anchor_immutable','function':'app.reject_mutation'}
    sql("CREATE OR REPLACE FUNCTION app.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$; CREATE TRIGGER anchor_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON app.anchor FOR EACH STATEMENT EXECUTE FUNCTION app.reject_mutation();")
    # Supply minimum privileges so the audit reaches the semantic trigger probe.
    sql('GRANT SELECT,INSERT ON app.anchor TO trading_user; GRANT UPDATE(id) ON app.anchor TO trading_user;')
    assert any(x['code']=='immutable_probe_failed' for x in run()[1]['issues'])


def test_minimum_and_allowed_privileges_are_distinct(case):
    sql, manifest, run=case
    manifest['profiles']['append']={'required':['SELECT','INSERT'],'allowed':['SELECT','INSERT','REFERENCES']}
    assert run('apply')[0].returncode == 0
    assert sql("SELECT has_table_privilege('trading_user','app.anchor','REFERENCES');").stdout.strip()=='f'


def test_grant_option_and_database_create_are_not_silent(case):
    sql, _, run=case
    sql('GRANT SELECT ON app.anchor TO trading_user WITH GRANT OPTION; GRANT CREATE ON DATABASE postgres TO trading_user;')
    codes={x['code'] for x in run()[1]['issues']}
    assert {'unexpected_grant_option','unexpected_database_control'} <= codes
    sql('REVOKE CREATE ON DATABASE postgres FROM trading_user;')


def test_exact_unicode_rule_overrides_dynamic_pattern(case):
    sql, manifest, run=case
    sql('CREATE TABLE app.kline_牛来usdt_1h(id int); CREATE TABLE app.kline_btcusdt_1h(id int);')
    manifest['objects'] += [
        {'schema':'app','pattern':'^kline_[a-z0-9_]+$','kind':'table','grants':{}},
        {'schema':'app','name':'kline_btcusdt_1h','kind':'table','grants':{'trading_user':'read'}},
        {'schema':'app','name':'kline_牛来usdt_1h','kind':'table','grants':{'trading_user':'read'}}]
    result,report=run('apply')
    assert result.returncode == 0, result.stdout+result.stderr


def test_postflight_failure_rolls_back_grants(case):
    sql, manifest, run=case
    manifest['objects'][0]['lock']={'role':'trading_user','column':'id','trigger':'anchor_immutable','function':'app.reject_mutation'}
    sql("CREATE OR REPLACE FUNCTION app.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$; CREATE TRIGGER anchor_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON app.anchor FOR EACH STATEMENT EXECUTE FUNCTION app.reject_mutation();")
    result,_=run('apply')
    assert result.returncode==2 and 'rolled back' in result.stderr
    assert sql("SELECT has_table_privilege('trading_user','app.anchor','SELECT');").stdout.strip()=='f'


def test_column_grant_option_on_allowed_select_is_rejected(case):
    sql, _, run=case
    sql('GRANT SELECT,INSERT ON app.anchor TO trading_user; GRANT UPDATE(id) ON app.anchor TO trading_user; GRANT SELECT(payload) ON app.anchor TO trading_user WITH GRANT OPTION;')
    result,report=run()
    assert result.returncode==1
    assert any(x['code']=='unexpected_grant_option' and x.get('column')=='payload' for x in report['issues'])


def test_noinherit_set_role_membership_is_rejected(case):
    sql, _, run=case
    sql('ALTER ROLE trading_user NOINHERIT; GRANT DELETE ON app.anchor TO inherited_reader; GRANT inherited_reader TO trading_user;')
    try:
        assert any(x['code']=='unexpected_membership' for x in run()[1]['issues'])
    finally:
        sql('ALTER ROLE trading_user INHERIT;')


def test_cutover_equity_anchor_is_account_scoped_and_append_only(pg):
    env, sql, tmp = pg
    migration = ROOT/'database/postgres/migrations/2026-11-25-account-mode-equity.sql'
    assert sql('CREATE ROLE trading_migration_admin; CREATE SCHEMA trading AUTHORIZATION trading_migration_admin; GRANT USAGE ON SCHEMA trading TO trading_user').returncode == 0
    try:
        for _ in range(2):
            result = sql('SET ROLE trading_migration_admin;\n' + migration.read_text())
            assert result.returncode == 0, result.stderr
        contract = json.loads((ROOT/'database/postgres/permissions.json').read_text())
        # This fixture tests one table, not the production cluster identity.
        contract.pop('bootstrap_identity', None)
        contract['objects'] = [o for o in contract['objects'] if o.get('name') == 'account_mode_equity']
        contract['schemas'] = ['trading']
        contract['roles'] = {'trading_user': {'required': True, 'schemas': {'trading': ['USAGE']}}}
        manifest = tmp/'cutover.json'
        manifest.write_text(json.dumps(contract))
        for action in ('apply', 'audit'):
            result = subprocess.run(['python3', str(CLI), action, '--local', '--manifest', str(manifest)],
                                    env=env, text=True, capture_output=True)
            assert result.returncode == 0, result.stdout + result.stderr
        insert = """INSERT INTO trading.account_mode_equity VALUES
            ('main',1000,'pm_final',NULL,to_timestamp(0.9),100,100,0,'flat-1',repeat('a',64),DEFAULT),
            ('main',1000,'classic_open',NULL,to_timestamp(1.1),50,50,0,'flat-2',repeat('a',64),DEFAULT)
            ON CONFLICT DO NOTHING;"""
        mismatch = insert.replace("50,50,0,'flat-2',repeat('a'", "50,50,0,'flat-2',repeat('b'")
        assert sql('SET ROLE trading_user;' + mismatch).returncode != 0
        assert sql('SET ROLE trading_user;' + insert + insert).returncode == 0
        assert sql('SET ROLE trading_user; SELECT count(*) FROM trading.account_mode_equity').stdout.strip().endswith('2')
        for stmt in ('UPDATE trading.account_mode_equity SET total_equity=0',
                     'DELETE FROM trading.account_mode_equity'):
            assert sql('SET ROLE trading_user;' + stmt).returncode != 0
    finally:
        sql('DROP SCHEMA trading CASCADE; DROP ROLE trading_migration_admin')


@pytest.mark.asyncio
async def test_pm_and_classic_facts_round_trip_under_trading_role(pg, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from shared.database import DatabaseManager
    from shared.trade_logger import TradeLogger
    from shared.execution_facts_sink import PostgresExecutionFactsSink
    from shared.accounting.report_reader import read_monthly_economic_report
    from scripts.export_execution_facts import ExecutionFactExporter

    env, sql, _ = pg
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    assert sql((ROOT/"database/postgres/init-scripts/11-execution-facts.sql").read_text()).returncode == 0
    assert sql((ROOT/"database/postgres/migrations/2026-11-04-income-immutability.sql").read_text()).returncode == 0
    assert sql((ROOT/"database/postgres/migrations/2026-11-09-derived-income-attribution.sql").read_text()).returncode == 0
    assert sql((ROOT/"database/postgres/migrations/2026-11-25-account-fact-provenance.sql").read_text()).returncode == 0
    db = DatabaseManager(host=env["PGHOST"], port=int(env["PGPORT"]), database="postgres",
                         user="trading_user", password="", min_pool_size=1, max_pool_size=2)
    try:
        await db.connect()
        await TradeLogger(db, "hrs").ensure_table_exists()
        await db.execute("INSERT INTO trading.order_strategy_evidence "
                         "(account_id,symbol,order_id,strategy,source_kind,source_id,evidence) "
                         "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)",
                         "main", "BTCUSDT", 221, "hrs", "classic_algo_link", "31", '{"algo_id":"31"}')
        sink = PostgresExecutionFactsSink(db)
        for pm, left, right, orders in ((True, 90, 99, [(1, "btc_eth", 98)]),
                                       (False, 100, 110, [(2, "btc_eth_aggressive", 100), (3, "hrs", 101)])):
            monkeypatch.setenv("BINANCE_ACCOUNT_MODE", "pm" if pm else "classic")
            trades = []
            incomes = {"REALIZED_PNL": [], "COMMISSION": [], "FUNDING_FEE": []}
            for order, strategy, stamp in orders:
                identity = dict(symbol="BTCUSDT", orderId=order, side="BUY", time=stamp)
                trades.append(dict(identity, id=order, qty="1", price="1", realizedPnl=str(order),
                                   commissionAsset="USDT", commission="0.1"))
                assert await TradeLogger(db, strategy).log_order(
                    dict(orderId=order, status="FILLED", executedQty="1", avgPrice="1", time=stamp),
                    "BTCUSDT", "BUY", "MARKET")
                for kind, amount in (("REALIZED_PNL", str(order)), ("COMMISSION", "-0.1")):
                    incomes[kind].append(dict(time=stamp, symbol="BTCUSDT", asset="USDT",
                                              incomeType=kind, income=amount, tranId=order, tradeId=order))
            if not pm:
                incomes["FUNDING_FEE"].append(dict(time=100, symbol="BTCUSDT", asset="USDT",
                                                    incomeType="FUNDING_FEE", income="-1", tranId=1))
            client = SimpleNamespace(use_unified_account=pm, get_user_trades=AsyncMock(return_value=trades),
                                     get_income_history=AsyncMock(side_effect=lambda **kw: incomes[kw["income_type"]]))
            exporter = ExecutionFactExporter("main", client, sink=sink, refresh_completed=True)
            for _ in range(2):
                assert (await exporter.export_fills("BTCUSDT", left, right)).page_complete
                for kind in incomes:
                    result = await exporter.export_income(left, right, kind)
                    assert result.page_complete, result.errors
        report = await read_monthly_economic_report(db, "main", 90, 110)
        assert report["income_count"] == 7 and report["account_coverage_complete"]
        assert Decimal(report["account_usdt_total"]) == Decimal("4.7")
        assert {sid: Decimal(bucket["net_usdt"]) for sid,bucket in report["by_strategy"].items()} == {
            "btc_eth": Decimal(".9"), "btc_eth_aggressive": Decimal("1.9"), "hrs": Decimal("2.9")}
        assert Decimal(report["unattributed_usdt_total"]) == -1
        assert [s["income_count"] for s in report["mode_segments"]] == [2,5]
        raw = await db.fetch_all("SELECT source, count(*) AS n FROM trading.execution_fact_raw GROUP BY source")
        assert {r["source"]: r["n"] for r in raw} == {
            "userTrades": 1, "um/income": 2, "fapi/userTrades": 2, "fapi/income": 5}
        with pytest.raises(Exception, match="immutable income raw"):
            await db.execute("UPDATE trading.execution_fact_raw SET raw_payload='{}' WHERE source='fapi/income'")
        with pytest.raises(Exception, match="fill identity"):
            await db.execute("UPDATE trading.execution_fact_fill SET order_id=99 WHERE trade_id=1")
        with pytest.raises(Exception, match="fill identity"):
            await db.execute("UPDATE trading.execution_fact_fill SET commission=5 WHERE trade_id=1")

    finally:
        await db.disconnect()
        sql("DROP SCHEMA trading CASCADE")


@pytest.mark.asyncio
async def test_trade_logger_reused_order_only_updates_current_account_epoch(pg,monkeypatch):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from shared.database import DatabaseManager
    from shared.trade_logger import BEIJING_TZ, TradeLogger

    env, sql, _ = pg
    boundary_ms = int((datetime.now(timezone.utc)-timedelta(minutes=20)).timestamp()*1000)
    boundary = datetime.fromtimestamp(boundary_ms/1000,BEIJING_TZ).replace(tzinfo=None)
    monkeypatch.setenv("TRADE_ACCOUNT_ID","main")
    monkeypatch.setenv("BINANCE_ACCOUNT_MODE","classic")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS",str(boundary_ms))
    assert sql("CREATE SCHEMA trading AUTHORIZATION trading_user").returncode == 0
    db = DatabaseManager(host=env["PGHOST"], port=int(env["PGPORT"]), database="postgres",
                         user="trading_user", password="", min_pool_size=1, max_pool_size=2)
    try:
        await db.connect()
        log = TradeLogger(db,"hrs")
        await log.ensure_table_exists()
        async def record(order,stamp,account="main",symbol="BTCUSDT"):
            return await db.fetch_one("INSERT INTO trading.trade_records "
                "(strategy,account_id,symbol,order_id,side,order_type,quantity,price,status,executed_at) "
                "VALUES ('hrs',$1,$2,$3,'SELL','MARKET',1,10,'NEW',$4) RETURNING id",
                account,symbol,order,stamp)
        pm = await record("42",boundary-timedelta(milliseconds=1))
        current = await record("42",boundary)
        other = await record("42",boundary,account="subaccount")
        client = SimpleNamespace(use_unified_account=False, get_order=AsyncMock(return_value=dict(
            symbol="BTCUSDT", orderId=42, side="SELL", status="FILLED", executedQty="1", time=boundary_ms)), get_user_trades=AsyncMock(return_value=[
            dict(symbol="BTCUSDT",orderId=42,commissionAsset="BNB",commission="8",time=boundary_ms-1),
            dict(symbol="BTCUSDT",orderId=42,commissionAsset="USDT",commission=".2",time=boundary_ms,id=12,qty="1",side="SELL")]))
        assert (await log.reconcile_commissions(client))["matched_orders"] == 1
        assert await log.update_realized_pnl("42",Decimal("3.4"),symbol="BTCUSDT",side="SELL",executed_at=boundary)
        assert await log._mark_existing_close_record("hrs","BTCUSDT","SELL",None,boundary)
        assert await log.mark_entry_canceled("42","BTCUSDT")
        rows = {r['id']:r for r in await db.fetch_all("SELECT id,commission,realized_pnl,close_reason,status FROM trading.trade_records")}
        for row in (rows[pm['id']], rows[other['id']]):
            assert (row["commission"],row["realized_pnl"],row["close_reason"],row["status"]) == (0,None,None,"NEW")
        assert tuple(rows[current["id"]][k] for k in ("commission","realized_pnl","close_reason","status")) == (
            Decimal("-.2"), Decimal("3.4"), "STOP_LOSS","CANCELED")

        import asyncio
        assert all(await asyncio.gather(*(log.insert_pnl_summary(
            Decimal("2"), "ETHUSDT", "SELL", executed_at=boundary, since=boundary)
            for _ in range(5))))
        summary = await db.fetch_one("SELECT count(*) n FROM trading.trade_records WHERE order_type='PNL_SUMMARY'")
        assert summary["n"] == 1

        old_stub = await record(None,boundary-timedelta(milliseconds=1),symbol="ETHUSDT")
        assert not await log.update_realized_pnl("",Decimal("2"),symbol="ETHUSDT",side="SELL",executed_at=boundary)
        current_stub = await record(None,boundary,symbol="ETHUSDT")
        assert await log.update_realized_pnl("",Decimal("2"),symbol="ETHUSDT",side="SELL",executed_at=boundary)
        amounts = {r["id"]:r["realized_pnl"] for r in await db.fetch_all("SELECT id,realized_pnl FROM trading.trade_records WHERE symbol='ETHUSDT' AND order_id IS NULL")}
        assert amounts=={old_stub['id']:None,current_stub['id']:Decimal("2")}

        await record("42",boundary+timedelta(seconds=1))
        await record(None,boundary)
        assert (await log.reconcile_commissions(client))["matched_orders"] == 0
        assert not await log.mark_entry_canceled("42","BTCUSDT")
        assert not await log.update_realized_pnl("42",Decimal("99"),symbol="BTCUSDT",side="SELL",executed_at=boundary)
        assert not await log.update_realized_pnl("42",Decimal("99"),symbol="BTCUSDT",side="SELL",executed_at=boundary-timedelta(milliseconds=1))
        monkeypatch.setenv("BINANCE_ACCOUNT_MODE","pm")
        assert await log.update_realized_pnl("42",Decimal("1"),symbol="BTCUSDT",side="SELL",executed_at=boundary-timedelta(milliseconds=1))
    finally:
        await db.disconnect()
        sql("DROP SCHEMA trading CASCADE")


def test_cutover_scopes_open_condition_ids_and_preserves_replays(pg):
    _, sql, _ = pg
    migration = ROOT / 'database/postgres/migrations/2026-11-25-scoped-open-condition-ids.sql'
    assert sql("""CREATE SCHEMA btc_eth AUTHORIZATION trading_user;
        SET ROLE trading_user;
        CREATE TABLE btc_eth.condition_orders (account_id text, strategy_name text, symbol text,
            algo_id bigint, order_id bigint, status text);
        CREATE UNIQUE INDEX idx_co_algo_unique ON btc_eth.condition_orders(algo_id);
        CREATE UNIQUE INDEX idx_co_order_unique ON btc_eth.condition_orders(order_id);""").returncode == 0
    try:
        for _ in range(2):
            result = sql('SET ROLE trading_user;\n' + migration.read_text())
            assert result.returncode == 0, result.stderr
        result = sql("""SET ROLE trading_user;
            INSERT INTO btc_eth.condition_orders VALUES
              ('main','hrs','ETHUSDT',1,2,'OPEN'),('main','btc_eth','ETHUSDT',1,2,'OPEN'),
              ('sub','hrs','ETHUSDT',1,2,'OPEN'),('main','hrs','BTCUSDT',1,2,'OPEN');
            INSERT INTO btc_eth.condition_orders SELECT * FROM btc_eth.condition_orders ON CONFLICT DO NOTHING;
            SELECT count(*) FROM btc_eth.condition_orders;
            SELECT count(*) FROM pg_indexes WHERE schemaname='btc_eth' AND indexname LIKE 'idx_co_scoped_%';""")
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[-2:] == ['4', '2']
    finally:
        sql('DROP SCHEMA btc_eth CASCADE')


@pytest.mark.asyncio
async def test_income_calibration_isolated_epoch_progress_and_complete_order(pg, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from ai_tuner.reconciler import income_reconciler as module
    from shared.database import DatabaseManager
    from shared.trade_logger import TradeLogger

    env, sql, _ = pg
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_ACCOUNT_MODE", "classic")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "100")
    monkeypatch.setattr(module, "_utc_now_ms", lambda: 110)
    assert sql((ROOT/"database/postgres/init-scripts/11-execution-facts.sql").read_text()).returncode == 0
    db = DatabaseManager(host=env["PGHOST"], port=int(env["PGPORT"]), database="postgres",
                         user="trading_user", password="", min_pool_size=1, max_pool_size=2)
    try:
        await db.connect()
        await TradeLogger(db, "hrs").ensure_table_exists()
        assert sql("""
            INSERT INTO trading.trade_records(strategy,symbol,order_id,side,order_type,quantity,price,status,executed_at,account_id)
            SELECT 'hrs','BTCUSDT','77','SELL','MARKET',1,2,'FILLED',timezone('Asia/Shanghai', to_timestamp(t)), 'main'
            FROM (VALUES (0.099), (0.100)) AS times(t);
            INSERT INTO trading.execution_fact_fill(account_id,symbol,trade_id,order_id,side,qty,realized_pnl,trade_time_utc)
            VALUES ('main','BTCUSDT',1,77,'SELL',4,8,to_timestamp(.099)),
                   ('main','BTCUSDT',2,77,'SELL',1,3,to_timestamp(.100));
            INSERT INTO trading.execution_fact_income(account_id,symbol,trade_id,tran_id,asset,income_type,income,event_time_utc)
            VALUES ('main','BTCUSDT',2,3,'USDT','REALIZED_PNL',3,to_timestamp(.101)),
                   ('main','BTCUSDT',1,4,'USDT','REALIZED_PNL',8,to_timestamp(.099));
        """).returncode == 0
        receipt = dict(symbol="BTCUSDT", orderId=77, side="SELL", status="FILLED", executedQty="2", time=100)
        income = dict(symbol="BTCUSDT", incomeType="REALIZED_PNL", tradeId=2, tranId=3, asset="USDT", time=101, income="3")
        client = SimpleNamespace(use_unified_account=False, testnet=True,
            get_order=AsyncMock(return_value=receipt), get_income_history=AsyncMock(return_value=[income]))
        rec = module.IncomeReconciler(db, client, {"reconciler": {"state_schema": "trading"}})
        await rec._ensure_state_table()
        await db.execute(rec._reconciled_ts_upsert_sql(), 99, "main")
        await rec.run_once()
        client.get_income_history.assert_awaited_with(start_time=100, end_time=110, income_type="REALIZED_PNL", limit=1000)
        async def pnls():
            return [r["realized_pnl"] for r in await db.fetch_all(
                "SELECT realized_pnl FROM trading.trade_records ORDER BY executed_at")]
        assert await pnls() == [None, None]
        receipt["executedQty"] = "1"
        await rec.run_once()
        await rec.run_once()
        assert await pnls() == [None, Decimal("3")]
        progress = await db.fetch_all("SELECT account_id,last_reconciled_ts FROM trading.pnl_reconcile_state_by_account")
        assert {r["account_id"]: r["last_reconciled_ts"] for r in progress} == {"main": 99, rec.progress_id: 110}
        with pytest.raises(ValueError):
            await rec._read_verified_fill("BTCUSDT", 1, 4, Decimal("8"), 101)
    finally:
        await db.disconnect()
        sql("DROP SCHEMA trading CASCADE")
