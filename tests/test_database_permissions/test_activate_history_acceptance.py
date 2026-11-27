"""Administrator receipt CAS and revocation are serialized with live risk writers."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

import asyncpg
import pytest
import pytest_asyncio

from shared.accounting.history_tolerance import acceptance_digest, read_history_acceptance

ROOT = Path(__file__).resolve().parents[2]


def payload(revision=1, enabled=True):
    return dict(kind='historical_risk_acceptance_v1', account_id='default', revision=revision,
        enabled=enabled, exchange_uid_sha256='a'*64, classic_cutover_ms=1795582464000,
        accepted_from='2026-10-31T16:00:00+00:00', accepted_through='2026-11-26T07:59:00+00:00',
        effective_at='2026-11-26T08:00:00+00:00', classic_flat_at='2026-11-25T05:32:58.143000+00:00',
        classic_flat_evidence_sha256='b'*64, classic_open_equity='391.65638581',
        risk_limit_ratio='0.05', hrs_daily_limit='0.03', hrs_rolling_limit='0.06',
        classic_risk_method='classic_fixed_capital_cashflow_adjusted_drawdown_v1',
        automatic_allocation=True, approval_ref='operator:2026-11-27',
        allowed_gap_codes=['pm_native_income_coverage', 'historical_nav_sampling'],
        classic_known_equity_points=[dict(as_of_ms=1795584778143, equity_usdt='380',
            net_transfer_usdt='0', source_sha256='c'*64)],
        allocation_ceilings_usdt={s:'100' for s in ('btc_eth', 'btc_eth_aggressive', 'hrs', 'new_coin')},
        allocation_anchor_id=12,allocation_anchor_entries_sha256='f'*64)


@pytest.fixture(scope='module')
def postgres_server():
    if not all(shutil.which(binary) for binary in ('initdb', 'pg_ctl', 'psql')):
        pytest.skip('local PostgreSQL binaries required')
    with tempfile.TemporaryDirectory(prefix='acceptance-activation-') as tmp:
        data = Path(tmp)/'data'
        subprocess.run(['initdb','-D',str(data),'-U','activation_admin','-A','trust','--no-locale','-E','UTF8'], check=True,capture_output=True)
        subprocess.run(['pg_ctl','-D',str(data),'-l',str(Path(tmp)/'log'),'-o',f"-F -k {tmp} -h '' -p 55450",'-w','start'],check=True,capture_output=True)
        env = dict(os.environ,PGHOST=tmp,PGPORT='55450',PGDATABASE='postgres',PGUSER='activation_admin')
        try:
            subprocess.run(['psql','-XAt','-v','ON_ERROR_STOP=1'],input='CREATE ROLE trading_migration_admin LOGIN; CREATE ROLE trading_user LOGIN; CREATE ROLE hrs_acceptance_reader LOGIN;',env=env,text=True,capture_output=True,check=True)
            yield tmp, env
        finally:
            subprocess.run(['pg_ctl','-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True)


@pytest_asyncio.fixture
async def database(postgres_server, monkeypatch):
    monkeypatch.setenv('BINANCE_CLASSIC_CUTOVER_MS','1795582464000')
    host, env = postgres_server
    name = 'activation_'+uuid.uuid4().hex
    control = await asyncpg.connect(host=host,port=55450,user='activation_admin',database='postgres')
    await control.execute('CREATE DATABASE '+name)
    setup = await asyncpg.connect(host=host,port=55450,user='activation_admin',database=name)
    await setup.execute('CREATE SCHEMA trading AUTHORIZATION trading_migration_admin; SET ROLE trading_migration_admin;')
    await setup.execute((ROOT/'database/postgres/migrations/2026-11-27-historical-risk-acceptance.sql').read_text())
    gate = (ROOT/'database/postgres/migrations/2026-11-17-hrs-equity-service.sql').read_text().split('CREATE TABLE IF NOT EXISTS trading.hrs_equity_segment')[0]
    await setup.execute(gate)
    await setup.close()
    full = json.loads((ROOT/'database/postgres/permissions.json').read_text())
    objects = [row for row in full['objects'] if row.get('schema')=='trading' and row.get('name') in {'historical_risk_acceptance','classic_account_risk_point','strategy_equity_gate'}]
    manifest=dict(version=1,schemas=['trading'],profiles=full['profiles'],objects=objects,
        roles={r:dict(required=True,schemas={'trading':['USAGE']}) for r in ['trading_user','hrs_acceptance_reader']})
    path=Path(host)/(name+'.json');path.write_text(json.dumps(manifest))
    applied=subprocess.run(['python3',str(ROOT/'scripts/database_permissions.py'),'apply','--local','--manifest',str(path)],env=dict(env,PGDATABASE=name),text=True,capture_output=True)
    assert applied.returncode==0,applied.stdout+applied.stderr
    connections=[]
    async def connect(role='trading_migration_admin'):
        conn=await asyncpg.connect(host=host,port=55450,user=role,database=name)
        connections.append(conn)
        return conn
    try:
        yield connect, dict(env,PGDATABASE=name,PGUSER='trading_migration_admin',BINANCE_CLASSIC_CUTOVER_MS='1795582464000')
    finally:
        for conn in connections:await conn.close()
        await control.execute('DROP DATABASE '+name)
        await control.close()


async def ready_gate(conn):
    await conn.execute("INSERT INTO trading.strategy_equity_gate VALUES ('default','hrs',8,true,true,clock_timestamp(),clock_timestamp()+interval '90 seconds','[\"existing_reason\"]',$1,clock_timestamp())",'d'*64)


async def wait_for_advisory_wait(observer, blocked):
    for _ in range(100):
        if await observer.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND locktype='advisory' AND NOT granted)",blocked.get_server_pid()):
            return
        await asyncio.sleep(.01)
    raise AssertionError('activation did not wait on the expected advisory lock')


@pytest.mark.asyncio
async def test_admin_append_idempotence_cas_and_atomic_invalidation(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect();await ready_gate(admin)
    first=payload()
    result=await append_acceptance(admin,first,0,None)
    assert result['status']=='applied'
    gate=await admin.fetchrow("SELECT * FROM trading.strategy_equity_gate")
    assert not gate['ready'] and not gate['allowed'] and gate['valid_until']<=datetime.now(timezone.utc)
    assert json.loads(gate['reasons'])==['existing_reason','history_acceptance_changed']
    assert (await append_acceptance(admin,first,0,None))['status']=='already_applied'
    assert await admin.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==1
    with pytest.raises(ValueError,match='compare_and_swap'):
        await append_acceptance(admin,payload(2),0,None)
    with pytest.raises(ValueError,match='compare_and_swap'):
        await append_acceptance(admin,payload(2),1,'e'*64)
    await append_acceptance(admin,payload(2,False),1,acceptance_digest(first))
    with pytest.raises(ValueError,match='compare_and_swap'):
        await append_acceptance(admin,first,0,None)
    assert await read_history_acceptance(admin,'default') is None
    assert await admin.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==2


@pytest.mark.asyncio
async def test_runtime_cannot_activate_or_insert_but_can_read_old_receipt(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect();runtime=await connect('trading_user')
    await append_acceptance(admin,payload(),0,None)
    with pytest.raises(ValueError,match='administrator_required'):
        await append_acceptance(runtime,payload(2,False),1,acceptance_digest(payload()))
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await runtime.execute('INSERT INTO trading.historical_risk_acceptance VALUES ($1,$2,$3::jsonb,$4,now())','default',2,json.dumps(payload(2,False)),acceptance_digest(payload(2,False)))
    await append_acceptance(admin,payload(2,False),1,acceptance_digest(payload()))
    assert await runtime.fetchval('SELECT payload->>\'enabled\' FROM trading.historical_risk_acceptance WHERE revision=1')=='true'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('exchange_uid_sha256','e'*64),('classic_open_equity','1000'),('classic_flat_at','2026-11-25T06:00:00+00:00'),('classic_flat_evidence_sha256','e'*64),('classic_known_equity_points',[]),('allocation_anchor_id',13),('allocation_anchor_entries_sha256','e'*64)])
async def test_reactivation_cannot_reset_baseline_or_drop_known_losses(database,field,value):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect()
    await append_acceptance(admin,payload(),0,None)
    revoked=payload(2,False);await append_acceptance(admin,revoked,1,acceptance_digest(payload()))
    candidate=payload(3);candidate[field]=value
    with pytest.raises(ValueError):await append_acceptance(admin,candidate,2,acceptance_digest(revoked))
    assert await admin.fetchval('SELECT max(revision) FROM trading.historical_risk_acceptance')==2


@pytest.mark.asyncio
async def test_known_point_amendment_is_rejected_but_addition_is_preserved(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect();await append_acceptance(admin,payload(),0,None)
    changed=payload(2);changed['classic_known_equity_points'][0]['equity_usdt']='391.65638581'
    with pytest.raises(ValueError,match='known_equity'):await append_acceptance(admin,changed,1,acceptance_digest(payload()))
    added=payload(2);added['classic_known_equity_points'].append(dict(as_of_ms=1795584779143,equity_usdt='370',net_transfer_usdt='0',source_sha256='f'*64))
    assert (await append_acceptance(admin,added,1,acceptance_digest(payload())))['status']=='applied'


@pytest.mark.asyncio
async def test_dry_run_and_cli_default_do_not_mutate(database,tmp_path):
    from scripts.activate_history_acceptance import append_acceptance
    connect,env=database;admin=await connect();await ready_gate(admin)
    assert (await append_acceptance(admin,payload(),0,None,dry_run=True))['status']=='dry_run'
    assert await admin.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==0
    assert await admin.fetchval('SELECT allowed FROM trading.strategy_equity_gate')
    path=tmp_path/'receipt.json';path.write_text(json.dumps(payload()))
    process=await asyncio.create_subprocess_exec(sys.executable,'-m','scripts.activate_history_acceptance','--payload',str(path),'--expected-revision','0','--expected-sha','none',cwd=ROOT,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr=await process.communicate()
    assert process.returncode==0,stderr.decode()
    assert json.loads(stdout)['status']=='dry_run'
    assert await admin.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==0


@pytest.mark.asyncio
async def test_revocation_waits_for_publisher_then_invalidates_its_allowed_projection(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect();publisher=await connect('trading_user')
    await append_acceptance(admin,payload(),0,None);await ready_gate(admin)
    async with publisher.transaction(isolation='read_committed'):
        await publisher.execute("SELECT pg_advisory_xact_lock(hashtextextended('classic-account-risk-v1:default',0))")
        await publisher.execute("SELECT pg_advisory_xact_lock(hashtextextended('default',7183))")
        task=asyncio.create_task(append_acceptance(admin,payload(2,False),1,acceptance_digest(payload())))
        await wait_for_advisory_wait(publisher,admin)
        assert not task.done()
        await publisher.execute('UPDATE trading.strategy_equity_gate SET ready=true,allowed=true')
    assert (await task)['status']=='applied'
    assert not await admin.fetchval('SELECT allowed FROM trading.strategy_equity_gate')


@pytest.mark.asyncio
async def test_revocation_waits_for_hrs_session_lock_then_invalidates_its_projection(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect();hrs=await connect('trading_user')
    await append_acceptance(admin,payload(),0,None);await ready_gate(admin)
    await hrs.execute("SELECT pg_advisory_lock(hashtextextended('default',7183))")
    task=asyncio.create_task(append_acceptance(admin,payload(2,False),1,acceptance_digest(payload())))
    try:
        await wait_for_advisory_wait(hrs,admin);assert not task.done()
        await hrs.execute('UPDATE trading.strategy_equity_gate SET ready=true,allowed=true')
    finally:await hrs.execute("SELECT pg_advisory_unlock(hashtextextended('default',7183))")
    await task
    assert not await admin.fetchval('SELECT allowed FROM trading.strategy_equity_gate')


@pytest.mark.asyncio
async def test_two_activation_attempts_with_same_parent_cannot_both_succeed(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;one=await connect();two=await connect()
    first=payload();second=payload();second['approval_ref']='another approval'
    results=await asyncio.gather(append_acceptance(one,first,0,None),append_acceptance(two,second,0,None),return_exceptions=True)
    assert sum(isinstance(r,dict) and r['status']=='applied' for r in results)==1
    assert sum(isinstance(r,ValueError) for r in results)==1
    assert await one.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==1


@pytest.mark.asyncio
async def test_gate_invalidation_failure_rolls_back_receipt(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect();await ready_gate(admin)
    await admin.execute("CREATE FUNCTION trading.fail_gate_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test gate failure'; END $$; CREATE TRIGGER fail_gate BEFORE UPDATE ON trading.strategy_equity_gate FOR EACH ROW EXECUTE FUNCTION trading.fail_gate_write()")
    with pytest.raises(asyncpg.RaiseError,match='test gate failure'):
        await append_acceptance(admin,payload(),0,None)
    assert await admin.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==0
    assert await admin.fetchval('SELECT allowed FROM trading.strategy_equity_gate')


@pytest.mark.asyncio
async def test_cancelled_activation_wait_releases_all_locks_and_preserves_old_state(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect();hrs=await connect('trading_user')
    await ready_gate(admin)
    await hrs.execute("SELECT pg_advisory_lock(hashtextextended('default',7183))")
    task=asyncio.create_task(append_acceptance(admin,payload(),0,None))
    try:
        await wait_for_advisory_wait(hrs,admin)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert await hrs.fetchval("SELECT pg_try_advisory_xact_lock(hashtextextended('classic-account-risk-v1:default',0))")
        assert await admin.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==0
        assert await admin.fetchval('SELECT allowed FROM trading.strategy_equity_gate')
    finally:await hrs.execute("SELECT pg_advisory_unlock(hashtextextended('default',7183))")


@pytest.mark.asyncio
async def test_idempotent_current_cas_does_not_invalidate_fresh_hrs_result(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect();await append_acceptance(admin,payload(),0,None)
    await ready_gate(admin)
    assert (await append_acceptance(admin,payload(),1,acceptance_digest(payload())))['status']=='already_applied'
    assert await admin.fetchval('SELECT allowed FROM trading.strategy_equity_gate')
    with pytest.raises(ValueError,match='compare_and_swap'):
        await append_acceptance(admin,payload(),1,'e'*64)


@pytest.mark.asyncio
async def test_invalid_cli_or_cas_inputs_are_rejected_without_writes(database):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect()
    for revision,digest in [(True,None),(-1,None),(0,'e'*64),(1,None),(1,'broken')]:
        with pytest.raises(ValueError,match='compare_and_swap_input'):
            await append_acceptance(admin,payload(),revision,digest)
    with pytest.raises(ValueError,match='payload_invalid'):
        await append_acceptance(admin,[],0,None)
    with pytest.raises(ValueError,match='compare_and_swap'):
        await append_acceptance(admin,payload(3),0,None)
    disabled=payload(1,False);disabled['approval_ref']=''
    with pytest.raises(ValueError,match='approval_missing'):
        await append_acceptance(admin,disabled,0,None)
    assert await admin.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==0


@pytest.mark.asyncio
async def test_cli_apply_is_explicit_and_errors_have_nonzero_exit(database,tmp_path):
    connect,env=database;admin=await connect()
    path=tmp_path/'receipt.json';path.write_text(json.dumps(payload()))
    async def run(*extra):
        process=await asyncio.create_subprocess_exec(sys.executable,'-m','scripts.activate_history_acceptance',
            '--payload',str(path),'--expected-revision','0','--expected-sha','none',*extra,
            cwd=ROOT,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        stdout,stderr=await process.communicate()
        assert not stderr,stderr.decode()
        return process.returncode,json.loads(stdout)
    code,result=await run('--apply')
    assert code==0 and result['status']=='applied'
    assert await admin.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==1
    bad=payload(2);path.write_text(json.dumps(bad))
    code,result=await run('--apply')
    assert code==1 and result['reason']=='history_acceptance_compare_and_swap_failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('allocation_anchor_id',None),('allocation_anchor_id',True),
    ('allocation_anchor_entries_sha256',None),('allocation_anchor_entries_sha256','invalid')])
async def test_first_approval_requires_a_well_formed_legacy_allocation_anchor(database,field,value):
    from scripts.activate_history_acceptance import append_acceptance
    connect,_=database;admin=await connect();candidate=payload();candidate[field]=value
    with pytest.raises(ValueError,match='anchor'):
        await append_acceptance(admin,candidate,0,None)
    assert await admin.fetchval('SELECT count(*) FROM trading.historical_risk_acceptance')==0
