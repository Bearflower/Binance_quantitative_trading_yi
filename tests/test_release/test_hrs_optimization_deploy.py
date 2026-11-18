from copy import deepcopy
import importlib.util
from pathlib import Path
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sys
import uuid

import pytest


def module():
    path = Path('release/hrs-execution/deploy_optimization.py')
    spec = importlib.util.spec_from_file_location('hrs_release', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_overlay_preserves_other_pins_and_actual_frozen_mounts():
    mod = module()
    previous = {'services': {'hrs-strategy': {'image': 'old', 'environment': {'OLD': '1'}},
                             'btc-eth-aggressive': {'image': 'other-pinned'}}}
    before = deepcopy(previous)
    mounts = [{'Type': 'bind', 'Source': '/root/frozen/config.yaml',
               'Destination': '/app/strategies/hrs/config.yaml', 'RW': False}]
    overlay = mod.release_overlay(previous, 'hrs-image', 'hub-image', mounts, 'a'*40)
    assert previous == before
    assert overlay['services']['btc-eth-aggressive']['image'] == 'other-pinned'
    assert overlay['services']['hrs-strategy']['volumes'][0]['read_only'] is True
    assert overlay['services']['hrs-strategy']['environment']['OLD'] == '1'
    assert overlay['services']['hrs-strategy']['environment']['ENTRY_EQUITY_GATE_STRATEGIES'] == 'hrs'


def test_reporter_role_has_no_trading_permissions_or_api_credentials():
    mod = module()
    sql = mod.reader_grants('trading_platform')
    assert 'INSERT' not in sql and 'UPDATE' not in sql and 'DELETE' not in sql
    assert 'ALL TABLES' not in sql
    assert 'hrs.hrs_decision_evidence' in sql
    assert 'trading.execution_fact_income' in sql


def migration_files(mod, folder, changes=None):
    folder.mkdir(parents=True, exist_ok=True)
    for name in mod.MIGRATIONS:
        (folder/name).write_text((changes or {}).get(name,'SELECT 1;\n'))
    return {name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in mod.MIGRATIONS}


def test_migration_bytes_are_bound_and_only_standalone_wrappers_are_removed(tmp_path):
    mod=module()
    body="BEGIN;\nDO $$ BEGIN PERFORM 1; END $$;\nCOMMIT;\n"
    hashes=migration_files(mod,tmp_path,{'2026-11-17-hrs-allocation-event-audit.sql':body})
    sql=mod.migration_sql(tmp_path,hashes)
    assert sql.startswith('BEGIN;') and sql.rstrip().endswith('COMMIT;')
    assert sum(line.strip()=='BEGIN;' for line in sql.splitlines())==1
    assert sum(line.strip()=='COMMIT;' for line in sql.splitlines())==1
    assert 'DO $$ BEGIN PERFORM 1; END $$;' in sql
    (tmp_path/mod.MIGRATIONS[0]).write_text('SELECT 2;')
    with pytest.raises(ValueError,match='migration_hash_mismatch'):
        mod.migration_sql(tmp_path,hashes)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv('HRS_TEST_POSTGRES_DSN'),reason='isolated PostgreSQL required')
async def test_real_postgres_later_migration_failure_rolls_back_every_earlier_change(tmp_path):
    import asyncpg
    mod=module();schema='deploy_atomic_'+uuid.uuid4().hex
    changes={mod.MIGRATIONS[0]:f'CREATE TABLE {schema}.first_change(id int);',
             mod.MIGRATIONS[2]:f'BEGIN;\nCREATE TABLE {schema}.middle_change(id int);\nCOMMIT;\n',
             mod.MIGRATIONS[3]:'SELECT 1/0;'}
    hashes=migration_files(mod,tmp_path,changes)
    connection=await asyncpg.connect(os.environ['HRS_TEST_POSTGRES_DSN'])
    try:
        await connection.execute(f'CREATE SCHEMA {schema}')
        with pytest.raises(asyncpg.DivisionByZeroError):
            await connection.execute(mod.migration_sql(tmp_path,hashes))
        await connection.execute('ROLLBACK')
        assert await connection.fetchval('SELECT to_regclass($1)',schema+'.first_change') is None
        assert await connection.fetchval('SELECT to_regclass($1)',schema+'.middle_change') is None
    finally:
        await connection.execute(f'DROP SCHEMA {schema} CASCADE')
        await connection.close()


def test_named_actual_volumes_are_declared_external_without_changing_other_pins():
    mod=module()
    result=mod.release_overlay({'services':{}},'hrs','hub',[
        dict(Type='volume',Name='trading_system_actual_logs',Source='/var/lib/docker/volumes/x',
             Destination='/app/logs',RW=True)],'a'*40)
    source=result['services']['hrs-strategy']['volumes'][0]['source']
    assert result['volumes'][source]=={'external':True,'name':'trading_system_actual_logs'}


def test_environment_comparison_includes_removed_and_image_inherited_values():
    mod=module();sha='a'*40
    actual={'PATH':'/usr/bin','OLD_RISK_SETTING':'enabled','SECRET':'synthetic-sensitive'}
    drift=mod.environment_drift(actual,{'PATH':'/usr/bin','HRS_RELEASE_SHA':sha},{},sha)
    assert set(drift)=={'OLD_RISK_SETTING','SECRET'}
    assert 'synthetic-sensitive' not in str(drift)
    assert mod.environment_drift({'PATH':'/usr/bin'}, {'PATH':'/usr/bin','HRS_RELEASE_SHA':sha},
                                 {'HRS_EVIDENCE_CAPTURE_ENABLED':'1'},sha)==[]
    assert 'HRS_RELEASE_SHA' in mod.environment_drift({}, {'HRS_RELEASE_SHA':'wrong'}, {}, sha)


@pytest.mark.parametrize('previous_sha',[None,'b'*40])
def test_overlay_binds_candidate_sha_after_rollback_without_relaxing_other_environment(previous_sha):
    mod=module();sha='a'*40
    previous={'services':{'hrs-strategy':{'environment':{
        'HRS_RELEASE_SHA':previous_sha,'DATABASE_PASSWORD':'synthetic-private-value',
        'UNCHANGED_SETTING':'retained'}}}}
    original=deepcopy(previous)
    overlay=mod.release_overlay(previous,'candidate-hrs','candidate-hub',[],sha)
    proposed=overlay['services']['hrs-strategy']['environment']
    assert proposed['HRS_RELEASE_SHA']==sha
    assert proposed['DATABASE_PASSWORD']=='synthetic-private-value'
    assert proposed['UNCHANGED_SETTING']=='retained'
    assert previous==original
    actual={'HRS_RELEASE_SHA':previous_sha,'DATABASE_PASSWORD':'synthetic-private-value',
            'UNCHANGED_SETTING':'retained'}
    assert mod.environment_drift(actual,{'HRS_RELEASE_SHA':sha},proposed,sha)==[]
    proposed['DATABASE_PASSWORD']='different-synthetic-value'
    assert mod.environment_drift(actual,{'HRS_RELEASE_SHA':sha},proposed,sha)==['DATABASE_PASSWORD']


def initialization(now,sha,config):
    return dict(release_sha=sha,loaded_config={'strategy':'hrs','config_hash':config},
        config_mtime=(now+timedelta(seconds=1)).timestamp(),checked_at=(now+timedelta(seconds=30)).isoformat(),
        gate={'updated_at':(now+timedelta(seconds=20)).isoformat(),'ready':False,'allowed':False,
              'reasons':['daily_origin_missing'],'evidence_sha256':'b'*64},
        writer_recorded_at=(now+timedelta(seconds=21)).isoformat(),evidence_context_serializable=True)


@pytest.mark.parametrize('serializable',[None,False,'true',1])
def test_equity_heartbeat_without_serializable_scan_database_context_is_not_initialized(serializable):
    mod=module();now=datetime.now(timezone.utc);sha='a'*40;config='c'*64
    data=initialization(now,sha,config)
    if serializable is None:data.pop('evidence_context_serializable')
    else:data['evidence_context_serializable']=serializable
    with pytest.raises(RuntimeError,match='initialization_unproven'):
        mod.initialized_proof(data,now,sha,config)
    diagnostic=mod.safe_initialization(data,now,sha,config)
    assert diagnostic['evidence_context_serializable'] is False


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv('HRS_TEST_POSTGRES_DSN'),reason='isolated PostgreSQL required')
async def test_real_postgres_readonly_probe_serializes_legacy_claim_timestamps_and_infinity():
    import asyncpg
    mod=module();dsn=os.environ['HRS_TEST_POSTGRES_DSN']
    control=await asyncpg.connect(dsn);name='hrs_probe_context_'+uuid.uuid4().hex
    role='hrs_probe_reader_'+uuid.uuid4().hex
    admin=app=None
    try:
        await control.execute(f'CREATE ROLE {role} NOLOGIN NOSUPERUSER')
        await control.execute(f"CREATE DATABASE {name} TEMPLATE template0 ENCODING 'UTF8'")
        admin=await asyncpg.connect(dsn,database=name)
        await admin.execute('''CREATE SCHEMA trading; CREATE SCHEMA hrs;
            CREATE TABLE trading.strategy_risk_policy(account_id text,strategy_id text,revision bigint);
            CREATE TABLE trading.strategy_risk_active(account_id text,strategy_id text,revision bigint);
            CREATE TABLE trading.hrs_loss_gate_state(account_id text);
            CREATE TABLE hrs.hrs_positions(created_at timestamptz);
            CREATE TABLE hrs.hrs_blacklist(symbol text);
            CREATE TABLE hrs.hrs_active_symbols(symbol text);
            CREATE TABLE trading.entry_risk_reservation(account_id text,strategy_id text);
            CREATE TABLE trading.position_claims(claim_state text,created_at timestamp,updated_at timestamp,expires_at timestamp);
            CREATE TABLE trading.hrs_equity_evidence(account_id text,evidence_sha256 text,segment_id text,as_of timestamptz,available_at timestamptz,payload jsonb);
            INSERT INTO trading.position_claims VALUES('ACTIVE','2026-11-18 12:00:00','2026-11-18 12:01:00','infinity');
            INSERT INTO hrs.hrs_positions VALUES('2026-11-18 12:00:00+08')''')
        await admin.execute(f'GRANT USAGE ON SCHEMA trading,hrs TO {role}; GRANT SELECT ON ALL TABLES IN SCHEMA trading,hrs TO {role}')
        app=await asyncpg.connect(dsn,database=name,server_settings={'default_transaction_read_only':'on'})
        await app.execute(f'SET ROLE {role}')
        assert not await app.fetchval('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
        namespace={}
        exec(compile(mod.INITIALIZATION.rsplit('asyncio.run(main())',1)[0],'initialization-probe','exec'),namespace)
        assert await namespace['check_evidence_context'](app,'test') is True
        # The probe may neither write nor silently accept other naive clocks.
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await app.execute("INSERT INTO hrs.hrs_positions VALUES(now())")
        await admin.execute('ALTER TABLE hrs.hrs_positions ALTER COLUMN created_at TYPE timestamp;')
        await app.reload_schema_state()
        with pytest.raises(ValueError,match='naive_evidence_timestamp'):
            await namespace['check_evidence_context'](app,'test')
    finally:
        if app is not None:await app.close()
        if admin is not None:await admin.close()
        await control.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')
        await control.execute(f'DROP ROLE IF EXISTS {role}')
        await control.close()


@pytest.mark.parametrize('broken',['old_file','wrong_hash','old_gate','missing_writer','wrong_release'])
def test_application_proof_never_accepts_always_successful_healthcheck(broken):
    mod=module();now=datetime.now(timezone.utc);sha='a'*40;config='c'*64
    data=initialization(now,sha,config)
    if broken=='old_file':data['config_mtime']=(now-timedelta(seconds=1)).timestamp()
    elif broken=='wrong_hash':data['loaded_config']['config_hash']='d'*64
    elif broken=='old_gate':data['gate']['updated_at']=(now-timedelta(seconds=1)).isoformat()
    elif broken=='missing_writer':data['writer_recorded_at']=None
    else:data['release_sha']='d'*40
    with pytest.raises(RuntimeError,match='initialization_unproven'):
        mod.initialized_proof(data,now,sha,config)
    proof=mod.initialized_proof(initialization(now,sha,config),now,sha,config)
    assert proof['allowed'] is False and proof['reasons']==['daily_origin_missing']


def fake_rollout(tmp_path,monkeypatch,*,fail_hrs=False,proof_ok=True,existing_reporter=False):
    mod=module();root=tmp_path;sha='a'*40;folder=root/'.releases/hrs-optimization'/sha
    hashes=migration_files(mod,folder/'migrations')
    hrs='ghcr.io/bearflower/trading-hrs@sha256:'+'1'*64
    hub='ghcr.io/bearflower/trading-hrs@sha256:'+'2'*64
    env=dict(DATABASE_HOST='postgres',DATABASE_NAME='synthetic',AI_TUNER_SNAPSHOT_DB_USER='fake',
             AI_TUNER_SNAPSHOT_DB_PASSWORD='synthetic-only',PATH='/usr/bin')
    old_overlay={'services':{'hrs-strategy':{'image':'old-hrs'},'market-cap-service':{'image':'old-hub'}}}
    if existing_reporter:
        old_overlay['services']['hrs-acceptance-reporter']={'image':'old-reporter'}
    old=root/'old.json';old.write_text(json.dumps(old_overlay))
    pin=root/'.deploy_tmp/ledger-risk-active.json';pin.parent.mkdir();pin.write_text(json.dumps(
        {'sha':'old','overlay':str(old),'status':'active'}))
    manifest=root/'manifest.json';manifest.write_text(json.dumps(dict(source_hashes={'shared/equity_entry_gate.py':'f'*64},
        migration_hashes=hashes,expected_config_hash='c'*64)))
    started=datetime.now(timezone.utc)-timedelta(seconds=60)
    containers={}
    for name,image in [('trading_system-hrs','old-hrs'),('trading_system-market-cap','old-hub')]:
        containers[name]=dict(Id='old-'+name,Config=dict(Image=image,Env=[k+'='+v for k,v in env.items()],
            Labels={'com.docker.compose.project.config_files':str(root/'compose.yml')}),Mounts=[],
            NetworkSettings={'Networks':{'shared':{}}},State={'Status':'running','Health':{'Status':'healthy'},'StartedAt':started.isoformat()})
    containers['trading_system-postgres']=dict(Id='old-db',NetworkSettings={'Networks':{'shared':{}}})
    if existing_reporter:
        containers['trading_system-hrs-acceptance']=dict(Id='old-reporter',Config={'Image':'old-reporter'},Mounts=[],
            State={'Status':'running','Health':{'Status':'healthy'},'StartedAt':started.isoformat()})
    calls=[]
    def inspect(name):
        if name not in containers:raise RuntimeError('container_missing')
        return deepcopy(containers[name])
    def run(args,**kwargs):
        args=list(map(str,args));calls.append(args)
        if args[:3] in (['docker','ps','-q'],['docker','ps','-aq']):return ' '.join(containers)
        if args[:2]==['docker','inspect']:
            return json.dumps([dict(Name='/'+n,Id=c['Id']) for n,c in containers.items()])
        if args[:3]==['docker','image','inspect']:
            return json.dumps([{'Config':{'Env':['PATH=/usr/bin','HRS_RELEASE_SHA='+sha]}}])
        if 'config' in args:return json.dumps({'services':{'hrs-strategy':{'environment':env}}})
        if 'up' in args:
            service=args[-1];file=Path(args[max(i for i,a in enumerate(args) if a=='-f')+1])
            overlay=json.loads(file.read_text());image=overlay['services'][service]['image']
            if service=='hrs-strategy' and fail_hrs and image==hrs:raise RuntimeError('synthetic_recreate_failure')
            name={'hrs-strategy':'trading_system-hrs','market-cap-service':'trading_system-market-cap',
                  'hrs-acceptance-reporter':'trading_system-hrs-acceptance'}[service]
            containers[name]=dict(Id='new-'+name,Config={'Image':image,'Labels':{
                'com.docker.compose.project':'trading_system','com.docker.compose.service':service}},Mounts=[],
                State={'Status':'running','Health':{'Status':'healthy'},'StartedAt':started.isoformat()})
        if args[:3]==['docker','rm','-f']:containers.pop(args[-1],None)
        return ''
    def read_initialization(*args):
        if not proof_ok:raise RuntimeError('initialization_unproven')
        return initialization(started,sha,'c'*64)
    monkeypatch.setattr(mod,'inspect',inspect);monkeypatch.setattr(mod,'run',run)
    monkeypatch.setattr(mod,'preflight',lambda:dict(account_id='audit',positions=[]))
    monkeypatch.setattr(mod,'read_initialization',read_initialization)
    monkeypatch.setattr(mod.time,'sleep',lambda seconds:None)
    monkeypatch.setattr(sys,'argv',['deploy','--sha',sha,'--hrs-image',hrs,'--hub-image',hub,
        '--root',str(root),'--manifest',str(manifest),'--apply'])
    return mod,containers,calls,folder,pin


def test_partial_replacement_rolls_back_without_touching_other_containers(tmp_path,monkeypatch):
    mod,containers,calls,folder,pin=fake_rollout(tmp_path,monkeypatch,fail_hrs=True)
    with pytest.raises(RuntimeError,match='rollout_failed'):
        mod.main()
    assert containers['trading_system-market-cap']['Config']['Image']=='old-hub'
    assert containers['trading_system-hrs']['Config']['Image']=='old-hrs'
    assert containers['trading_system-postgres']['Id']=='old-db'
    state=json.loads((folder/'state.json').read_text())
    assert state['status']=='failed' and state['rollback']['status']=='complete'
    assert json.loads(pin.read_text())['sha']=='old'
    before=(folder/'before.json').read_bytes()
    with pytest.raises(RuntimeError,match='rollout_failed'):
        mod.main()
    assert (folder/'before.json').read_bytes()==before


@pytest.mark.parametrize('existing',[False,True])
def test_healthy_container_without_new_initialization_proof_rolls_back_reporter_safely(tmp_path,monkeypatch,existing):
    mod,containers,calls,folder,pin=fake_rollout(tmp_path,monkeypatch,proof_ok=False,existing_reporter=existing)
    with pytest.raises(RuntimeError,match='rollout_failed'):
        mod.main()
    name='trading_system-hrs-acceptance'
    if existing:
        assert containers[name]['Config']['Image']=='old-reporter'
        assert not any(c[:3]==['docker','rm','-f'] and c[-1]==name for c in calls)
    else:
        assert name not in containers
    state=json.loads((folder/'state.json').read_text())
    assert state['status']=='failed' and state['rollback']['status']=='complete'
    assert 'complete'!=state['status']
    secret=tmp_path/'.secrets/hrs-acceptance.env'
    assert secret.stat().st_mode&0o777==0o600


def test_success_requires_application_proof_but_honest_denial_is_healthy(tmp_path,monkeypatch):
    mod,containers,calls,folder,pin=fake_rollout(tmp_path,monkeypatch)
    mod.main()
    state=json.loads((folder/'state.json').read_text())
    assert state['status']=='complete'
    assert state['initialization']['allowed'] is False
    assert state['initialization']['reasons']==['daily_origin_missing']
    assert state['other_containers_unchanged'] is True
    assert json.loads(pin.read_text())['sha']=='a'*40
    assert containers['trading_system-postgres']['Id']=='old-db'
    keys={line.split('=',1)[0] for line in (tmp_path/'.secrets/hrs-acceptance.env').read_text().splitlines()}
    assert keys=={'DATABASE_URL','TRADE_ACCOUNT_ID'}


def test_failed_rollback_is_explicit_and_does_not_claim_old_pin_active(tmp_path,monkeypatch):
    mod,containers,calls,folder,pin=fake_rollout(tmp_path,monkeypatch,fail_hrs=True)
    original=mod.run
    def run(args,**kwargs):
        if 'up' in args and any(str(a).endswith('rollback-images.json') for a in args) and args[-1]=='market-cap-service':
            raise RuntimeError('synthetic_rollback_failure')
        return original(args,**kwargs)
    monkeypatch.setattr(mod,'run',run)
    with pytest.raises(RuntimeError,match='rollout_failed_rollback_incomplete'):
        mod.main()
    state=json.loads((folder/'state.json').read_text())
    assert state['status']=='failed' and state['rollback']['status']=='incomplete'
    assert json.loads(pin.read_text())['status']=='rollback_failed'


def test_initialization_probe_reports_permission_failure_without_exception_text(monkeypatch,capsys):
    from types import SimpleNamespace
    mod=module()
    class InsufficientPrivilegeError(Exception):pass
    async def connect(**kwargs):raise InsufficientPrivilegeError('PASSWORD=synthetic-private-value')
    monkeypatch.setitem(sys.modules,'asyncpg',SimpleNamespace(connect=connect))
    for key in ('DATABASE_HOST','DATABASE_NAME','DATABASE_USER','DATABASE_PASSWORD'):
        monkeypatch.setenv(key,'synthetic-private-value')
    exec(compile(mod.INITIALIZATION,'initialization-probe','exec'),{})
    output=capsys.readouterr().out
    data=json.loads(next(line.split('=',1)[1] for line in output.splitlines() if line.startswith('HRS_INITIALIZATION=')))
    assert data['status']=='error'
    assert data['stage']=='collector_connect'
    assert data['error_code']=='database_permission_denied'
    assert 'synthetic-private-value' not in output


def test_initialization_attempts_are_saved_before_rollback_with_only_allowlisted_fields(tmp_path,monkeypatch):
    mod,containers,calls,folder,pin=fake_rollout(tmp_path,monkeypatch)
    monkeypatch.setattr(mod,'read_initialization',lambda:dict(status='error',stage='collector_projection_read',
        error_code='database_permission_denied',stderr='PASSWORD=synthetic-private-value',
        gate={'ready':False,'allowed':False,'reasons':['InsufficientPrivilegeError','secret=synthetic-private-value']},
        environment={'SECRET':'synthetic-private-value'}))
    original=mod.rollback
    def rollback(*args):
        pending=json.loads((folder/'state.json').read_text())
        assert pending['status']=='failing'
        assert pending['error_code']=='candidate_initialization_timeout'
        observations=pending['observations']
        assert len(observations)==90
        assert observations[-1]['initialization']['stage']=='collector_projection_read'
        assert observations[-1]['initialization']['error_code']=='database_permission_denied'
        assert observations[-1]['initialization']['gate']=={
            'ready':False,'allowed':False,'reasons':['InsufficientPrivilegeError'],'unknown_reason_count':1}
        assert observations[-1]['containers']['trading_system-hrs']['health']=='healthy'
        assert 'synthetic-private-value' not in json.dumps(pending)
        return original(*args)
    monkeypatch.setattr(mod,'rollback',rollback)
    with pytest.raises(RuntimeError,match='rollout_failed_rollback_complete'):mod.main()
    state=json.loads((folder/'state.json').read_text())
    assert state['error_code']=='candidate_initialization_timeout'
    assert state['observations'][-1]['initialization']['error_code']=='database_permission_denied'


def test_failed_process_health_is_saved_without_docker_logs_or_raw_errors(tmp_path,monkeypatch):
    mod,containers,calls,folder,pin=fake_rollout(tmp_path,monkeypatch)
    inspect=mod.inspect
    def failed(name):
        data=inspect(name)
        if name=='trading_system-hrs' and data['Config']['Image'].startswith('ghcr.io/'):
            data['State'].update(Status='exited',ExitCode=17,Error='synthetic-private-value',
                Health={'Status':'unhealthy','Log':[{'Output':'synthetic-private-value'}]})
        return data
    monkeypatch.setattr(mod,'inspect',failed)
    with pytest.raises(RuntimeError,match='rollout_failed'):mod.main()
    state=json.loads((folder/'state.json').read_text())
    assert state['error_code']=='candidate_process_failed'
    observed=state['observations'][-1]['containers']['trading_system-hrs']
    assert observed['status']=='exited' and observed['health']=='unhealthy' and observed['exit_code']==17
    assert 'synthetic-private-value' not in json.dumps(state)


def test_each_initialization_exec_failure_is_retained_without_arbitrary_exception_text(tmp_path,monkeypatch):
    mod,containers,calls,folder,pin=fake_rollout(tmp_path,monkeypatch)
    count=0
    def read():
        nonlocal count
        count+=1
        if count==1:raise RuntimeError('docker failed with exit 137')
        raise RuntimeError('PASSWORD=synthetic-private-value')
    monkeypatch.setattr(mod,'read_initialization',read)
    with pytest.raises(RuntimeError,match='rollout_failed'):mod.main()
    state=json.loads((folder/'state.json').read_text())
    assert len(state['observations'])==count==90
    assert state['observations'][0]['initialization']['error_code']=='command_failed'
    assert state['observations'][0]['initialization']['exit_code']==137
    assert state['observations'][1]['initialization']['error_code']=='unclassified_error'
    assert 'synthetic-private-value' not in json.dumps(state)


def test_initialization_probe_preserves_projection_when_independent_writer_is_denied(monkeypatch,capsys):
    from types import SimpleNamespace
    mod=module()
    class InsufficientPrivilegeError(Exception):pass
    class Connection:
        def transaction(self,**kwargs):
            class Transaction:
                async def __aenter__(self):pass
                async def __aexit__(self,*args):pass
            return Transaction()
        async def fetch(self,*args):return []
        async def fetchrow(self,*args):
            return {'ready':False,'allowed':False,'reasons':'["InsufficientPrivilegeError"]',
                'updated_at':datetime.now(timezone.utc),'evidence_sha256':'a'*64}
        async def close(self):pass
    calls=0
    async def connect(**kwargs):
        nonlocal calls
        calls+=1
        if calls==2:raise InsufficientPrivilegeError('synthetic-private-value')
        return Connection()
    monkeypatch.setitem(sys.modules,'asyncpg',SimpleNamespace(connect=connect))
    for key in ('DATABASE_HOST','DATABASE_NAME','DATABASE_USER','DATABASE_PASSWORD',
                'AI_TUNER_SNAPSHOT_DB_USER','AI_TUNER_SNAPSHOT_DB_PASSWORD'):
        monkeypatch.setenv(key,'synthetic-private-value')
    exec(compile(mod.INITIALIZATION,'initialization-probe','exec'),{})
    output=capsys.readouterr().out
    data=json.loads(output.split('HRS_INITIALIZATION=',1)[1])
    assert data['stage']=='writer_connect' and data['error_code']=='database_permission_denied'
    assert data['gate']['ready'] is False
    assert data['gate']['reasons']==['InsufficientPrivilegeError']
    assert 'synthetic-private-value' not in output


def test_failure_audit_write_error_cannot_prevent_target_rollback(tmp_path,monkeypatch):
    mod,containers,calls,folder,pin=fake_rollout(tmp_path,monkeypatch,fail_hrs=True)
    atomic=mod.atomic
    def disk_error(path,value):
        if Path(path).name=='state.json' and value.get('status')=='failing':
            raise OSError('synthetic-private-value')
        return atomic(path,value)
    monkeypatch.setattr(mod,'atomic',disk_error)
    with pytest.raises(RuntimeError,match='rollout_failed_rollback_complete'):mod.main()
    assert containers['trading_system-market-cap']['Config']['Image']=='old-hub'
    state=json.loads((folder/'state.json').read_text())
    assert state['pre_rollback_audit_persisted'] is False
    assert state['rollback']['status']=='complete'
    assert 'synthetic-private-value' not in json.dumps(state)
