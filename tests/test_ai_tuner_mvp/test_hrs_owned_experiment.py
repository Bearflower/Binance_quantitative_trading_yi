"""Prospective experiments must bind the approved cash/quota scope before replay."""
import json
from types import SimpleNamespace
import pytest
from ai_tuner import hrs_exit_experiment as experiment
from ai_tuner.replay.models import DataBlocked
from strategies.hrs.margin_budget import ALL_ACCOUNT,HRS_OWNED
from strategies.hrs.risk_budget import FLOOR
from tests.test_ai_tuner_mvp.test_hrs_exit_experiment import registration

def financial(scope=HRS_OWNED):
    return dict(entry_policy=dict(account_id='test',reason=json.dumps(dict(hrs_risk_budget_version=FLOOR,hrs_margin_scope=scope))),hrs_margin_scope=scope,account_available_balance='100',account_available_balance_source='papi/v1/account.totalAvailableBalance',account_status='NORMAL',account_cash_observed_at='2026-11-19T00:00:00+00:00',account_pending_reservations=[])

@pytest.mark.parametrize('scope',['unknown',None])
def test_registration_unknown_margin_scope_is_not_a_valid_regime(scope):
    r=registration();r['runtime_margin_scope']=scope
    with pytest.raises(ValueError,match='registered_runtime_margin_scope_unknown'):experiment.validate_registration(r)

@pytest.mark.parametrize('source,bad',[('bootstrap',ALL_ACCOUNT),('input',ALL_ACCOUNT),('input','unknown')])
def test_scope_mismatch_is_detected_before_any_candidate_execution(tmp_path,source,bad):
    boot=financial();rows=[financial(),financial()]
    if source=='bootstrap':boot=financial(bad)
    else:rows[-1]=financial(bad)
    (tmp_path/'hrs_decision_inputs.jsonl').write_text(''.join(json.dumps(dict(risk=r))+'\n' for r in rows))
    with pytest.raises(DataBlocked,match='registered_runtime_margin_scope_mismatch'):
        experiment._registered_margin_validation(dict(runtime_margin_scope=HRS_OWNED),SimpleNamespace(snapshot_dir=tmp_path),boot)

@pytest.mark.parametrize('field,value',[('account_available_balance',None),('account_status','REDUCE_ONLY'),('account_pending_reservations',None),('account_cash_observed_at',None),('account_cash_observed_at','2026-11-19T00:00:00')])
def test_registered_owned_scope_requires_original_cash_sources(tmp_path,field,value):
    row=financial();row[field]=value
    (tmp_path/'hrs_decision_inputs.jsonl').write_text(json.dumps(dict(risk=row))+'\n')
    with pytest.raises(DataBlocked,match='registered_runtime_margin_scope'):
        experiment._registered_margin_validation(dict(runtime_margin_scope=HRS_OWNED),SimpleNamespace(snapshot_dir=tmp_path),financial())

def test_margin_registration_counts_all_inputs_and_keeps_old_unregistered_behavior(tmp_path):
    (tmp_path/'hrs_decision_inputs.jsonl').write_text(json.dumps(dict(risk=financial()))+'\n')
    assert experiment._registered_margin_validation(dict(runtime_margin_scope=HRS_OWNED),SimpleNamespace(snapshot_dir=tmp_path),financial())==dict(status='VERIFIED',scope=HRS_OWNED,input_rows_checked=1)
    assert experiment._registered_margin_validation({},None,{})==dict(status='NOT_REGISTERED',scope=None,input_rows_checked=0)

def test_engine_identity_contains_the_actual_margin_module():
    result=experiment.engine_identity()
    assert 'strategies/hrs/margin_budget.py' in result['source_files']

@pytest.mark.parametrize('declared_scope,expected_runs',[(HRS_OWNED,6),(ALL_ACCOUNT,0)])
def test_owned_registration_is_enforced_by_sealed_original_sources_and_real_runner(tmp_path,declared_scope,expected_runs):
    from datetime import timedelta
    from tests.test_ai_tuner_mvp.replay.test_hrs_owned_margin_parity import owned_fixture
    from tests.test_ai_tuner_mvp.replay.test_hrs_wiring import AT
    portal,_=owned_fixture(tmp_path)
    raw=portal.hrs_replay_state(AT)
    r=registration();r.update(runtime_margin_scope=declared_scope,
        runtime_risk_budget_scope=FLOOR,runtime_admission_scope='hrs_owned_and_unreleased_reservations_v2')
    r['baseline'].update(config=raw['config'],config_hashes=raw['config_hashes'],
        effective_config_sha256=experiment.fingerprint(raw['config']))
    r['windows'][0].update(start=AT.isoformat(),end=(AT+timedelta(hours=3)).isoformat(),snapshot_dir=str(portal.snapshot_dir))
    r['windows'][1].update(start='2026-12-01T00:00:00+08:00',end='2027-01-01T00:00:00+08:00')
    r['windows'][2].update(start='2027-01-01T00:00:00+08:00',end='2027-02-01T00:00:00+08:00')
    report=experiment.run_experiment(r,snapshot_roots=[portal.snapshot_dir])
    assert report['engine_runs']==expected_runs,report['blocked_reasons']
    assert report['runtime_margin_scope']==declared_scope
    if expected_runs:
        assert report['evaluations'][0]['tiers']['baseline']['runtime_margin_scope_validation']==dict(status='VERIFIED',scope=HRS_OWNED,input_rows_checked=2)
    else:
        assert any('registered_runtime_margin_scope_mismatch' in reason for reason in report['blocked_reasons'])
    assert report['performance_accepted'] is False
