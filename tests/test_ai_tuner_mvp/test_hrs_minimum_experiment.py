"""A new risk regime must be preregistered and checked for every replay input."""
import json
from types import SimpleNamespace
import pytest
from ai_tuner import hrs_exit_experiment as experiment
from ai_tuner.replay.models import DataBlocked
from strategies.hrs.risk_budget import FLOOR,STRICT
from tests.test_ai_tuner_mvp.test_hrs_exit_experiment import registration


def policy(version):
    return {'reason':json.dumps({'hrs_risk_budget_version':version})}

@pytest.mark.parametrize('version',['unknown',None])
def test_registration_rejects_unknown_budget_scope(version):
    r=registration();r['runtime_risk_budget_scope']=version
    with pytest.raises(ValueError,match='registered_runtime_risk_budget_scope_unknown'):
        experiment.validate_registration(r)

@pytest.mark.parametrize('bootstrap_version,input_version',[(STRICT,FLOOR),(FLOOR,STRICT),(FLOOR,'unknown')])
def test_budget_preregistration_checks_bootstrap_and_all_inputs(tmp_path,bootstrap_version,input_version):
    p=tmp_path/'hrs_decision_inputs.jsonl'
    p.write_text(json.dumps({'risk':{'entry_policy':policy(FLOOR)}})+'\n'+json.dumps({'risk':{'entry_policy':policy(input_version)}})+'\n')
    with pytest.raises(DataBlocked,match='registered_runtime_risk_budget_scope_mismatch'):
        experiment._registered_budget_validation({'runtime_risk_budget_scope':FLOOR},SimpleNamespace(snapshot_dir=tmp_path),{'entry_policy':policy(bootstrap_version)})


def test_registered_budget_reports_every_checked_input_without_changing_legacy(tmp_path):
    (tmp_path/'hrs_decision_inputs.jsonl').write_text(json.dumps({'risk':{'entry_policy':policy(FLOOR)}})+'\n')
    result=experiment._registered_budget_validation({'runtime_risk_budget_scope':FLOOR},SimpleNamespace(snapshot_dir=tmp_path),{'entry_policy':policy(FLOOR)})
    assert result==dict(status='VERIFIED',scope=FLOOR,input_rows_checked=1)
    assert experiment._registered_budget_validation({},None,{})==dict(status='NOT_REGISTERED',scope=None,input_rows_checked=0)

@pytest.mark.parametrize('registered_version,expected_runs',[(FLOOR,6),(STRICT,0)])
def test_budget_preregistration_is_enforced_by_real_replay_runner(tmp_path,registered_version,expected_runs):
    from datetime import timedelta
    from tests.test_ai_tuner_mvp.replay.test_hrs_minimum_budget import floor_fixture
    from tests.test_ai_tuner_mvp.replay.test_hrs_wiring import AT
    portal,_=floor_fixture(tmp_path)
    raw=portal.hrs_replay_state(AT)
    r=registration();r['runtime_risk_budget_scope']=registered_version
    r['runtime_admission_scope']='hrs_owned_and_unreleased_reservations_v2'
    r['baseline']['config']=raw['config'];r['baseline']['config_hashes']=raw['config_hashes']
    r['baseline']['effective_config_sha256']=experiment.fingerprint(raw['config'])
    r['windows'][0].update(start=AT.isoformat(),end=(AT+timedelta(hours=3)).isoformat(),snapshot_dir=str(portal.snapshot_dir))
    r['windows'][1].update(start='2026-12-01T00:00:00+08:00',end='2027-01-01T00:00:00+08:00')
    r['windows'][2].update(start='2027-01-01T00:00:00+08:00',end='2027-02-01T00:00:00+08:00')
    report=experiment.run_experiment(r,snapshot_roots=[portal.snapshot_dir])
    assert report['engine_runs']==expected_runs,report['blocked_reasons']
    assert report['runtime_risk_budget_scope']==registered_version
    if expected_runs:
        assert report['evaluations'][0]['tiers']['baseline']['runtime_risk_budget_scope_validation']==dict(status='VERIFIED',scope=FLOOR,input_rows_checked=2)
    else:
        assert any('registered_runtime_risk_budget_scope_mismatch' in reason for reason in report['blocked_reasons'])
    assert report['performance_accepted'] is False
