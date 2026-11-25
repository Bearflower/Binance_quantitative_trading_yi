"""PG15-only identity split tests; no production credentials or catalog writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest

ROOT=Path(__file__).resolve().parents[2]
SCRIPT=ROOT/'scripts/database_bootstrap_identity.py'
PG=Path('/opt/homebrew/opt/postgresql@15/bin')


def module():
    assert SCRIPT.exists(), 'controlled bootstrap identity migration is missing'
    spec=importlib.util.spec_from_file_location('bootstrap_identity',SCRIPT)
    obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj)
    return obj


def test_renderer_never_updates_system_catalogs_or_reassigns_all_owned():
    m=module()
    text=SCRIPT.read_text().lower()
    assert 'update pg_authid' not in text
    assert 'reassign owned' not in text
    assert m.APPLICATION == 'trading_user'


@pytest.fixture
def cluster():
    if not (PG/'initdb').exists():pytest.skip('isolated PostgreSQL15 binaries required')
    with tempfile.TemporaryDirectory(prefix='identity-') as tmp:
        d=Path(tmp);env=dict(os.environ,PGHOST=tmp,PGPORT='55443',PGDATABASE='trading_platform',PGUSER='trading_migration_admin',PATH=str(PG)+os.pathsep+os.environ['PATH'])
        subprocess.run([str(PG/'initdb'),'-D',str(d/'data'),'-U','trading_user','-A','trust','--no-locale','-E','UTF8'],check=True,capture_output=True)
        subprocess.run([str(PG/'pg_ctl'),'-D',str(d/'data'),'-l',str(d/'log'),'-o',f"-F -k {tmp} -h '' -p 55443 -c autovacuum_naptime=1 -c authentication_timeout=1",'-w','start'],check=True,capture_output=True)
        def sql(query,user='trading_migration_admin',database='trading_platform'):
            return subprocess.run([str(PG/'psql'),'-XqAt','-v','ON_ERROR_STOP=1','-v','VERBOSITY=sqlstate','-U',user,'-d',database],input=query,text=True,capture_output=True,env=env)
        try:
            assert sql('CREATE ROLE trading_migration_admin LOGIN SUPERUSER; CREATE DATABASE trading_platform OWNER trading_migration_admin;','trading_user','postgres').returncode==0
            setup="""CREATE SCHEMA app AUTHORIZATION trading_user;
CREATE TABLE app.legacy(id serial PRIMARY KEY,n text); ALTER TABLE app.legacy OWNER TO trading_user;
CREATE TABLE app.protected(id int,n text); GRANT SELECT ON app.protected TO trading_user;
CREATE FUNCTION app.legacy_fn() RETURNS text LANGUAGE sql SECURITY DEFINER AS 'SELECT current_user::text'; ALTER FUNCTION app.legacy_fn() OWNER TO trading_user;
CREATE TYPE app.status AS ENUM ('ready'); ALTER TYPE app.status OWNER TO trading_user;
ALTER DEFAULT PRIVILEGES FOR ROLE trading_migration_admin IN SCHEMA app GRANT SELECT ON TABLES TO trading_user;
ALTER DEFAULT PRIVILEGES FOR ROLE trading_user IN SCHEMA app GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO trading_user;
ALTER DEFAULT PRIVILEGES FOR ROLE trading_user IN SCHEMA app GRANT SELECT,USAGE ON SEQUENCES TO trading_user;
ALTER ROLE trading_user SET search_path TO app,public;
ALTER ROLE trading_user NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'test-only-password';
"""
            assert sql(setup).returncode==0
            manifest={'version':1,'schemas':['app'],'roles':{'trading_user':{'required':True,'schemas':{'app':['USAGE','CREATE']}}},'profiles':{'none':[],'read':['SELECT'],'legacy':{'required':['SELECT','INSERT','UPDATE','DELETE'],'allowed':['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']},'sequence':['USAGE','SELECT','UPDATE']},'objects':[{'schema':'app','name':'legacy','kind':'table','owner':'trading_user','required':True,'grants':{'trading_user':'legacy'}},{'schema':'app','name':'legacy_id_seq','kind':'sequence','owner':'trading_user','grants':{'trading_user':'sequence'}},{'schema':'app','name':'protected','kind':'table','owner':'trading_migration_admin','grants':{'trading_user':'read'}}], 'bootstrap_identity':{'bootstrap_role':'trading_bootstrap_admin','application_role':'trading_user','oid':10,'allow_legacy_demoted':True,'database':'trading_platform','owned_schemas':['app'],'owned_routines':[{'schema':'app','name':'legacy_fn','arguments':''}],'owned_types':[{'schema':'app','name':'status'}],'default_privileges':[{'creator':'trading_migration_admin','schema':'app','kind':'TABLES','grantee':'trading_user','privileges':['SELECT']},{'creator':'trading_user','schema':'app','kind':'TABLES','grantee':'trading_user','privileges':['DELETE','INSERT','SELECT','UPDATE']},{'creator':'trading_user','schema':'app','kind':'SEQUENCES','grantee':'trading_user','privileges':['SELECT','USAGE']}]}}
            yield sql,manifest,env,d
        finally:
            subprocess.run([str(PG/'pg_ctl'),'-D',str(d/'data'),'-m','immediate','-w','stop'],check=True,capture_output=True)


def test_split_preserves_runtime_boundary_settings_password_and_rollback(cluster):
    m=module();sql,manifest,env,d=cluster
    plan=json.loads(sql(m.inventory_sql()).stdout)
    assert sql('ALTER ROLE trading_user NOLOGIN;').returncode==0
    result=sql(m.render(manifest,plan,'forward'))
    assert result.returncode==0,result.stderr
    assert sql("SELECT oid<>10 AND NOT rolsuper AND NOT rolcanlogin FROM pg_roles WHERE rolname='trading_user';").stdout.strip()=='t'
    assert sql("SELECT rolsuper AND NOT rolcanlogin AND rolpassword IS NULL FROM pg_authid WHERE oid=10 AND rolname='trading_bootstrap_admin';").stdout.strip()=='t'
    assert sql("SET ROLE trading_user; SELECT app.legacy_fn();").stdout.strip()=='trading_user'
    assert sql('SET ROLE trading_user; INSERT INTO app.protected VALUES(1,\'x\');').returncode!=0
    assert sql('SET ROLE trading_user; INSERT INTO app.legacy(n) VALUES(\'ok\');').returncode==0
    assert sql("SELECT setconfig::text FROM pg_db_role_setting WHERE setrole=(SELECT oid FROM pg_roles WHERE rolname='trading_user');").stdout.strip()=='{"search_path=app, public"}'
    assert sql(m.render(manifest,plan,'rollback')).returncode==0
    assert sql("SELECT oid=10 AND NOT rolsuper AND NOT rolcanlogin FROM pg_roles WHERE rolname='trading_user';").stdout.strip()=='t'
    assert sql("SELECT to_regrole('trading_bootstrap_admin') IS NULL;").stdout.strip()=='t'


def test_ungated_migration_and_drift_fail_without_partial_identity_changes(cluster):
    m=module();sql,manifest,_,_=cluster
    plan=json.loads(sql(m.inventory_sql()).stdout)
    assert sql(m.render(manifest,plan,'forward')).returncode!=0
    assert sql("SELECT oid=10 AND NOT rolsuper FROM pg_roles WHERE rolname='trading_user';").stdout.strip()=='t'
    assert sql('ALTER ROLE trading_user NOLOGIN; CREATE TABLE app.unreviewed(id int); ALTER TABLE app.unreviewed OWNER TO trading_user;').returncode==0
    assert sql(m.render(manifest,plan,'forward')).returncode!=0
    assert sql("SELECT to_regrole('trading_bootstrap_admin') IS NULL;").stdout.strip()=='t'


@pytest.mark.parametrize('password_format',['scram-sha-256','md5'])
def test_real_password_authentication_survives_both_role_renames(cluster,password_format):
    m=module();sql,manifest,env,d=cluster
    assert sql("SET password_encryption='"+password_format+"'; ALTER ROLE trading_user PASSWORD 'test-only-password';").returncode==0
    hba=d/'data/pg_hba.conf';hba.write_text('local all trading_user '+('md5' if password_format=='md5' else 'scram-sha-256')+'\n'+hba.read_text())
    assert sql('SELECT pg_reload_conf();').returncode==0
    def authenticate():
        return subprocess.run([str(PG/'psql'),'-XqAt','-U','trading_user','-d','trading_platform','-c',"SELECT oid FROM pg_roles WHERE rolname=current_user;"],env=dict(env,PGPASSWORD='test-only-password'),text=True,capture_output=True)
    assert authenticate().stdout.strip()=='10'
    plan=json.loads(sql(m.inventory_sql()).stdout)
    assert sql('ALTER ROLE trading_user NOLOGIN;').returncode==0
    assert sql(m.render(manifest,plan,'forward')).returncode==0
    assert authenticate().returncode!=0
    assert sql('ALTER ROLE trading_user LOGIN;').returncode==0
    authenticated=authenticate();assert authenticated.returncode==0 and authenticated.stdout.strip()!='10'
    assert sql('ALTER ROLE trading_user NOLOGIN;').returncode==0
    assert sql(m.render(manifest,plan,'rollback')).returncode==0
    assert sql('ALTER ROLE trading_user LOGIN;').returncode==0
    assert authenticate().stdout.strip()=='10'


def test_postflight_acl_violation_rolls_back_entire_identity_transaction(cluster):
    m=module();sql,manifest,_,_=cluster
    plan=json.loads(sql(m.inventory_sql()).stdout)
    assert sql('ALTER ROLE trading_user NOLOGIN;').returncode==0
    migration=m.render(manifest,plan,'forward').replace('CREATE TEMP TABLE acl_result','GRANT UPDATE ON app.protected TO trading_user; CREATE TEMP TABLE acl_result',1)
    assert sql(migration).returncode!=0
    assert sql("SELECT oid=10 AND NOT rolsuper FROM pg_roles WHERE rolname='trading_user';").stdout.strip()=='t'
    assert sql("SELECT to_regrole('trading_bootstrap_admin') IS NULL;").stdout.strip()=='t'
    assert sql("SELECT has_table_privilege('trading_user','app.protected','UPDATE');").stdout.strip()=='f'


def test_audit_rejects_bootstrap_login_and_password(cluster):
    m=module();sql,manifest,_,_=cluster
    plan=json.loads(sql(m.inventory_sql()).stdout)
    assert sql('ALTER ROLE trading_user NOLOGIN;').returncode==0
    assert sql(m.render(manifest,plan,'forward')).returncode==0
    assert sql("ALTER ROLE trading_bootstrap_admin LOGIN PASSWORD 'test-forbidden';").returncode==0
    result=sql(m.permissions.render_sql(manifest,'audit'))
    assert 'bootstrap_identity_mismatch' in result.stdout


def test_contract_restores_database_connect_when_public_connect_is_revoked(cluster):
    m=module();sql,manifest,_,_=cluster
    manifest['roles']['trading_user']['database']={'required':['CONNECT','TEMPORARY'],'allowed':['CONNECT','TEMPORARY']}
    assert sql('REVOKE CONNECT ON DATABASE trading_platform FROM PUBLIC; GRANT CONNECT,TEMPORARY ON DATABASE trading_platform TO trading_user;').returncode==0
    plan=json.loads(sql(m.inventory_sql()).stdout)
    assert sql('ALTER ROLE trading_user NOLOGIN;').returncode==0
    assert sql(m.render(manifest,plan,'forward')).returncode==0
    assert sql("SELECT has_database_privilege('trading_user',current_database(),'CONNECT');").stdout.strip()=='t'


def test_existing_asyncpg_pool_reconnects_to_new_oid_without_pool_restart(cluster):
    import asyncio
    import asyncpg
    m=module();sql,manifest,env,_=cluster
    async def check():
        pool=await asyncpg.create_pool(host=env['PGHOST'],port=int(env['PGPORT']),database='trading_platform',user='trading_user',password='test-only-password',min_size=1,max_size=1)
        try:
            old_pid=await pool.fetchval('SELECT pg_backend_pid()')
            plan=json.loads(sql(m.inventory_sql()).stdout)
            assert sql('ALTER ROLE trading_user NOLOGIN;').returncode==0
            assert sql(m.render(manifest,plan,'forward')).returncode!=0
            assert sql("SELECT oid=10 AND NOT rolsuper FROM pg_roles WHERE rolname='trading_user';").stdout.strip()=='t'
            # The production runbook drains idle connections after quiescence.
            assert sql('SELECT pg_terminate_backend('+str(old_pid)+');').returncode==0
            assert sql(m.render(manifest,plan,'forward')).returncode==0
            assert sql('ALTER ROLE trading_user LOGIN;').returncode==0
            await asyncio.sleep(0.05)
            assert await pool.fetchval("SELECT oid FROM pg_roles WHERE rolname=current_user")!=10
            assert await pool.fetchval('SELECT pg_backend_pid()')!=old_pid
            assert await pool.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname=current_user") is False
        finally:
            await pool.close()
    asyncio.run(check())


def test_native_autovacuum_and_autoanalyze_recover_under_nologin_bootstrap(cluster):
    import time
    m=module();sql,manifest,_,_=cluster
    assert sql("ALTER TABLE app.protected SET(autovacuum_vacuum_threshold=0,autovacuum_vacuum_scale_factor=0,autovacuum_vacuum_insert_threshold=0,autovacuum_vacuum_insert_scale_factor=0,autovacuum_analyze_threshold=0,autovacuum_analyze_scale_factor=0); INSERT INTO app.protected SELECT n,'before' FROM generate_series(1,200)n; UPDATE app.protected SET n='after'; SELECT pg_stat_force_next_flush();").returncode==0
    plan=json.loads(sql(m.inventory_sql()).stdout)
    assert sql('ALTER ROLE trading_user NOLOGIN;').returncode==0
    assert sql(m.render(manifest,plan,'forward')).returncode==0
    for _ in range(15):
        result=sql("SELECT autovacuum_count>0 AND autoanalyze_count>0 FROM pg_stat_user_tables WHERE relname='protected';")
        if result.stdout.strip()=='t':break
        time.sleep(0.5)
    else:pytest.fail('native maintenance did not recover')


def test_role_setting_drift_blocks_before_rename(cluster):
    m=module();sql,manifest,_,_=cluster
    plan=json.loads(sql(m.inventory_sql()).stdout)
    assert sql("ALTER ROLE trading_user SET search_path TO public,app; ALTER ROLE trading_user NOLOGIN;").returncode==0
    assert sql(m.render(manifest,plan,'forward')).returncode!=0
    assert sql("SELECT to_regrole('trading_bootstrap_admin') IS NULL;").stdout.strip()=='t'


def test_strict_contract_rejects_legacy_oid10_but_accepts_separated_identity(cluster):
    import copy
    m=module();sql,transitional,_,_=cluster
    strict=copy.deepcopy(transitional)
    strict['bootstrap_identity']['allow_legacy_demoted']=False

    rejected=sql(m.permissions.render_sql(strict,'audit'))
    assert rejected.returncode==0,rejected.stderr
    report=json.loads(rejected.stdout)
    assert report['ok'] is False
    assert any(issue['code']=='bootstrap_identity_missing' for issue in report['issues'])

    plan=json.loads(sql(m.inventory_sql()).stdout)
    assert sql('ALTER ROLE trading_user NOLOGIN;').returncode==0
    migrated=sql(m.render(transitional,plan,'forward'))
    assert migrated.returncode==0,migrated.stderr

    accepted=sql(m.permissions.render_sql(strict,'audit'))
    assert accepted.returncode==0,accepted.stderr
    report=json.loads(accepted.stdout)
    assert report['ok'] is True
    assert report['issues']==[]
