"""权限工具的真实 PostgreSQL 回归；使用隔离 Unix socket，无生产凭据。

源自 c4a0bea5，仅移入权限工具/契约测试；账户切换业务集成不属于本次移入范围。
"""
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












def test_current_risk_income_report_objects_are_mandatory(case):
    """CR-026：新建或恢复的数据库必须在缺少必需对象时阻止激活。"""
    sql, _, run = case
    source = json.loads((ROOT / 'database/postgres/permissions.json').read_text())
    names = {'strategy_risk_policy', 'strategy_risk_active', 'strategy_risk_audit',
             'strategy_risk_audit_id_seq', 'entry_risk_reservation',
             'income_ingestion_attempt', 'income_ingestion_conflict',
             'income_ingestion_attempt_event', 'income_ingestion_attempt_event_event_id_seq',
             'ledger_report_run', 'ledger_report_active'}
    rules = [r for r in source['objects'] if r['schema'] == 'trading' and r.get('name') in names]
    assert {r['name'] for r in rules} == names
    assert sql('CREATE SCHEMA IF NOT EXISTS trading; GRANT USAGE ON SCHEMA trading TO trading_user;').returncode == 0
    contract = {'version': 1, 'schemas': ['trading'],
                'roles': {'trading_user': {'required': True, 'schemas': {'trading': ['USAGE']}},
                          'hrs_acceptance_reader': {'required': False, 'schemas': {}}},
                'profiles': source['profiles'], 'objects': rules}
    result, report = run(contract=contract)
    assert result.returncode == 1
    assert {r['object'] for r in report['issues'] if r['code'] == 'missing_object'} == {'trading.' + n for n in names}


def test_every_manifest_object_has_explicit_availability_classification():
    """遗漏字段不能再静默变成可选对象；例外必须附带可核对的原因。"""
    source = json.loads((ROOT / 'database/postgres/permissions.json').read_text())
    for rule in source['objects']:
        assert isinstance(rule.get('required'), bool), rule
        if not rule['required']:
            assert rule.get('optional_reason', '').strip(), rule
    required = {'trade_records', 'execution_fact_income', 'position_claims', 'short_positions'}
    assert all(rule['required'] for rule in source['objects'] if rule.get('name') in required)
