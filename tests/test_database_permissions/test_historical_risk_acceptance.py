"""Local real-Postgres proof of receipt authority and append-only risk points."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import pytest

ROOT=Path(__file__).resolve().parents[2]
MIGRATION=ROOT/'database/postgres/migrations/2026-11-27-historical-risk-acceptance.sql'


@pytest.fixture(scope='module')
def database():
    if not all(shutil.which(x) for x in ('initdb','pg_ctl','psql')):
        pytest.skip('local PostgreSQL binaries required')
    with tempfile.TemporaryDirectory(prefix='historical-risk-') as tmp:
        data=Path(tmp)/'data'
        subprocess.run(['initdb','-D',str(data),'-U','risk_test_admin','-A','trust','--no-locale','-E','UTF8'],check=True,capture_output=True)
        subprocess.run(['pg_ctl','-D',str(data),'-l',str(Path(tmp)/'log'),'-o',f"-F -k {tmp} -h '' -p 55447",'-w','start'],check=True,capture_output=True)
        env=dict(os.environ,PGHOST=tmp,PGPORT='55447',PGDATABASE='postgres',PGUSER='risk_test_admin')
        def sql(text):return subprocess.run(['psql','-XAt','-v','ON_ERROR_STOP=1'],input=text,env=env,text=True,capture_output=True)
        try:
            assert sql('CREATE ROLE trading_migration_admin LOGIN; CREATE ROLE trading_user LOGIN; CREATE ROLE hrs_acceptance_reader LOGIN; CREATE SCHEMA trading AUTHORIZATION trading_migration_admin;').returncode==0
            assert sql('SET ROLE trading_migration_admin;\n'+MIGRATION.read_text()).returncode==0
            # Exact authoritative object profiles, scoped to this isolated migration.
            full=json.loads((ROOT/'database/postgres/permissions.json').read_text())
            objects=[r for r in full['objects'] if r.get('schema')=='trading' and r.get('name') in {'historical_risk_acceptance','classic_account_risk_point'}]
            assert len(objects)==2
            manifest={'version':1,'schemas':['trading'],'roles':{r:{'required':True,'schemas':{'trading':['USAGE']}} for r in ['trading_user','hrs_acceptance_reader']},'profiles':full['profiles'],'objects':objects}
            file=Path(tmp)/'manifest.json';file.write_text(json.dumps(manifest))
            result=subprocess.run(['python3',str(ROOT/'scripts/database_permissions.py'),'apply','--local','--manifest',str(file)],env=env,text=True,capture_output=True)
            assert result.returncode==0,result.stdout+result.stderr
            yield sql,env,file
        finally:
            subprocess.run(['pg_ctl','-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True)


def test_policy_is_admin_only_and_risk_points_append_only(database):
    sql,_,_=database
    payload='{"account_id":"default","revision":1}'
    insert="INSERT INTO trading.historical_risk_acceptance(account_id,revision,payload,receipt_sha256) VALUES ('default',1,'"+payload+"','"+'a'*64+"');"
    assert sql('SET ROLE trading_user;'+insert).returncode!=0
    assert sql('SET ROLE trading_migration_admin;'+insert).returncode==0
    assert sql("SET ROLE trading_user;INSERT INTO trading.classic_account_risk_point(account_id,policy_revision,sample_time_ms,payload) VALUES ('default',1,123,'{}');").returncode==0
    for table in ['historical_risk_acceptance','classic_account_risk_point']:
        assert sql('SET ROLE trading_user;SELECT * FROM trading.'+table).returncode==0
        for mutation in ['DELETE FROM trading.'+table,'TRUNCATE trading.'+table,"UPDATE trading."+table+" SET payload='{}'"]:
            assert sql('SET ROLE trading_user;'+mutation).returncode!=0
            assert sql('SET ROLE trading_migration_admin;'+mutation).returncode!=0
    assert sql("SET ROLE trading_user;BEGIN;SELECT pg_advisory_xact_lock(hashtextextended('classic-account-risk-v1:default',0));COMMIT;").returncode==0


def test_schema_and_acl_reapplication_is_idempotent(database):
    sql,env,file=database
    assert sql('SET ROLE trading_migration_admin;\n'+MIGRATION.read_text()).returncode==0
    result=subprocess.run(['python3',str(ROOT/'scripts/database_permissions.py'),'audit','--local','--manifest',str(file)],env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['ok']
    assert sql('SELECT count(*) FROM trading.historical_risk_acceptance').stdout.strip()=='1'
    assert sql('SELECT count(*) FROM trading.classic_account_risk_point').stdout.strip()=='1'
