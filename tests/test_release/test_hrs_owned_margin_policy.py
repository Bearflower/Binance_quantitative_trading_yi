"""Own HRS quota opt-in must preserve previous risk policy and exact expiry."""
import importlib.util,json
from pathlib import Path
import pytest
import pytest_asyncio
from tests.test_release.test_hrs_minimum_budget_policy import policy_database,module as floor_module,previous as floor_previous
PATH=Path(__file__).parents[2]/'release/hrs-execution/activate_owned_margin.py'
OWN='hrs_owned_quota_account_free_v1'

def module():
    assert PATH.exists(),'owned margin activation helper missing'
    spec=importlib.util.spec_from_file_location('owned_margin_policy',PATH);result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

def previous():
    p=floor_previous();p['revision']=7;p['reason']=json.dumps({'hrs_risk_budget_version':'hrs_minimum_executable_v1','release_sha':'a'*40});return p

def test_owned_quota_reason_is_explicit_and_old_caps_expiry_preserved():
    m=module();p=previous();c=m.prepare_candidate(p,'b'*40)
    assert c['previous_policy']==p and c['expected_revision']==7 and c['new_revision']==8
    reason=json.loads(c['new_reason'])
    assert reason['hrs_risk_budget_version']=='hrs_minimum_executable_v1'
    assert reason['hrs_margin_scope']==OWN and reason['global_available_balance_required'] is True
    assert reason['base_single_ratio']=='.01' or reason['base_single_ratio']=='0.01'
    sql=m.activation_sql(c)
    assert 'expires_at' in sql and 'make_interval' not in sql
    assert 'pg_advisory_xact_lock' in sql and '7183' in sql
    assert 'GRANT ' not in sql and 'DELETE ' not in sql and 'position_claims' not in sql

@pytest.mark.parametrize('field,value',[('revision',6),('mode','capped'),('allow_add',True),('freeze_allocation',False),('entry_ceiling',30),('ratio_cap',.2),('multiplier',1),('reason','prose strict'),('reason','{"hrs_risk_budget_version":"hrs_minimum_executable_v1","hrs_margin_scope":"unknown"}')])
def test_other_previous_policy_is_not_the_approved_quota_change(field,value):
    p=previous();p[field]=value
    with pytest.raises(ValueError):module().prepare_candidate(p,'b'*40)

@pytest_asyncio.fixture
async def prior7(policy_database):
    db,p,dsn,name=policy_database
    await db.execute(floor_module().activation_sql(floor_module().prepare_candidate(p,'a'*40)))
    p=json.loads(await db.fetchval("SELECT row_to_json(p) FROM trading.strategy_risk_policy p WHERE strategy_id='hrs' AND revision=7"))
    yield db,p,dsn,name

@pytest.mark.asyncio
async def test_real_pg_policy8_preserves_flags_and_audits_idempotent_apply_and_rollback(prior7):
    db,p,_,_=prior7;m=module();c=m.prepare_candidate(p,'b'*40)
    original=await db.fetchval("SELECT jsonb_agg(to_jsonb(p) ORDER BY strategy_id,revision) FROM trading.strategy_risk_policy p")
    await db.execute(m.activation_sql(c))
    assert await db.fetchval("SELECT revision FROM trading.strategy_risk_active WHERE strategy_id='hrs'")==8
    await db.execute(m.activation_sql(c))
    current=json.loads(await db.fetchval("SELECT row_to_json(p) FROM trading.strategy_risk_policy p WHERE strategy_id='hrs' AND revision=8"))
    for key in ['expires_at','mode','allow_add','multiplier','ratio_cap','entry_ceiling','freeze_allocation']:assert current[key]==p[key]
    assert json.loads(current['reason'])['hrs_margin_scope']==OWN
    assert await db.fetchval("SELECT count(*) FROM trading.strategy_risk_audit WHERE strategy_id='hrs' AND revision=8")==1
    assert await db.fetchval("SELECT jsonb_agg(to_jsonb(p) ORDER BY strategy_id,revision) FROM trading.strategy_risk_policy p WHERE NOT(strategy_id='hrs' AND revision=8)")==original
    await db.execute(m.activation_sql(c,rollback=True))
    assert await db.fetchval("SELECT revision FROM trading.strategy_risk_active WHERE strategy_id='hrs'")==7
    assert await db.fetchval("SELECT count(*) FROM trading.strategy_risk_policy WHERE strategy_id='hrs'")==3
    assert await db.fetchval("SELECT count(*) FROM trading.strategy_risk_audit WHERE strategy_id='hrs' AND previous_revision=8 AND revision=7")==1

@pytest.mark.asyncio
async def test_real_pg_missing_pointer_never_records_owned_quota_rollback(prior7):
    import asyncpg
    db,p,_,_=prior7;m=module();c=m.prepare_candidate(p,'b'*40)
    await db.execute(m.activation_sql(c));await db.execute("DELETE FROM trading.strategy_risk_active WHERE strategy_id='hrs'")
    with pytest.raises(asyncpg.RaiseError):await db.execute(m.activation_sql(c,rollback=True))
    await db.execute('ROLLBACK')
    assert await db.fetchval("SELECT count(*) FROM trading.strategy_risk_audit WHERE previous_revision=8")==0

@pytest.mark.asyncio
async def test_real_pg_concurrent_owned_quota_activation_is_single_audit(prior7):
    import asyncio,asyncpg
    db,p,dsn,name=prior7;m=module();sql=m.activation_sql(m.prepare_candidate(p,'b'*40));other=await asyncpg.connect(dsn,database=name)
    try:await asyncio.gather(db.execute(sql),other.execute(sql))
    finally:await other.close()
    assert await db.fetchval("SELECT count(*) FROM trading.strategy_risk_audit WHERE strategy_id='hrs' AND revision=8")==1
    assert await db.fetchval("SELECT revision FROM trading.strategy_risk_active WHERE strategy_id='hrs'")==8


def test_release_metadata_binds_owned_quota_and_revision8():
    from tests.test_release.test_hrs_minimum_budget_policy import deploy_module
    manifest=dict(source_hashes={'strategies/hrs/entry_occupancy.py':'a'*64},runtime_admission_scope='hrs_owned_and_unreleased_reservations_v2',runtime_risk_budget_scope='hrs_minimum_executable_v1',runtime_margin_scope=OWN,target_hrs_policy_revision=8)
    actual=deploy_module().admission_change_metadata(manifest)
    assert actual['runtime_margin_scope']==OWN and actual['margin_scope_changed'] is True
    assert actual['account_available_balance_required'] is True
    assert actual['target_hrs_policy_revision']==8 and actual['sltp_parameters_changed'] is False

@pytest.mark.parametrize('scope,revision',[(OWN,7),('unknown',8),(None,8)])
def test_release_metadata_rejects_unknown_or_misaligned_margin_scope(scope,revision):
    from tests.test_release.test_hrs_minimum_budget_policy import deploy_module
    manifest=dict(source_hashes={},runtime_risk_budget_scope='hrs_minimum_executable_v1',runtime_margin_scope=scope,target_hrs_policy_revision=revision)
    with pytest.raises(ValueError):deploy_module().admission_change_metadata(manifest)
