"""Controlled policy revision cannot silently change margin, expiry or other strategies."""
from datetime import datetime,timezone
import importlib.util,json
from pathlib import Path
import pytest

PATH=Path(__file__).parents[2]/'release/hrs-execution/activate_minimum_budget.py'

def module():
    if not PATH.exists():return None
    spec=importlib.util.spec_from_file_location('minimum_policy',PATH);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def previous():
    return dict(account_id='default',strategy_id='hrs',revision=6,effective_at='2026-11-14T03:09:52.457088+00:00',expires_at='2026-12-14T03:09:52.457088+00:00',mode='reduced',allow_add=False,multiplier=.5,ratio_cap=.15,entry_ceiling=26.18,freeze_allocation=True,reason='previous approved entry policy',created_at='2026-11-14T03:09:52.457088+00:00')

def test_candidate_preserves_exact_expiry_and_financial_caps():
    m=module();assert m is not None,'minimum budget activation helper missing'
    p=previous();c=m.prepare_candidate(p,'a'*40)
    assert c['previous_policy']==p and c['new_revision']==7
    reason=json.loads(c['new_reason']);assert reason['hrs_risk_budget_version']=='hrs_minimum_executable_v1'
    assert reason['release_sha']=='a'*40
    sql=m.activation_sql(c)
    assert 'expires_at' in sql and 'make_interval' not in sql
    assert 'pg_advisory_xact_lock' in sql and '7183' in sql
    assert 'strategy_risk_audit' in sql and 'position_claims' not in sql
    assert 'GRANT ' not in sql and 'DELETE ' not in sql and 'UPDATE trading.strategy_risk_policy' not in sql

@pytest.mark.parametrize('field,value',[('account_id','other'),('strategy_id','btc_eth_aggressive'),('revision',7),('allow_add',True),('freeze_allocation',False),('entry_ceiling',27),('multiplier',1),('ratio_cap',.2),('mode','monitor_only')])
def test_unapproved_policy_snapshot_cannot_be_activated(field,value):
    m=module();assert m is not None,'minimum budget activation helper missing'
    p=previous();p[field]=value
    with pytest.raises(ValueError):m.prepare_candidate(p,'a'*40)

def test_snapshot_or_reason_tampering_is_not_an_activation_contract():
    m=module();assert m is not None,'minimum budget activation helper missing'
    c=m.prepare_candidate(previous(),'a'*40);c['previous_policy']['entry_ceiling']=27
    with pytest.raises(ValueError):m.activation_sql(c)
    c=m.prepare_candidate(previous(),'a'*40);c['new_reason']='{"hrs_risk_budget_version":"unknown"}'
    with pytest.raises(ValueError):m.activation_sql(c)

import asyncio,os,uuid
import pytest_asyncio

@pytest_asyncio.fixture
async def policy_database():
    dsn=os.getenv('HRS_TEST_POSTGRES_DSN')
    if not dsn:pytest.skip('isolated administrator PostgreSQL DSN required')
    import asyncpg
    control=await asyncpg.connect(dsn);name='hrs_floor_policy_'+uuid.uuid4().hex
    await control.execute(f"CREATE DATABASE {name} TEMPLATE template0 ENCODING 'UTF8'")
    db=None
    try:
        db=await asyncpg.connect(dsn,database=name)
        await db.execute("CREATE SCHEMA trading; CREATE TABLE trading.position_claims(symbol text,strategy text,claim_state text,expires_at timestamptz);")
        await db.execute((Path(__file__).parents[2]/'database/postgres/migrations/2026-11-04-entry-risk-policy.sql').read_text())
        for strategy,revision,mode,multiplier,ratio,cap in [('hrs',6,'reduced','.5','.15','26.18'),('btc_eth',5,'capped','1','.20','30'),('btc_eth_aggressive',5,'reduced','.5','.15','25'),('new_coin',5,'capped','1','.25','40')]:
            await db.execute("INSERT INTO trading.strategy_risk_policy(account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason) VALUES('default',$1,$2,now()-interval '1 day',now()+interval '14 days',$3,false,$4,$5,$6,'approved fixture')",strategy,revision,mode,__import__('decimal').Decimal(multiplier),__import__('decimal').Decimal(ratio),__import__('decimal').Decimal(cap))
            await db.execute("INSERT INTO trading.strategy_risk_active VALUES('default',$1,$2)",strategy,revision)
        raw=await db.fetchval("SELECT row_to_json(p) FROM trading.strategy_risk_policy p WHERE strategy_id='hrs' AND revision=6")
        yield db,json.loads(raw),dsn,name
    finally:
        if db:await db.close()
        await control.execute(f'DROP DATABASE {name} WITH (FORCE)');await control.close()

@pytest.mark.asyncio
async def test_real_pg_exact_expiry_idempotent_cas_and_audited_rollback(policy_database):
    db,p,_,_=policy_database;m=module();c=m.prepare_candidate(p,'a'*40)
    others=await db.fetchval("SELECT jsonb_agg(to_jsonb(p) ORDER BY strategy_id) FROM trading.strategy_risk_policy p WHERE strategy_id<>'hrs'")
    await db.execute(m.activation_sql(c));await db.execute(m.activation_sql(c))
    now=json.loads(await db.fetchval("SELECT row_to_json(p) FROM trading.strategy_risk_policy p WHERE strategy_id='hrs' AND revision=7"))
    for field in ['expires_at','mode','allow_add','multiplier','ratio_cap','entry_ceiling','freeze_allocation']:assert now[field]==p[field]
    assert await db.fetchval("SELECT revision FROM trading.strategy_risk_active WHERE strategy_id='hrs'")==7
    assert await db.fetchval('SELECT count(*) FROM trading.strategy_risk_audit')==1
    assert await db.fetchval("SELECT jsonb_agg(to_jsonb(p) ORDER BY strategy_id) FROM trading.strategy_risk_policy p WHERE strategy_id<>'hrs'")==others
    await db.execute(m.activation_sql(c,rollback=True))
    assert await db.fetchval("SELECT revision FROM trading.strategy_risk_active WHERE strategy_id='hrs'")==6
    assert await db.fetchval('SELECT count(*) FROM trading.strategy_risk_audit')==2
    assert await db.fetchval("SELECT count(*) FROM trading.strategy_risk_policy WHERE strategy_id='hrs'")==2

@pytest.mark.asyncio
async def test_real_pg_changed_review_snapshot_rolls_back_without_policy_or_audit(policy_database):
    import asyncpg
    db,p,_,_=policy_database;m=module();changed=dict(p,reason='different reviewed snapshot');c=m.prepare_candidate(changed,'a'*40)
    with pytest.raises(asyncpg.RaiseError):await db.execute(m.activation_sql(c))
    await db.execute('ROLLBACK')
    assert await db.fetchval("SELECT revision FROM trading.strategy_risk_active WHERE strategy_id='hrs'")==6
    assert await db.fetchval("SELECT count(*) FROM trading.strategy_risk_policy WHERE strategy_id='hrs'")==1
    assert await db.fetchval('SELECT count(*) FROM trading.strategy_risk_audit')==0

@pytest.mark.asyncio
async def test_real_pg_concurrent_same_candidate_commits_one_revision_and_audit(policy_database):
    import asyncpg
    db,p,dsn,name=policy_database;m=module();sql=m.activation_sql(m.prepare_candidate(p,'a'*40))
    other=await asyncpg.connect(dsn,database=name)
    try:await asyncio.gather(db.execute(sql),other.execute(sql))
    finally:await other.close()
    assert await db.fetchval("SELECT revision FROM trading.strategy_risk_active WHERE strategy_id='hrs'")==7
    assert await db.fetchval('SELECT count(*) FROM trading.strategy_risk_audit')==1

def deploy_module():
    path=PATH.with_name('deploy_optimization.py')
    spec=importlib.util.spec_from_file_location('minimum_deploy_metadata',path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

def test_deploy_metadata_declares_effective_budget_change_separately_from_base_ratios():
    m=deploy_module()
    manifest={'source_hashes':{'strategies/hrs/entry_occupancy.py':'a'*64},
        'runtime_admission_scope':'hrs_owned_and_unreleased_reservations_v2',
        'runtime_risk_budget_scope':'hrs_minimum_executable_v1','target_hrs_policy_revision':7}
    actual=m.admission_change_metadata(manifest)
    assert actual['risk_budget_model_changed'] is True
    assert actual['effective_risk_limits_changed'] is True
    assert actual['risk_budget_percentages_changed'] is False
    assert actual['runtime_risk_budget_scope']=='hrs_minimum_executable_v1'
    assert actual['target_hrs_policy_revision']==7
    assert actual['sltp_parameters_changed'] is False

@pytest.mark.parametrize('scope,revision',[('unknown',7),('hrs_minimum_executable_v1',6),(None,7)])
def test_deploy_metadata_rejects_unrecognized_or_mismatched_budget_contract(scope,revision):
    m=deploy_module()
    manifest={'source_hashes':{},'runtime_risk_budget_scope':scope,'target_hrs_policy_revision':revision}
    with pytest.raises(ValueError):m.admission_change_metadata(manifest)

def test_legacy_deployment_manifest_does_not_claim_floor_enabled():
    result=deploy_module().admission_change_metadata({'source_hashes':{}})
    assert result['risk_budget_model_changed'] is False
    assert result['effective_risk_limits_changed'] is False
    assert result['runtime_risk_budget_scope'] is None
    assert result['target_hrs_policy_revision'] is None

@pytest.mark.asyncio
async def test_real_pg_missing_active_pointer_cannot_append_rollback_audit(policy_database):
    import asyncpg
    db,p,_,_=policy_database;m=module();c=m.prepare_candidate(p,'a'*40)
    await db.execute(m.activation_sql(c))
    await db.execute("DELETE FROM trading.strategy_risk_active WHERE strategy_id='hrs'")
    with pytest.raises(asyncpg.RaiseError):await db.execute(m.activation_sql(c,rollback=True))
    await db.execute('ROLLBACK')
    assert await db.fetchval('SELECT count(*) FROM trading.strategy_risk_audit')==1
    assert await db.fetchval("SELECT count(*) FROM trading.strategy_risk_active WHERE strategy_id='hrs'")==0


def test_floor_release_permission_audit_binds_uploaded_contract_and_never_applies(tmp_path,monkeypatch):
    import hashlib
    m=deploy_module();tool=tmp_path/'database_permissions.py';contract=tmp_path/'permissions.json'
    tool.write_text('verified audit tool');contract.write_text('{}')
    manifest={'permission_contract_sha256':hashlib.sha256(contract.read_bytes()).hexdigest(),
        'permission_tool_sha256':hashlib.sha256(tool.read_bytes()).hexdigest()}
    calls=[]
    def run(args,**kw):calls.append(args);return json.dumps({'ok':True,'mode':'audit','manifest_sha256':manifest['permission_contract_sha256']})
    monkeypatch.setattr(m,'run',run)
    report=m.permission_audit(tmp_path,manifest,'trading_platform')
    assert report['ok'] is True and calls[0][2]=='audit' and 'apply' not in calls[0]
    contract.write_text('{"changed":true}')
    with pytest.raises(ValueError):m.permission_audit(tmp_path,manifest,'trading_platform')
    assert len(calls)==1


def test_floor_release_permission_failure_blocks_activation(tmp_path,monkeypatch):
    import hashlib
    m=deploy_module();(tmp_path/'database_permissions.py').write_text('x');(tmp_path/'permissions.json').write_text('{}')
    manifest={'permission_contract_sha256':hashlib.sha256(b'{}').hexdigest(),'permission_tool_sha256':hashlib.sha256(b'x').hexdigest()}
    monkeypatch.setattr(m,'run',lambda *a,**kw:json.dumps({'ok':False,'mode':'audit','manifest_sha256':manifest['permission_contract_sha256']}))
    with pytest.raises(RuntimeError):m.permission_audit(tmp_path,manifest,'trading_platform')
